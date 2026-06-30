from __future__ import annotations
"""안정성 & 성능 모듈.

1. API 재시도 — 실패 시 자동 재시도 (지수 백오프)
2. 타임아웃 관리 — 응답 없으면 빠르게 포기
3. 병렬 처리 — 여러 종목 동시 분석
4. 네트워크 장애 복구 — 연결 끊김 자동 재연결
5. 데이터 무결성 — JSON 깨짐 방지, 파일 잠금
6. 주문 이중 체크 — 중복 주문 방지, 미체결 관리
"""

import json
import logging
import os
import time
import functools
try:
    import fcntl  # Unix 전용 — JSON 파일 advisory 락(flock)
except ImportError:  # Windows 등: 락 미지원 → no-op shim (운영은 Linux systemd, dev/CI만 해당)
    class _NoFcntl:
        LOCK_SH = LOCK_EX = LOCK_UN = 0
        @staticmethod
        def flock(*_args, **_kwargs):
            return None
    fcntl = _NoFcntl()  # type: ignore
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ══════════════════════════════════════
# 1. API 재시도 데코레이터
# ══════════════════════════════════════

def retry(max_retries: int = 3, backoff: float = 1.0, exceptions: tuple = (Exception,)):
    """API 호출 재시도 데코레이터.

    실패 시 지수 백오프로 재시도: 1초 → 2초 → 4초
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_err = e
                    if attempt < max_retries:
                        wait = backoff * (2 ** attempt)
                        logger.warning(
                            "%s 실패 (%d/%d), %s초 후 재시도: %s",
                            func.__name__, attempt + 1, max_retries, wait, str(e)[:80]
                        )
                        time.sleep(wait)
                    else:
                        logger.error("%s 최종 실패 (%d회 시도): %s", func.__name__, max_retries + 1, e)
            raise last_err
        return wrapper
    return decorator


def log_exceptions(default_return=None):
    """함수 예외를 자동으로 로그 + default 반환 (raise X).

    키움 REST API 예제(20250605 youtube)의 패턴 도입. try-except 보일러플레이트
    제거 + 일관된 로깅. retry 데코레이터와 달리 재시도 안 함 (조용한 폴백).

    사용 예:
        @log_exceptions(default_return={})
        def get_balance():
            return self._call_api(...)
        # 예외 발생 시: logger.exception 후 {} 반환

        @log_exceptions(default_return=None)
        def get_ohlcv(code):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                logger.exception("Exception in %s", func.__qualname__)
                return default_return
        return wrapper
    return decorator


def http_error_with_body(response):
    """HTTPError에 response.text를 포함시켜 디버그 용이하게.

    키움 예제 utils.py 패턴 — KIS API도 에러 본문이 중요한 정보 (rate limit,
    잘못된 종목코드 등). raise_for_status만 쓰면 본문이 사라져 디버그 어려움.

    사용 예:
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise type(e)(http_error_with_body(response)) from e
    """
    return f"HTTP {response.status_code}: {response.text[:500]}"


# ══════════════════════════════════════
# 2. 안전한 JSON 파일 읽기/쓰기
# ══════════════════════════════════════

def safe_json_load(path: str, default=None):
    """JSON 로드 — 깨져있으면 백업 복구."""
    if default is None:
        default = {}

    if not os.path.exists(path):
        return default

    try:
        with open(path, encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            return data
    except json.JSONDecodeError:
        # 파일 깨짐 → 백업 시도
        backup = path + ".bak"
        if os.path.exists(backup):
            logger.warning("JSON 깨짐, 백업에서 복구: %s", path)
            try:
                with open(backup, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        logger.error("JSON 복구 실패, 기본값 사용: %s", path)
        return default
    except Exception as e:
        logger.error("JSON 로드 실패: %s — %s", path, e)
        return default


def safe_json_save(path: str, data):
    """JSON 저장 — 원자적 쓰기 (깨짐 방지)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # 현재 파일을 백업
    if os.path.exists(path):
        backup = path + ".bak"
        try:
            os.replace(path, backup)
        except Exception:
            pass

    # 임시 파일에 먼저 쓰고 rename (원자적)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp, path)
    except Exception as e:
        logger.error("JSON 저장 실패: %s — %s", path, e)
        # 백업 복구
        backup = path + ".bak"
        if os.path.exists(backup):
            try:
                os.replace(backup, path)
            except Exception:
                pass


# ══════════════════════════════════════
# 3. 주문 안전 관리
# ══════════════════════════════════════

class OrderGuard:
    """주문 이중 체크 — 중복 주문/미체결 관리.

    키움 ch8 패턴 추가:
      - `record_order`가 `order_type`(market/limit) + `is_open` 플래그로
        미체결 추적 가능하도록 확장.
      - `get_stale_orders(timeout_sec)`로 정정 대상 주문 반환.
      - `mark_filled` / `mark_amended` / `mark_canceled` 로 라이프사이클 갱신.
    기존 시그니처는 호환 유지 (시장가 위주 현 코드 영향 없음).
    """

    ORDERS_FILE = os.path.join("data", "pending_orders.json")

    def __init__(self):
        self._pending = safe_json_load(self.ORDERS_FILE, default={"orders": []})
        self._cooldowns: dict[str, float] = {}  # 종목별 마지막 주문 시각

    def can_order(self, code: str, side: str, cooldown_sec: int = 30) -> bool:
        """주문 가능 여부 — 쿨다운 + 중복 방지.

        같은 종목 같은 방향 주문은 30초 이내 중복 불가.
        """
        key = f"{code}:{side}"
        now = time.time()

        last = self._cooldowns.get(key, 0)
        if now - last < cooldown_sec:
            logger.warning("주문 쿨다운: %s %s — %d초 대기", code, side, cooldown_sec - int(now - last))
            return False

        return True

    def record_order(self, code: str, side: str, qty: int, price: int,
                     order_no: str = "", order_type: str = "market",
                     market: str = "KR"):
        """주문 기록.

        order_type: "market" (시장가, 즉시 체결 가정) / "limit" (지정가, 미체결 추적)
        market: "KR" / "US"
        """
        key = f"{code}:{side}"
        self._cooldowns[key] = time.time()

        # 미체결 추적은 지정가만. 시장가는 즉시 체결로 보고 is_open=False 기록.
        is_open = order_type == "limit" and bool(order_no)
        self._pending["orders"].append({
            "code": code,
            "side": side,
            "qty": qty,
            "price": price,
            "order_no": order_no,
            "order_type": order_type,
            "market": market,
            "is_open": is_open,
            "timestamp": datetime.now().isoformat(),
        })
        # 최근 100건만 유지
        self._pending["orders"] = self._pending["orders"][-100:]
        safe_json_save(self.ORDERS_FILE, self._pending)

    # ── 미체결 추적 (키움 ch8 amend 패턴) ──

    def get_open_orders(self) -> list:
        """is_open=True인 지정가 주문 전체."""
        return [o for o in self._pending.get("orders", []) if o.get("is_open")]

    def get_stale_orders(self, timeout_sec: int = 60) -> list:
        """timeout_sec 초과 미체결 주문 → 정정 후보."""
        cutoff = datetime.now() - timedelta(seconds=timeout_sec)
        out = []
        for o in self.get_open_orders():
            try:
                if datetime.fromisoformat(o["timestamp"]) <= cutoff:
                    out.append(o)
            except (ValueError, KeyError):
                continue
        return out

    def _update_order(self, order_no: str, **changes) -> bool:
        if not order_no:
            return False
        changed = False
        for o in self._pending.get("orders", []):
            if o.get("order_no") == order_no:
                o.update(changes)
                changed = True
                break
        if changed:
            safe_json_save(self.ORDERS_FILE, self._pending)
        return changed

    def mark_filled(self, order_no: str):
        """체결 완료로 표시 — 더 이상 정정 대상 아님."""
        self._update_order(order_no, is_open=False, closed_reason="filled",
                           closed_at=datetime.now().isoformat())

    def mark_canceled(self, order_no: str):
        """취소됨."""
        self._update_order(order_no, is_open=False, closed_reason="canceled",
                           closed_at=datetime.now().isoformat())

    def mark_amended(self, order_no: str, new_order_no: str = ""):
        """정정 완료 — 원주문은 닫고 새 주문번호 매핑 기록."""
        self._update_order(order_no, is_open=False, closed_reason="amended",
                           closed_at=datetime.now().isoformat(),
                           amended_to=new_order_no)


# ══════════════════════════════════════
# 4. 네트워크 상태 모니터
# ══════════════════════════════════════

class NetworkMonitor:
    """네트워크 연결 상태 추적."""

    def __init__(self):
        self._failures: list[float] = []
        self._last_success: float = time.time()

    def record_success(self):
        self._last_success = time.time()
        self._failures.clear()

    def record_failure(self):
        self._failures.append(time.time())
        # 최근 5분 실패만 유지
        cutoff = time.time() - 300
        self._failures = [t for t in self._failures if t > cutoff]

    def is_healthy(self) -> bool:
        """최근 5분간 실패 5회 미만이면 건강."""
        return len(self._failures) < 5

    def should_pause(self) -> bool:
        """연속 실패 시 잠시 멈춤."""
        if len(self._failures) >= 3:
            # 마지막 3회 실패가 30초 이내면 네트워크 문제
            if self._failures[-1] - self._failures[-3] < 30:
                return True
        return False

    def get_status(self) -> dict:
        return {
            "healthy": self.is_healthy(),
            "recent_failures": len(self._failures),
            "seconds_since_success": int(time.time() - self._last_success),
        }


# ══════════════════════════════════════
# 5. 주문 관문 안전 검증 (변조/조작 방어)
# ══════════════════════════════════════

class OrderSafetyValidator:
    """주문 전송 직전 관문 검증 — 변조된 상위 함수가 만든 악성/버그 주문을 차단.

    `KISClient._order` / `_us_order`(모든 주문이 통과하는 단일 관문)에서 호출한다.
    전략·사이징·매매 계층이 악의적으로 변형(악성코드 삽입/의도적 버그)되어도, 주문은
    반드시 이 관문을 지나므로 다음 **주식조작·계좌탈취 패턴**을 구조적으로 막는다:

      - 초과/유령 매도 (held_qty 초과)         → 보유 초과 매도 차단
      - 워시트레이딩 (반대방향 즉시 반복)        → 최소 간격 강제
      - 스푸핑/조작가 지정가 (시장가 ±30% 밖)   → 비정상 가격 거부
      - 계좌 드레인/과대 매수 (현금 초과 notional) → 현금 한도 강제
      - 인젝션 과대수량 / 비정상 코드·수량       → 상한·형식 검증

    fail-closed: 위반이거나 검증 자체가 예외면 주문을 **거부**한다(변조로 우회 방어).
    순수 함수 — 절대 예외를 밖으로 던지지 않는다(어떤 입력에도 (bool, str) 반환).
    """

    MAX_QTY = 5_000_000              # 절대 수량 상한 (injection/fat-finger)
    MAX_PRICE_DEVIATION = 0.30       # 지정가가 시장가 ±30% 밖 → 스푸핑/조작가
    WASH_WINDOW_SEC = 30             # 반대방향 주문 최소 간격 (워시트레이딩)
    CASH_TOLERANCE = 1.02            # 매수 notional ≤ 주문가능현금 × 1.02 (수수료 여유)

    def validate(self, *, side, code, qty, price=0, order_type="01",
                 held_qty=None, orderable_cash=None, market_price=0.0,
                 last_opposite_ts=0.0, now=None) -> tuple:
        """(ok: bool, reason: str). 미지의 컨텍스트(None)는 해당 검사만 건너뛴다."""
        try:
            now = now if now is not None else time.time()
            s = (side or "").lower()
            if s not in ("buy", "sell"):
                return False, f"비정상 side: {side!r}"
            # 1) 종목코드 형식 — 인젝션/비정상 심볼 방어
            if not isinstance(code, str) or not (0 < len(code) <= 12) or not code.strip():
                return False, f"비정상 종목코드: {code!r}"
            # 2) 수량 — 양의 정수 + 상한 (인젝션/fat-finger)
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                return False, f"비정상 수량: {qty!r}"
            if qty > self.MAX_QTY:
                return False, f"수량 상한 초과: {qty:,} > {self.MAX_QTY:,} (injection 의심)"
            # 3) 가격 — 음수 금지
            if price is None or (isinstance(price, (int, float)) and price < 0):
                return False, f"비정상 가격: {price!r}"
            # 4) 초과/유령 매도 차단
            if s == "sell" and held_qty is not None and qty > held_qty:
                return False, f"초과 매도 차단: {qty:,} > 보유 {held_qty:,} (유령/조작 매도)"
            # 5) 워시트레이딩 — 반대방향 주문 직후 차단
            if last_opposite_ts and 0 <= (now - last_opposite_ts) < self.WASH_WINDOW_SEC:
                return False, (f"워시트레이딩 의심: 반대방향 주문 {now - last_opposite_ts:.0f}s 전 "
                               f"(< {self.WASH_WINDOW_SEC}s)")
            # 6) 지정가 스푸핑/조작가 — 시장가 대비 ±30% 밖
            if price and price > 0 and market_price and market_price > 0:
                dev = abs(price - market_price) / market_price
                if dev > self.MAX_PRICE_DEVIATION:
                    return False, (f"비정상 지정가: 시장가 {market_price:,.2f} 대비 "
                                   f"{dev * 100:.0f}% 이탈 (스푸핑/조작 의심)")
            # 7) 매수 notional ≤ 주문가능현금 (드레인/과대 매수)
            if s == "buy" and orderable_cash is not None and orderable_cash >= 0:
                ref = price if (price and price > 0) else market_price
                if ref and ref > 0 and qty * ref > orderable_cash * self.CASH_TOLERANCE:
                    return False, (f"주문가능현금 초과: {qty * ref:,.0f} > 현금 "
                                   f"{orderable_cash:,.0f} (드레인/과대 매수 의심)")
            return True, "ok"
        except Exception as e:  # noqa: BLE001 — fail-closed: 검증 예외도 차단
            return False, f"검증 예외 — 안전차단(fail-closed): {e}"

    def validate_amend(self, *, code, order_no, qty, price=0, order_type="00") -> tuple:
        """정정(amend) 주문 구조 검증 — 일반 주문과 같은 단일 관문에서. (ok, reason).

        정정도 별도 endpoint로 나가므로 `validate`와 같은 코드/수량/상한 규칙을 재사용해
        오염된 pending_orders가 만든 비정상 정정(과대수량·음수가격·인젝션 코드)을 막는다.
        취소(cancel)는 리스크를 줄이는 동작이라 호출 측에서 이 검증을 건너뛴다.
        """
        try:
            if not order_no or not str(order_no).strip():
                return False, f"원주문번호 없음: {order_no!r}"
            if not isinstance(code, str) or not (0 < len(code) <= 12) or not code.strip():
                return False, f"비정상 종목코드: {code!r}"
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                return False, f"비정상 수량: {qty!r}"
            if qty > self.MAX_QTY:
                return False, f"수량 상한 초과: {qty:,} > {self.MAX_QTY:,} (injection 의심)"
            # 지정가 정정("00")은 양수 가격 필요, 시장가 정정("01")은 price=0 허용.
            if order_type == "00":
                if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
                    return False, f"지정가 정정 비정상 가격: {price!r}"
            elif price is None or (isinstance(price, (int, float)) and price < 0):
                return False, f"비정상 가격: {price!r}"
            return True, "ok"
        except Exception as e:  # noqa: BLE001 — fail-closed
            return False, f"검증 예외 — 안전차단(fail-closed): {e}"


def verify_pnl_invariants(*, trades, deposits, latest_snapshot=None,
                          positions=None, tol: float = 0.05) -> list:
    """매 사이클(1분 tick) 손익/자산 무결성 점검 — 위반 사항 목록 반환(없으면 빈 리스트).

    버그·상태파일 변조·정산 드리프트를 조기 포착하는 invariant 검사. 순수·무예외(어떤
    garbage 입력도 list 반환). 산술만 — API/네트워크 호출 없음(매 tick 호출해도 비용 0).
    검사:
      1) 거래 실현손익 비정상값(NaN/inf)
      2) 포지션 sanity (음수 수량, 보유 중 평단 0, NaN/inf)
      3) 무차입 불변: 누적 실현손실이 총입금을 초과할 수 없음
      4) 자산 정합: effective_equity ≈ 입금 + 실현(스냅샷일까지) + 미실현 (tol 초과 시 경고)
    """
    import math

    def _num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) \
            and not math.isnan(x) and not math.isinf(x)

    issues: list = []
    try:
        # 컨테이너 타입이 어긋나면(테스트 Mock·손상 상태) 값-무결성 위반이 아니라 빈 입력으로 본다.
        if not isinstance(trades, (list, tuple)):
            trades = []
        if not isinstance(positions, dict):
            positions = {}
        sells = [t for t in trades if isinstance(t, dict) and t.get("type") == "sell"]
        realized_total = 0.0
        for t in sells:
            p = t.get("realized_pnl")
            if p is None:
                continue
            if not _num(p):
                issues.append(f"실현손익 비정상값({t.get('code') or t.get('ticker')}): {p!r}")
                continue
            realized_total += p

        for code, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            q = pos.get("qty", 0) or 0
            ap = pos.get("avg_price", 0) or 0
            if _num(q) and q < 0:
                issues.append(f"음수 보유수량: {code} qty={q}")
            if _num(q) and q > 0 and _num(ap) and ap <= 0:
                issues.append(f"보유 중 평단 0 이하: {code}")
            for k in ("qty", "avg_price", "peak_profit_rate", "high_since_buy"):
                v = pos.get(k)
                if v is not None and not _num(v):
                    issues.append(f"포지션 비정상값 {code}.{k}={v!r}")

        if _num(deposits) and deposits > 0 and realized_total < -deposits:
            issues.append(f"실현손실이 총입금 초과(무차입 불가): 실현 {realized_total:,.0f} "
                          f"< -입금 {deposits:,.0f}")

        if isinstance(latest_snapshot, dict):
            eff = latest_snapshot.get("effective_equity")
            if _num(eff) and _num(deposits) and deposits > 0:
                sdate = str(latest_snapshot.get("date", ""))
                realized_to = sum((t.get("realized_pnl") or 0) for t in sells
                                  if _num(t.get("realized_pnl")) and str(t.get("date", "")) <= sdate)
                unreal = latest_snapshot.get("unrealized_krw", 0) or 0
                expected = deposits + realized_to + (unreal if _num(unreal) else 0)
                if expected > 0 and abs(eff - expected) / expected > tol:
                    issues.append(
                        f"자산 정합 불일치: effective {eff:,.0f} ≠ 입금+실현+평가 {expected:,.0f} "
                        f"({(eff - expected) / expected * 100:+.1f}%)")
    except Exception as e:  # noqa: BLE001 — 무예외(검증이 매매를 깨면 안 됨)
        issues.append(f"무결성 점검 예외: {e}")
    return issues
