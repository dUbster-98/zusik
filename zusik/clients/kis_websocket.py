from __future__ import annotations
"""KIS 실시간 시세 WebSocket — extreme tier 보유 종목용 틱 스트림.

KIS API 문서 기반:
  - approval_key 발급: POST /oauth2/Approval
  - WebSocket: ws://ops.koreainvestment.com:21000 (실전) / 31000 (모의)
  - TR_ID: H0STCNT0 (국내 체결가), HDFSCNT0 (해외 체결가)
  - 메시지 포맷:
      JSON: {"header": {...}, "body": {...}} (구독 응답, PINGPONG)
      Raw:  "0|H0STCNT0|001|005930^090000^57000^..." (체결 데이터)

5/1 진행:
  - 실 연결 + 자동 재연결 + ping/pong
  - 단일 종목 다수 구독 (H0STCNT0/HDFSCNT0)
  - 메시지 파싱 → callback({"code", "price", "ts"})
"""

import json
import logging
import threading
import time
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

try:
    import websocket  # pip install websocket-client
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False
    logger.warning("websocket-client 미설치 — WebSocket 비활성")


class KISWebSocketManager:
    """KIS 실시간 시세 매니저. 자동 재연결 + 구독 큐.

    extreme tier 보유 종목만 구독 권장 (KIS WebSocket 종목 한도 ~40종).
    KO/AAPL 같은 안정 종목은 구독 안 함 → 분봉 폴백.
    """

    URL_REAL = "ws://ops.koreainvestment.com:21000"
    URL_VIRTUAL = "ws://ops.koreainvestment.com:31000"

    # 해외 실시간 거래소 코드 정규화 (주문/시세용 별칭 → WS용 3자리)
    _US_EXCD = {
        "NASD": "NAS", "NASDAQ": "NAS", "NAS": "NAS",
        "NYSE": "NYS", "NYS": "NYS",
        "AMEX": "AMS", "AMS": "AMS",
    }

    def __init__(self, app_key: str, app_secret: str, is_virtual: bool = False):
        self.app_key = app_key
        self.app_secret = app_secret
        self.is_virtual = is_virtual
        self._approval_key: Optional[str] = None
        # code → {"callback": fn, "market": "KR"|"US"}
        self._subscriptions: dict[str, dict] = {}
        self._connected = False
        self._stop_flag = False
        self._lock = threading.Lock()
        self._ws: Optional["websocket.WebSocketApp"] = None
        self._ws_thread: Optional[threading.Thread] = None

    @property
    def is_active(self) -> bool:
        return self._connected and bool(self._approval_key)

    def get_approval_key(self) -> Optional[str]:
        """KIS WebSocket 인증 키 발급 (REST 호출)."""
        if self._approval_key:
            return self._approval_key
        host = ("https://openapivts.koreainvestment.com:29443"
                if self.is_virtual else
                "https://openapi.koreainvestment.com:9443")
        url = f"{host}/oauth2/Approval"
        try:
            resp = requests.post(
                url,
                headers={"content-type": "application/json"},
                data=json.dumps({
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "secretkey": self.app_secret,
                }),
                timeout=10,
            )
            if resp.status_code == 200:
                self._approval_key = resp.json().get("approval_key")
                logger.info("KIS WebSocket approval_key 발급 OK")
                return self._approval_key
            logger.warning("WebSocket approval_key 발급 실패: %s", resp.text[:120])
        except Exception as e:
            logger.warning("WebSocket approval 오류: %s", e)
        return None

    def start(self) -> bool:
        """WebSocket 연결 시작."""
        if not _WS_AVAILABLE:
            logger.warning("WebSocket 비활성 (websocket-client 미설치)")
            return False
        if not self.get_approval_key():
            return False
        if self._ws_thread and self._ws_thread.is_alive():
            return True
        self._stop_flag = False
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()
        return True

    def stop(self):
        self._stop_flag = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        with self._lock:
            self._subscriptions.clear()

    def _ws_loop(self):
        """연결 루프 — 연결 끊기면 5초 후 재시도."""
        url = self.URL_VIRTUAL if self.is_virtual else self.URL_REAL
        while not self._stop_flag:
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                    on_ping=lambda *a, **k: None,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.warning("WS 루프 오류: %s", e)
            self._connected = False
            if not self._stop_flag:
                logger.info("WebSocket 재연결 5초 대기...")
                time.sleep(5)

    def _on_open(self, ws):
        self._connected = True
        logger.info("KIS WebSocket 연결 OK")
        # 기존 구독 재등록
        with self._lock:
            subs = list(self._subscriptions.items())
        for code, info in subs:
            self._send_subscribe(code, info.get("market", "KR"),
                                 info.get("exchange", "NAS"))

    def _on_close(self, ws, status_code=None, msg=None):
        # websocket-client 버전에 따라 status_code/msg가 누락된 채 호출됨 (~0.59 호환).
        # 기본값을 두어 양쪽 시그니처 모두 수용.
        self._connected = False
        logger.info("KIS WebSocket 종료 (%s)", status_code)

    def _on_error(self, ws, err):
        logger.warning("KIS WebSocket 에러: %s", err)

    def _send_subscribe(self, code: str, market: str = "KR", exchange: str = "NAS"):
        if not self._ws or not self._connected:
            return
        tr_id = "H0STCNT0" if market == "KR" else "HDFSCNT0"
        # 해외(HDFSCNT0)는 tr_key 가 'D'+거래소(NAS/NYS/AMS)+종목 형식이어야 한다.
        # 바닥 티커만 보내면 KIS 가 구독 ACK(rt_cd=0)는 주지만 체결 틱을 전혀 안 흘려보낸다.
        if market == "KR":
            tr_key = code
        else:
            excd = self._US_EXCD.get((exchange or "NAS").upper(), "NAS")
            tr_key = f"D{excd}{code}"
        msg = json.dumps({
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",  # 1=등록, 2=해제
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": tr_key,
                }
            }
        })
        try:
            self._ws.send(msg)
            logger.debug("WS 구독 전송: %s/%s (tr_key=%s)", market, code, tr_key)
        except Exception as e:
            logger.debug("WS 구독 전송 실패 %s: %s", code, e)

    def _on_message(self, ws, raw):
        """KIS 메시지 파싱.

        - JSON: 구독 응답, PINGPONG (무시)
        - Raw: '0|TR_ID|N|data1^data2^...'
        """
        try:
            if not raw:
                return
            if raw.startswith("{"):
                # JSON: 구독 응답 또는 ping
                try:
                    data = json.loads(raw)
                    tr_id = data.get("header", {}).get("tr_id")
                    if tr_id == "PINGPONG":
                        # PONG 응답
                        if self._ws:
                            self._ws.send(raw)
                    elif data.get("body", {}).get("rt_cd") == "0":
                        logger.debug("WS 구독 응답 OK: %s", tr_id)
                except Exception:
                    pass
                return

            # Raw 데이터 메시지
            parts = raw.split("|")
            if len(parts) < 4:
                return
            tr_id = parts[1]
            data = parts[3]
            fields = data.split("^")

            cb_payload = None
            if tr_id == "H0STCNT0" and len(fields) >= 3:
                # 국내 체결가: 종목코드(0) | 체결시각(1) | 현재가(2)
                code = fields[0]
                cb_payload = {
                    "code": code,
                    "price": float(fields[2]) if fields[2] else 0.0,
                    "ts": fields[1],
                    "volume": int(fields[12]) if len(fields) > 12 and fields[12] else 0,
                    "market": "KR",
                }
            elif tr_id == "HDFSCNT0" and len(fields) >= 11:
                # 해외 체결가: RSYM(0)/SYMB(1)/.../LAST(11)
                # KIS HDFSCNT0 필드 순서: RSYM/SYMB/ZDIV/TYMD/XYMD/XHMS/KYMD/KHMS/OPEN/HIGH/LOW/LAST/SIGN/...
                code = fields[1]
                cb_payload = {
                    "code": code,
                    "price": float(fields[11]) if fields[11] else 0.0,
                    "ts": fields[5] if len(fields) > 5 else "",
                    "volume": 0,
                    "market": "US",
                }

            if cb_payload and cb_payload["price"] > 0:
                code = cb_payload["code"]
                with self._lock:
                    info = self._subscriptions.get(code)
                if info and "callback" in info:
                    try:
                        info["callback"](cb_payload)
                    except Exception as e:
                        logger.debug("WS callback 오류 %s: %s", code, e)
        except Exception as e:
            logger.debug("WS 메시지 파싱 오류: %s", e)

    def subscribe(self, code: str, callback: Callable[[dict], None],
                  market: str = "KR", exchange: str = "NAS") -> bool:
        """종목 구독 등록. 이미 연결돼 있으면 즉시 구독 전송.

        해외는 exchange(NAS/NYS/AMS, 또는 NASD/NYSE/AMEX 별칭)로 tr_key 를 구성한다.
        구독 dict 은 바닥 코드(HDFSCNT0 SYMB 필드와 일치)로 키잉해 콜백 매칭을 보장한다.
        """
        with self._lock:
            self._subscriptions[code] = {
                "callback": callback, "market": market, "exchange": exchange}
        if self._connected:
            self._send_subscribe(code, market, exchange)
        return True
