from __future__ import annotations

"""토스증권 Open API 클라이언트 (지원 — 라이브 검증).

토스증권 공식 Open API(https://developers.tossinvest.com/docs, OpenAPI 3.0)를 보고 구현했다.
KISClient 과 같은 인터페이스를 따르되, 시장시계·호가단위 등 브로커 무관 정적 헬퍼는 KISClient
것을 그대로 물려쓰고(상속), 네트워크 메서드만 토스 API 로 갈아끼운다.

확인한 명세(2026-06):
  base    : https://openapi.tossinvest.com
  인증    : POST /oauth2/token  (Authorization: Basic base64(id:secret),
            Content-Type: x-www-form-urlencoded, body grant_type=client_credentials)
            → {access_token, token_type:"Bearer", expires_in}
  계좌    : GET  /api/v1/accounts            → [{accountSeq, accountNo, accountType}]
            이후 모든 계좌/주문 요청에 헤더 X-Tossinvest-Account: {accountSeq}
  시세    : GET  /api/v1/prices?symbols=005930 → [{symbol, lastPrice, currency, timestamp}]
  캔들    : GET  /api/v1/candles
  보유    : GET  /api/v1/holdings            → {items:[{symbol, quantity, averagePurchasePrice,
                                                        lastPrice, name, marketCountry, currency}]}
  매수가능: GET  /api/v1/buying-power
  주문    : POST /api/v1/orders  {symbol, side:BUY|SELL, orderType:LIMIT|MARKET,
                                  quantity, price?, timeInForce?:DAY|CLS} → {orderId}
  취소/정정: POST /api/v1/orders/{orderId}/cancel · /modify

**안전 정책 (샌드박스 없음)**:
  - 주문은 기본 dry-run(전송 안 함, 보낼 본문만 로깅). 실제 전송은 TOSS_LIVE_ORDERS=true 일 때만
    (토스는 모의투자 샌드박스가 없어 실계좌 직행 — KIS_VIRTUAL 의 토스판 안전장치).
  - 모든 주문은 KIS 와 동일한 OrderSafetyValidator 단일 관문을 통과한다.
  - 국내+미국 주식 모두 지원(같은 엔드포인트, ticker+USD). 호가창(orderbook)만 미지원(None).
  - 등락률·거래량은 /prices 가 안 줘서 /candles(일봉 2개)로 파생 보강(_enrich_quote, 60s 캐시).

라이브 검증(2026-06): 토큰·계좌(accountSeq 자동탐색)·시세·일/분봉·잔고·매수가능·환율·미국시세(AAPL)·
USD 매수가능 전부 200 정상. 주문 본문 매핑도 라이브 확인(LIMIT → 200+orderId, 목록·취소 동작).
주의: MARKET 매수는 토스가 상한가 버퍼를 잡아 현금 빠듯하면 거부(insufficient-buying-power) →
KR 매수는 marketable LIMIT 권장. 파생/인버스 ETF 는 계좌의 위험고지 등록이 필요(prerequisite-required).
"""

import base64
import json as _json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from zusik import paths
from zusik.clients.kis_client import KISClient

logger = logging.getLogger(__name__)

TOSS_BASE = "https://openapi.tossinvest.com"

# 토스 rate limit(429) 회피용 self-throttle — KISClient 의 자체 스로틀을 네트워크 메서드 오버라이드로
# 우회하므로, 토스 전용으로 요청 간 최소 간격을 둔다. 실측: 동시 30요청 중 ~10만 통과(나머지 429),
# 이후 윈도우 내 추가 요청도 429. 모듈 레벨 락이라 여러 인스턴스/스레드가 전역으로 공유한다.
_TOSS_RATE_LOCK = threading.Lock()
_TOSS_LAST_TS = [0.0]
_TOSS_MIN_INTERVAL = 0.15        # 초당 ~6-7요청 (버스트 차단)


def _toss_live_orders() -> bool:
    """실제 주문 전송 허용 여부. 기본 False(dry-run) — 검증 전 사고 방지."""
    return os.getenv("TOSS_LIVE_ORDERS", "").strip().lower() in ("1", "true", "yes", "on")


class TossClient(KISClient):
    """토스증권 Open API 클라이언트. KISClient 인터페이스 호환(국내+미국, 라이브 검증)."""

    _TOKEN_FILE = paths.data_path("toss_token.json")

    def __init__(self, app_key: str, app_secret: str, account_no: str,
                 account_prod: str = "01", is_virtual: bool = False):
        # app_key=client_id, app_secret=client_secret (토스 OAuth2 클라이언트 자격)
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_no = str(account_no or "")
        self.account_prod = account_prod
        self.is_virtual = is_virtual            # 토스는 모의 샌드박스 미제공 — 의미상 보관만
        self.base_url = TOSS_BASE
        self._access_token = ""
        self._token_expires = datetime.min
        self._account_seq: str | None = None
        self._balance_cache: dict | None = None
        self._name_cache: dict = {}
        self._quote_enrich_cache: dict = {}     # symbol → (ts, dict): 캔들 파생 등락률/거래량
        # 주문 관문 안전 검증(KIS 와 동일 단일 관문)
        from zusik.core.resilience import OrderSafetyValidator
        self._order_safety = OrderSafetyValidator()
        self._last_order_ts: dict = {}
        logger.info("토스증권 클라이언트 활성 — 주문 %s.",
                    "실전송(TOSS_LIVE_ORDERS=true)" if _toss_live_orders()
                    else "기본 dry-run (실주문은 TOSS_LIVE_ORDERS=true)")

    # ── 인증 ──
    def _ensure_token(self):
        if self._access_token and datetime.now() < self._token_expires - timedelta(minutes=10):
            return
        # 파일 캐시 재사용(다른 프로세스 발급분)
        try:
            if os.path.exists(self._TOKEN_FILE):
                with open(self._TOKEN_FILE, encoding="utf-8") as f:
                    cached = _json.load(f)
                exp = datetime.strptime(cached["expires"], "%Y-%m-%d %H:%M:%S")
                if datetime.now() < exp - timedelta(minutes=10):
                    self._access_token, self._token_expires = cached["token"], exp
                    return
        except Exception:
            pass
        basic = base64.b64encode(f"{self.app_key}:{self.app_secret}".encode()).decode()
        resp = requests.post(
            f"{self.base_url}/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires = datetime.now() + timedelta(seconds=int(data.get("expires_in", 3600)))
        try:
            paths.write_json_atomic(self._TOKEN_FILE, {
                "token": self._access_token,
                "expires": self._token_expires.strftime("%Y-%m-%d %H:%M:%S")})
        except Exception:
            pass

    def _headers(self, with_account: bool = False) -> dict:
        self._ensure_token()
        h = {"Authorization": f"Bearer {self._access_token}"}
        if with_account:
            h["X-Tossinvest-Account"] = str(self._get_account_seq())
        return h

    def _invalidate_token(self):
        self._access_token = ""
        try:
            if os.path.exists(self._TOKEN_FILE):
                os.remove(self._TOKEN_FILE)
        except Exception:
            pass

    @staticmethod
    def _throttle():
        """요청 간 최소 간격 유지 (전역 직렬화) — 토스 429 버스트 차단."""
        with _TOSS_RATE_LOCK:
            now = time.monotonic()
            wait = _TOSS_MIN_INTERVAL - (now - _TOSS_LAST_TS[0])
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            _TOSS_LAST_TS[0] = now

    @staticmethod
    def _retry_after(resp, attempt: int) -> float:
        """429/5xx 재시도 대기. Retry-After 헤더 우선, 없으면 지수 백오프(상한 3s)."""
        try:
            ra = (getattr(resp, "headers", {}) or {}).get("Retry-After")
            if ra:
                return min(float(ra), 5.0)
        except Exception:
            pass
        return min(0.3 * (2 ** (attempt - 1)), 3.0)   # 0.3 → 0.6 → 1.2 → ...

    def _toss_request(self, method: str, path: str, *, params=None, body=None,
                      with_account: bool = False):
        # 401: 캐시 토큰 무효 → 재발급 후 재시도. 429/5xx: 백오프 후 재시도(rate limit/일시 오류 견고성).
        url = f"{self.base_url}{path}"
        last = None
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            headers = self._headers(with_account)
            self._throttle()
            if method == "GET":
                r = requests.get(url, headers=headers, params=params or {}, timeout=12)
            else:
                headers["Content-Type"] = "application/json"
                r = requests.post(url, headers=headers, json=body, timeout=12)
            sc = getattr(r, "status_code", 200)
            if sc == 401 and attempt == 1:
                self._invalidate_token()
                last = r
                continue
            if (sc == 429 or 500 <= sc < 600) and attempt < max_attempts:
                wait = self._retry_after(r, attempt)
                logger.debug("토스 %s %s → %.2fs 후 재시도 (%d/%d)", path, sc, wait, attempt, max_attempts)
                time.sleep(wait)
                last = r
                continue
            r.raise_for_status()
            return r.json()
        if last is not None:
            last.raise_for_status()   # 재시도 소진 → 마지막 응답 예외
        raise requests.HTTPError(f"토스 요청 재시도 소진: {path}")

    def _toss_get(self, path: str, params: dict | None = None, with_account: bool = False):
        return self._toss_request("GET", path, params=params, with_account=with_account)

    def _toss_post(self, path: str, body: dict, with_account: bool = True):
        return self._toss_request("POST", path, body=body, with_account=with_account)

    @staticmethod
    def _result(data):
        """토스 응답 봉투 {"result": ...} 에서 result 추출. 봉투가 아니면 그대로."""
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    def _get_account_seq(self) -> str:
        if self._account_seq:
            return self._account_seq
        accts = self._result(self._toss_get("/api/v1/accounts")) or []
        seq = ""
        for a in accts:
            if not self.account_no or str(a.get("accountNo", "")).startswith(self.account_no):
                seq = str(a.get("accountSeq", ""))
                break
        if not seq and accts:
            seq = str(accts[0].get("accountSeq", ""))
        self._account_seq = seq
        return seq

    # ── 시세 ──
    @staticmethod
    def _f(v) -> float:
        try:
            return float(v)
        except Exception:
            return 0.0

    _QUOTE_ENRICH_TTL = 60   # 캔들 파생 등락률/거래량 캐시(초) — price 호출마다 캔들 재조회 방지

    def _enrich_quote(self, symbol: str) -> dict:
        """일봉 캔들에서 등락률·거래량·고저시가·전일종가를 파생.

        토스 /prices 는 lastPrice 만 줘서 crash/surge·사이징이 약화된다(문서화된 한계). 이미
        검증된 /candles 로 보강 — 최신 봉=당일(형성 중, 거래량=당일 누적), 직전 봉 종가=전일종가.
        best-effort: 실패하면 빈 dict(원래 lastPrice-only 동작으로 폴백). 공식 API 만 사용."""
        now = time.time()
        cached = self._quote_enrich_cache.get(symbol)
        if cached and now - cached[0] < self._QUOTE_ENRICH_TTL:
            return cached[1]
        out: dict = {}
        try:
            df = self._candles(symbol, "D", 2)   # 과거→최신 순으로 정렬돼 돌아온다
            if df is not None and len(df) >= 1:
                last = df.iloc[-1]
                out = {
                    "volume": int(self._f(last.get("volume", 0))),
                    "high": self._f(last.get("high", 0)),
                    "low": self._f(last.get("low", 0)),
                    "open": self._f(last.get("open", 0)),
                }
                if len(df) >= 2:
                    prev_close = self._f(df.iloc[-2].get("close", 0))
                    last_close = self._f(last.get("close", 0))
                    out["prev_close"] = prev_close
                    if prev_close > 0:
                        out["change_rate"] = (last_close / prev_close - 1) * 100
        except Exception as e:
            logger.debug("토스 캔들 보강 실패 %s: %s", symbol, e)
        self._quote_enrich_cache[symbol] = (now, out)
        return out

    def get_current_price(self, stock_code: str) -> dict:
        """현재가. /prices(lastPrice) + /candles 파생(등락률·거래량·고저시가)으로 보강."""
        try:
            data = self._toss_get("/api/v1/prices", params={"symbols": stock_code})
        except Exception as e:
            logger.warning("토스 시세 조회 실패 %s: %s", stock_code, e)
            return {"price": 0, "change_rate": 0.0, "volume": 0, "name": stock_code}
        rows = self._result(data) or []
        row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else {})
        price = int(self._f(row.get("lastPrice", 0)))
        enr = self._enrich_quote(stock_code)
        return {
            "price": price,
            "change_rate": enr.get("change_rate", 0.0), "volume": enr.get("volume", 0),
            "high": int(enr.get("high", 0)), "low": int(enr.get("low", 0)),
            "open": int(enr.get("open", 0)), "prev_close": int(enr.get("prev_close", 0)),
            "market_cap": 0, "per": 0.0, "pbr": 0.0,
            "name": self._name_cache.get(stock_code, stock_code),
        }

    def _candles(self, symbol: str, period: str, count: int):
        """공유 캔들 조회 (KR/US 공통). interval 은 1d/1m 만 지원(주/월 → 1d 폴백)."""
        interval = "1m" if period in ("m", "1m") else "1d"
        try:
            data = self._toss_get("/api/v1/candles", params={
                "symbol": symbol, "interval": interval, "count": count})
        except Exception as e:
            logger.debug("토스 캔들 실패 %s: %s", symbol, e)
            return None
        res = self._result(data)
        rows = res.get("candles") if isinstance(res, dict) else (res or [])
        if not rows:
            return None
        try:
            df = pd.DataFrame([{
                "open": self._f(r.get("openPrice")),
                "high": self._f(r.get("highPrice")),
                "low": self._f(r.get("lowPrice")),
                "close": self._f(r.get("closePrice")),
                "volume": self._f(r.get("volume")),
            } for r in rows])
            # 토스는 최신→과거 순으로 주므로 과거→최신으로 뒤집는다.
            return df.iloc[::-1].reset_index(drop=True) if not df.empty else None
        except Exception:
            return None

    def get_ohlcv(self, stock_code: str, period: str = "D", count: int = 100):
        return self._candles(stock_code, period, count)

    def get_daily_long(self, stock_code: str, days: int = 250, period: str = "D"):
        return self.get_ohlcv(stock_code, period=period, count=days)

    # ── 잔고 ──
    def invalidate_balance_cache(self):
        self._balance_cache = None

    def get_balance(self) -> dict:
        cache = self._balance_cache
        now = time.time()
        if cache and now - cache.get("ts", 0) < self._BALANCE_TTL_SEC:
            return cache["data"]
        holdings, total_eval, total_profit = [], 0, 0
        try:
            res = self._result(self._toss_get("/api/v1/holdings", with_account=True))
            res = res if isinstance(res, dict) else {}
            # 요약(평가금액·손익)은 보유 0이어도 신뢰 가능
            total_eval = int(self._f(((res.get("marketValue") or {}).get("amount") or {}).get("krw", 0)))
            total_profit = int(self._f(((res.get("profitLoss") or {}).get("amount") or {}).get("krw", 0)))
            for it in (res.get("items") or []):
                if str(it.get("currency", "KRW")).upper() == "USD":
                    continue   # 미국 종목은 get_us_balance 담당 (KR 잔고에서 제외)
                qty = int(self._f(it.get("quantity", 0)))
                if qty <= 0:
                    continue
                code = str(it.get("symbol", ""))
                if it.get("name"):
                    self._name_cache[code] = it["name"]
                avg = int(self._f(it.get("averagePurchasePrice", 0)))
                cur = int(self._f(it.get("lastPrice", 0)))
                holdings.append({
                    "code": code, "name": it.get("name", code), "qty": qty,
                    "avg_price": avg, "current_price": cur, "eval_amount": cur * qty,
                    "profit": (cur - avg) * qty, "profit_rate": (cur / avg - 1) * 100 if avg else 0.0,
                })
        except Exception as e:
            logger.warning("토스 보유 조회 실패: %s", e)
        cash = 0
        try:
            res = self._result(self._toss_get("/api/v1/buying-power",
                                              params={"currency": "KRW"}, with_account=True))
            cash = int(self._f((res or {}).get("cashBuyingPower", 0)))
        except Exception as e:
            logger.debug("토스 매수가능금액 실패: %s", e)
        result = {
            "cash": cash, "total_cash": cash, "nxdy_cash": 0, "d2_cash": 0,
            "total_eval": total_eval, "total_profit": total_profit,
            "total_profit_rate": (total_profit / (total_eval - total_profit) * 100)
            if (total_eval - total_profit) else 0.0,
            "holdings": holdings,
        }
        self._balance_cache = {"ts": now, "data": result}
        return result

    def get_stock_name(self, stock_code: str) -> str:
        if stock_code in self._name_cache:
            return self._name_cache[stock_code]
        try:
            self.get_balance()  # 보유 종목이면 이름 캐시됨
        except Exception:
            pass
        return self._name_cache.get(stock_code, stock_code)

    # ── 주문 (OrderSafetyValidator 게이트 + 기본 dry-run) ──
    def buy_market(self, stock_code: str, qty: int) -> dict:
        return self._toss_order(stock_code, "buy", qty, price=0, order_type="01")

    def sell_market(self, stock_code: str, qty: int) -> dict:
        return self._toss_order(stock_code, "sell", qty, price=0, order_type="01")

    def _toss_order(self, code: str, side: str, qty, price, order_type: str,
                    us: bool = False) -> dict:
        """order_type: "00"=지정가(LIMIT), "01"=시장가(MARKET). us=True 면 미국(USD·ticker)."""
        toss_side = "BUY" if side == "buy" else "SELL"
        toss_type = "MARKET" if order_type == "01" else "LIMIT"

        # 안전 게이트(KIS 와 동일): 잔고 스냅샷 → 검증 → fail-closed. US 는 USD 잔고로 검증.
        held_qty = orderable_cash = None
        try:
            if us:
                bal = self.get_us_balance()
                orderable_cash = bal.get("cash_usd")
                held_qty = next((h["qty"] for h in bal.get("holdings", []) if h.get("ticker") == code), 0)
            else:
                bal = self.get_balance()
                orderable_cash = bal.get("cash")
                held_qty = next((h["qty"] for h in bal.get("holdings", []) if h.get("code") == code), 0)
        except Exception:
            pass
        market_price = 0.0
        if order_type == "00" and price and price > 0:
            quote = self.get_us_current_price(code) if us else self.get_current_price(code)
            market_price = self._f(quote.get("price", 0))
        _prev = self._last_order_ts.get(code)
        _last_opp = _prev[1] if (_prev and _prev[0] != side) else 0.0
        ok, why = self._order_safety.validate(
            side=side, code=code, qty=qty, price=price, order_type=order_type,
            held_qty=held_qty, orderable_cash=orderable_cash,
            market_price=market_price, last_opposite_ts=_last_opp)
        if not ok:
            logger.critical("토스 주문 안전 차단 (%s %s %s주): %s", side, code, qty, why)
            return {"success": False, "message": f"안전 검증 차단: {why}", "order_no": "", "blocked": True}
        self._last_order_ts[code] = (side, time.time())

        body = {"symbol": code, "side": toss_side, "orderType": toss_type, "quantity": str(qty)}
        if toss_type == "LIMIT":
            body["price"] = str(price)

        if not _toss_live_orders():
            logger.warning("[토스 dry-run] 주문 미전송 — TOSS_LIVE_ORDERS=true 시 실제 전송: %s", body)
            return {"success": False, "message": "dry-run (TOSS_LIVE_ORDERS 미설정)",
                    "order_no": "", "dry_run": True}
        try:
            data = self._toss_post("/api/v1/orders", body=body)
        except Exception as e:
            logger.error("토스 주문 전송 실패 (%s %s %s주): %s", side, code, qty, e)
            return {"success": False, "message": f"전송 실패: {e}", "order_no": ""}
        oid = str((self._result(data) or {}).get("orderId", "") or "")
        if oid:
            self.invalidate_balance_cache()
        return {"success": bool(oid), "message": "ok" if oid else str(data), "order_no": oid}

    def revise_order(self, stock_code: str, order_no: str, qty: int, price: int,
                     order_type: str = "00", org_branch: str = "") -> dict:
        if not _toss_live_orders():
            logger.warning("[토스 dry-run] 정정 미전송: %s qty=%s price=%s", order_no, qty, price)
            return {"success": False, "message": "dry-run", "order_no": order_no, "dry_run": True}
        body = {"quantity": str(qty)}
        if price > 0:
            body["price"] = str(price)
        try:
            self._toss_post(f"/api/v1/orders/{order_no}/modify", body=body)
            self.invalidate_balance_cache()
            return {"success": True, "message": "ok", "order_no": order_no}
        except Exception as e:
            return {"success": False, "message": str(e), "order_no": order_no}

    def cancel_order(self, order_no: str) -> dict:
        if not _toss_live_orders():
            logger.warning("[토스 dry-run] 취소 미전송: %s", order_no)
            return {"success": False, "message": "dry-run", "dry_run": True}
        try:
            self._toss_post(f"/api/v1/orders/{order_no}/cancel", body={})
            self.invalidate_balance_cache()
            return {"success": True, "message": "ok"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── 미국 주식 (KR 과 동일 엔드포인트, ticker + USD) ──
    def get_us_current_price(self, ticker: str, exchange: str = "NASD") -> dict:
        """미국 현재가. 토스 /api/v1/prices 는 lastPrice 위주(등락률·거래량 0)."""
        try:
            data = self._toss_get("/api/v1/prices", params={"symbols": ticker})
        except Exception as e:
            logger.warning("토스 미국 시세 실패 %s: %s", ticker, e)
            return {"price": 0.0, "change_rate": 0.0, "volume": 0, "name": ticker, "currency": "USD"}
        rows = self._result(data) or []
        row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else {})
        enr = self._enrich_quote(ticker)
        return {
            "price": self._f(row.get("lastPrice", 0)),
            "change_rate": enr.get("change_rate", 0.0), "volume": enr.get("volume", 0),
            "high": enr.get("high", 0.0), "low": enr.get("low", 0.0),
            "open": enr.get("open", 0.0), "prev_close": enr.get("prev_close", 0.0),
            "name": self._name_cache.get(ticker, ticker),
            "currency": row.get("currency", "USD"),
        }

    def get_us_ohlcv(self, ticker: str, exchange: str = "NASD", period: str = "D", count: int = 100):
        return self._candles(ticker, period, count)

    def get_us_daily_long(self, ticker: str, exchange: str = "NASD", days: int = 250, period: str = "D"):
        return self._candles(ticker, period, days)

    def get_us_minute_ohlcv(self, ticker: str, exchange: str = "NASD", minutes: int = 60):
        return self._candles(ticker, "m", minutes)

    def get_us_balance(self) -> dict:
        """미국 잔고. /holdings(USD 항목) + /buying-power?currency=USD. KIS get_us_balance 계약."""
        cash_usd = 0.0
        try:
            res = self._result(self._toss_get("/api/v1/buying-power",
                                              params={"currency": "USD"}, with_account=True))
            cash_usd = self._f((res or {}).get("cashBuyingPower", 0))
        except Exception as e:
            logger.debug("토스 USD 매수가능 실패: %s", e)
        holdings, total_eval, total_profit = [], 0.0, 0.0
        try:
            res = self._result(self._toss_get("/api/v1/holdings", with_account=True))
            res = res if isinstance(res, dict) else {}
            total_eval = self._f(((res.get("marketValue") or {}).get("amount") or {}).get("usd", 0))
            total_profit = self._f(((res.get("profitLoss") or {}).get("amount") or {}).get("usd", 0))
            for it in (res.get("items") or []):
                if str(it.get("currency", "")).upper() != "USD":
                    continue   # 미국 종목만
                qty = int(self._f(it.get("quantity", 0)))
                if qty <= 0:
                    continue
                tk = str(it.get("symbol", ""))
                if it.get("name"):
                    self._name_cache[tk] = it["name"]
                avg = self._f(it.get("averagePurchasePrice", 0))
                cur = self._f(it.get("lastPrice", 0))
                holdings.append({
                    "ticker": tk, "name": it.get("name", tk), "exchange": "NASD",
                    "qty": qty, "avg_price": avg, "current_price": cur,
                    "eval_amount": cur * qty, "profit": (cur - avg) * qty,
                    "profit_rate": (cur / avg - 1) * 100 if avg else 0.0,
                })
        except Exception as e:
            logger.warning("토스 미국 보유 조회 실패: %s", e)
        return {
            "cash_usd": cash_usd, "display_cash_usd": cash_usd, "sell_pending_usd": 0.0,
            "us_eval_usd": total_eval, "total_eval_usd": total_eval,
            "total_profit_usd": total_profit,
            "total_profit_rate": (total_profit / (total_eval - total_profit) * 100)
            if (total_eval - total_profit) else 0.0,
            "us_krw_in_account": 0, "currency_breakdown": [],
            "total_eval_krw": 0, "total_asset_krw": 0,
            "unsettled_sell_krw": 0, "unsettled_buy_krw": 0,
            "holdings": holdings,
        }

    def get_usd_krw_rate(self) -> float:
        """USD/KRW 환율. /api/v1/exchange-rate?baseCurrency=USD&quoteCurrency=KRW."""
        try:
            res = self._result(self._toss_get(
                "/api/v1/exchange-rate", params={"baseCurrency": "USD", "quoteCurrency": "KRW"}))
            rate = self._f((res or {}).get("rate", 0))
            return rate if rate > 0 else 1300.0
        except Exception as e:
            logger.debug("토스 환율 실패(폴백 1300): %s", e)
            return 1300.0

    def buy_us_limit(self, ticker: str, qty: int, price: float, exchange: str = "NASD") -> dict:
        return self._toss_order(ticker, "buy", qty, price=price, order_type="00", us=True)

    def sell_us_limit(self, ticker: str, qty: int, price: float, exchange: str = "NASD") -> dict:
        return self._toss_order(ticker, "sell", qty, price=price, order_type="00", us=True)

    def sell_us_market(self, ticker: str, qty: int, exchange: str = "NASD") -> dict:
        return self._toss_order(ticker, "sell", qty, price=0, order_type="01", us=True)

    def buy_us_market(self, ticker: str, qty: int, exchange: str = "NASD") -> dict:
        return self._toss_order(ticker, "buy", qty, price=0, order_type="01", us=True)

    # ── 미지원(토스 미제공) — 빈 응답 대신 명확히 ──
    def get_minute_ohlcv(self, stock_code: str, minutes: int = 60):
        return self._candles(stock_code, "m", minutes)

    def get_orderbook(self, *a, **k):
        return None  # 호가창 미지원 — 호출부는 None 을 허용한다
