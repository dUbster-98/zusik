from __future__ import annotations
"""메신저 추상화 — Discord 외 Telegram/Slack 등 다른 메신저에서도 동일 알림 사용.

구조:
  BaseTextNotifier  notify_* 를 plain text 로 렌더해 _send(text) 로 보낸다.
                    백엔드는 _send 만 구현. 누락 notify_* 는 __getattr__ no-op (안전망).
  TelegramNotifier  Bot API sendMessage. 선택적으로 getUpdates 폴링 → commands.json.
  SlackNotifier     Incoming Webhook 전송.
  MultiNotifier     여러 백엔드로 fan-out (Discord + Telegram + Slack 동시 발송).

알림 실패는 절대 매매를 막지 않는다 — 모든 전송은 try/except 로 격리.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> bool:
    """의존성 추가 없이 JSON POST (requests 있으면 사용, 없으면 urllib)."""
    try:
        import requests
        requests.post(url, json=payload, timeout=timeout)
        return True
    except Exception:
        try:
            import json
            import urllib.request
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=timeout)
            return True
        except Exception as e:
            logger.debug("POST 실패 %s: %s", url, e)
            return False


class BaseTextNotifier:
    """메신저 백엔드 공통 베이스 — notify_* 를 텍스트로 렌더해 _send 로 보낸다.

    notify_* 시그니처는 DiscordNotifier 와 호환(+ **kw 방어). 호출측 변경 불필요.
    누락 메서드는 __getattr__ 가 no-op 으로 흡수(시그니처 불일치로 매매가 멈추지 않게).
    """

    name = "text"

    def _send(self, text: str) -> None:  # pragma: no cover - 백엔드가 구현
        raise NotImplementedError

    def _safe_send(self, text: str) -> None:
        try:
            if text:
                self._send(text[:3500])
        except Exception as e:
            logger.debug("%s 전송 실패: %s", self.name, e)

    # ── 알림 렌더링 ──
    def notify_trade(self, side="", stock_name="", stock_code="", qty=0, price=0,
                     reason="", is_long_term=False, long_term_reason="",
                     realized_pnl=0, realized_rate=0, **kw):
        label = {"buy": "매수", "long_term_buy": "장기매수", "sell": "매도"}.get(side, side or "거래")
        msg = f"[{label}] {stock_name}({stock_code}) {qty}주 @ {price:,}"
        if realized_pnl:
            msg += f" | 손익 {realized_pnl:+,}원({realized_rate:+.1f}%)"
        if reason:
            msg += f"\n근거: {str(reason)[:1200]}"
        if is_long_term and long_term_reason:
            msg += f"\n장기투자: {str(long_term_reason)[:400]}"
        self._safe_send(msg)

    def notify_market_open(self, stocks=None, market_info="", **kw):
        stocks = stocks or []
        lines = ["[장 시작] 감시 종목"]
        if market_info:
            lines.append(str(market_info)[:800])
        for s in stocks[:15]:
            lines.append(f"- {s.get('name', s.get('code', ''))} ({s.get('code', '')})")
        self._safe_send("\n".join(lines))

    def notify_daily_report(self, *a, **kw):
        text = kw.get("report") or kw.get("text") or (a[0] if a else "")
        self._safe_send(f"[일일 리포트]\n{str(text)[:2500]}" if text else "[일일 리포트]")

    def notify_error(self, message="", **kw):
        self._safe_send(f"[오류] {message}")

    def notify_emergency_hold(self, reason="", **kw):
        self._safe_send(f"[긴급 홀딩] {reason}")

    def notify_emergency_release(self, *a, **kw):
        self._safe_send("긴급 홀딩 해제")

    def notify_strategy_switch(self, old_strategy="", new_strategy="", reason="", **kw):
        old = old_strategy or kw.get("old", "")
        new = new_strategy or kw.get("new", "")
        self._safe_send(f"전략 전환: {old} -> {new}\n{reason}")

    def notify_stock_danger(self, stock_name="", stock_code="", danger_level="",
                            reasons=None, action="", **kw):
        rs = ", ".join(reasons) if isinstance(reasons, (list, tuple)) else (reasons or "")
        self._safe_send(f"[위험감지:{danger_level}] {stock_name}({stock_code}) -> {action}\n{rs}")

    def notify_forced_stop_loss(self, stock_name="", stock_code="", loss_rate=0.0, qty=0, **kw):
        self._safe_send(f"[강제 손절] {stock_name}({stock_code}) {qty}주 ({loss_rate:+.1f}%)")

    def notify_daily_target_reached(self, realized_pnl=0, realized_rate=0.0, target_rate=0.0, **kw):
        pnl = realized_pnl or kw.get("pnl", 0)
        rate = realized_rate or kw.get("rate", 0.0)
        self._safe_send(f"일일 목표 도달: {pnl:+,}원 ({rate:+.2f}% / 목표 {target_rate:.2f}%)")

    def notify_mode_upgrade(self, old_mode="", new_mode="", total_asset=0, tier=None, **kw):
        self._safe_send(f"모드 변경: {old_mode} -> {new_mode} (총자산 {total_asset:,}원)")

    def notify_stock_rotation(self, changes="", kr_list="", us_list="", **kw):
        self._safe_send(f"종목 교체:\n{changes}")

    def notify_pattern_report(self, date="", stats=None, total_pnl=0, market="", **kw):
        stats = stats or {}
        if not stats:
            return
        tag = f"{market} " if market else ""
        lines = [f"[{tag}매도 패턴 리포트 {date}] 합계 {total_pnl:+,}원"]
        for pat, s in sorted(stats.items(), key=lambda x: -x[1].get("pnl_sum", 0))[:8]:
            lines.append(f"- {pat}: {s.get('count', 0)}건 / 승률 {s.get('win_rate', 0):.0f}% / "
                         f"총 {s.get('pnl_sum', 0):+,}원")
        self._safe_send("\n".join(lines))

    def notify_effective_pnl(self, date="", summary=None, **kw):
        s = summary or {}
        if not s:
            return
        self._safe_send(
            f"[실효 수익 {date}]\n"
            f"실현 누적: {s.get('realized_total', 0):+,}원\n"
            f"미실현: {s.get('unrealized_krw', 0):+,}원\n"
            f"실효 순수익: {s.get('effective_total', 0):+,}원\n"
            f"총자산 {s.get('total_equity_now', 0):,}원 / 입금 {s.get('total_deposits', 0):,}원"
        )

    def notify_monthly_report(self, stats=None, **kw):
        s = stats or {}
        if not s or s.get("days", 0) == 0:
            return
        from zusik.reporting.monthly_text import format_monthly_report
        self._safe_send(format_monthly_report(s))

    def notify_monthly_html_ready(self, stats=None, path="", **kw):
        s = stats or {}
        if not s or s.get("days", 0) == 0:
            return
        self._safe_send(
            f"[{s.get('month', '')} 월간 HTML 리포트 저장됨]\n"
            f"수익률 {s.get('return_pct', 0):+.2f}% / 실현 {s.get('realized', 0):+,}원 / "
            f"최대DD {s.get('max_drawdown', 0):+.2f}%\n"
            f"파일: {path}"
        )

    def notify_update_available(self, commit_hash="", msg="", author="", when="",
                                files="", count=0, **kw):
        self._safe_send(
            f"[새 버전 푸시됨] {commit_hash} {msg}\n"
            f"작성자 {author} · {when}\n"
            f"변경: {files}\n"
            f"적용하려면 '업데이트' 명령을 보내세요 (git pull + 재시작). 무시하면 현재 버전 유지."
        )

    def __getattr__(self, name):
        # 미구현 notify_* 는 조용히 no-op (시그니처 불일치로 매매가 멈추지 않게)
        if name.startswith("notify_") or name.startswith("send"):
            return lambda *a, **k: None
        raise AttributeError(name)


class TelegramNotifier(BaseTextNotifier):
    """Telegram Bot API 백엔드. 선택적으로 명령 폴링(getUpdates -> commands.json)."""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._api = f"https://api.telegram.org/bot{bot_token}"
        self._poll_thread = None

    def _send(self, text: str) -> None:
        _post_json(f"{self._api}/sendMessage",
                   {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True})

    def start_command_polling(self, queue_fn) -> None:
        """getUpdates 롱폴링 → 받은 텍스트를 queue_fn(cmd) 로 commands.json 큐에 적재.

        응답은 기존 명령 처리기가 notifier(MultiNotifier) 로 발송하므로 Telegram 에도 도달한다.
        """
        if self._poll_thread:
            return

        def _loop():
            import requests
            offset = None
            while True:
                try:
                    params = {"timeout": 50}
                    if offset is not None:
                        params["offset"] = offset
                    r = requests.get(f"{self._api}/getUpdates", params=params, timeout=60)
                    for upd in r.json().get("result", []):
                        offset = upd["update_id"] + 1
                        msg = upd.get("message") or upd.get("channel_post") or {}
                        text = (msg.get("text") or "").strip()
                        if text and str(msg.get("chat", {}).get("id")) == self._chat_id:
                            cmd = text.lstrip("/").strip()
                            if cmd:
                                queue_fn(cmd)
                except Exception as e:
                    logger.debug("telegram 폴링 오류: %s", e)
                    time.sleep(5)

        self._poll_thread = threading.Thread(target=_loop, daemon=True)
        self._poll_thread.start()
        logger.info("Telegram 명령 폴링 시작")


class SlackNotifier(BaseTextNotifier):
    """Slack 백엔드. Incoming Webhook 으로 알림 발송 + 선택적으로 Socket Mode 명령 수신.

    명령 수신은 app-level token(SLACK_APP_TOKEN, xapp-, scope connections:write)이 있을 때만
    동작한다. 공개 URL 없이 아웃바운드 WSS 로 슬래시 명령과 DM 을 받아 commands.json 큐에
    적재한다(Telegram getUpdates 폴링의 Slack 대응물). websocket-client 는 KIS WS 와 공용
    의존성이라 새 패키지가 필요 없다.
    """

    name = "slack"

    def __init__(self, webhook_url: str, app_token: str = ""):
        self._url = webhook_url
        self._app_token = app_token
        self._poll_thread = None

    def _send(self, text: str) -> None:
        _post_json(self._url, {"text": text})

    @staticmethod
    def _parse_socket_envelope(env):
        """Socket Mode 엔벨로프 → (envelope_id, cmd).

        cmd 가 없으면 빈 문자열. 봇 자신이 보낸 메시지(bot_id)나 시스템 subtype 은 무시해
        명령 루프를 방지한다. 슬래시 명령은 인자(text) 우선, 없으면 명령어 자체를 쓴다.
        """
        etype = env.get("type")
        eid = env.get("envelope_id")
        payload = env.get("payload") or {}
        cmd = ""
        if etype == "slash_commands":
            cmd = (payload.get("text") or "").strip()
            if not cmd:
                cmd = (payload.get("command") or "").lstrip("/").strip()
        elif etype == "events_api":
            ev = payload.get("event") or {}
            if ev.get("type") == "message" and not ev.get("bot_id") and not ev.get("subtype"):
                cmd = (ev.get("text") or "").strip()
        return eid, cmd.lstrip("/").strip()

    def start_command_polling(self, queue_fn) -> None:
        """Socket Mode(WSS)로 명령 수신 → queue_fn(cmd) 로 commands.json 큐에 적재.

        SLACK_APP_TOKEN 이 없으면 조용히 미동작(알림 전용으로 유지). 응답은 기존 명령
        처리기가 notifier(MultiNotifier)로 발송하므로 Slack webhook 에도 도달한다.
        """
        if self._poll_thread or not self._app_token:
            return

        def _loop():
            import json as _json
            import requests
            from websocket import create_connection
            while True:
                try:
                    r = requests.post(
                        "https://slack.com/api/apps.connections.open",
                        headers={"Authorization": f"Bearer {self._app_token}"}, timeout=15)
                    j = r.json()
                    if not j.get("ok"):
                        logger.warning("Slack Socket Mode 연결 실패: %s", j.get("error"))
                        time.sleep(30)
                        continue
                    ws = create_connection(j["url"], timeout=70)
                    while True:
                        raw = ws.recv()
                        if not raw:
                            continue
                        env = _json.loads(raw)
                        if env.get("type") == "disconnect":
                            break  # Slack 이 소켓 갱신 요청 → 바깥 루프가 재연결
                        eid, cmd = self._parse_socket_envelope(env)
                        if eid:
                            ws.send(_json.dumps({"envelope_id": eid}))  # 3초 내 ACK 필수
                        if cmd:
                            queue_fn(cmd)
                    ws.close()
                except Exception as e:
                    logger.debug("Slack Socket Mode 오류: %s", e)
                    time.sleep(5)

        self._poll_thread = threading.Thread(target=_loop, daemon=True)
        self._poll_thread.start()
        logger.info("Slack 명령 수신(Socket Mode) 시작")


class MultiNotifier:
    """여러 백엔드로 fan-out. 어떤 notify_*/send_* 호출이든 모든 백엔드에 방어적으로 전달."""

    def __init__(self, backends):
        self._backends = [b for b in backends if b is not None]

    def add(self, backend):
        if backend is not None:
            self._backends.append(backend)

    def __bool__(self):
        return bool(self._backends)

    def __getattr__(self, name):
        def fanout(*a, **k):
            for b in self._backends:
                try:
                    getattr(b, name)(*a, **k)
                except Exception as e:
                    logger.debug("%s.%s 실패: %s", type(b).__name__, name, e)
        return fanout
