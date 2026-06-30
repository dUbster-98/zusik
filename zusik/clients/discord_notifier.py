from __future__ import annotations
"""Discord Webhook 알림 모듈.

장 시작 전 알림, 매매 체결 알림, 장 마감 후 일일 리포트를 전송.

핵심 원칙:
  - '수익' = 매도 체결로 확정된 실현손익만 표기
  - 보유 중 평가손익은 '미실현(평가)손익'으로 별도 표기
  - 장기투자 종목은 별도 섹션에 사유와 함께 표시
"""

import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


def _split_for_discord(text: str, max_per_chunk: int = 1000) -> list[str]:
    """긴 텍스트를 max_per_chunk 단위로 분할 (제한 없음).

    가능하면 줄 경계 → 어휘 경계 순으로 자르고, 불가하면 강제 분할.
    호출자가 chunks의 처음 N개는 embed field로, 나머지는 후속 메시지로
    분배하는 패턴 (Discord embed 총 6,000 chars 한도 우회).
    """
    if not text:
        return []
    s = str(text)
    if len(s) <= max_per_chunk:
        return [s]
    chunks = []
    cursor = 0
    while cursor < len(s):
        end = cursor + max_per_chunk
        if end >= len(s):
            chunks.append(s[cursor:])
            break
        nl = s.rfind("\n", cursor, end)
        if nl <= cursor:
            sp = s.rfind(" ", cursor, end)
            if sp <= cursor:
                sp = end
            chunks.append(s[cursor:sp])
            cursor = sp + 1
        else:
            chunks.append(s[cursor:nl])
            cursor = nl + 1
    return chunks


def _split_plain_message(text: str, max_per_msg: int = 1900) -> list[str]:
    """Discord 일반 메시지 한도(2000) 안에 맞게 분할 — 후속 content용."""
    if not text:
        return []
    if len(text) <= max_per_msg:
        return [text]
    out = []
    cursor = 0
    while cursor < len(text):
        end = cursor + max_per_msg
        if end >= len(text):
            out.append(text[cursor:])
            break
        nl = text.rfind("\n", cursor, end)
        if nl <= cursor:
            sp = text.rfind(" ", cursor, end)
            if sp <= cursor:
                sp = end
            out.append(text[cursor:sp])
            cursor = sp + 1
        else:
            out.append(text[cursor:nl])
            cursor = nl + 1
    return out


class DiscordNotifier:
    """Discord Webhook 알림."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _send(self, content: str = "", embeds: list | None = None):
        # 1. 실행 중인 Discord Bot이 있다면 Bot Token으로 전송 (우선순위)
        try:
            import zusik.clients.discord_bot as discord_bot
            bot = discord_bot._discord_bot_ref
            if bot and bot._alert_channel:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    bot._send_alert(message=content, embeds=embeds),
                    bot.loop
                )
                return
        except Exception:
            pass

        # 2. Bot이 없으면 Webhook URL로 전송 (Fallback)
        if not self.webhook_url:
            logger.warning("Discord Webhook URL 미설정 (Bot도 비활성)")
            return

        payload = {}
        if content:
            payload["content"] = content
        if embeds:
            payload["embeds"] = embeds
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception:
            logger.warning("Discord 알림 전송 실패", exc_info=True)

    # ── 장 시작 전 알림 ──

    def notify_market_open(self, stocks: list[dict], market_info: str = ""):
        today = datetime.now().strftime("%Y-%m-%d (%a)")
        stock_list = "\n".join(f"  • {s.get('name', s['code'])} (`{s['code']}`)" for s in stocks)

        fields = [
            {"name": "감시 종목", "value": stock_list, "inline": False},
        ]
        if market_info:
            fields.append({"name": "시장 동향", "value": market_info[:1000], "inline": False})

        embed = {
            "title": f"장 시작 알림 — {today}",
            "description": "자동매매 봇이 가동됩니다.",
            "color": 0x2ECC71,
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])
        logger.info("Discord: 장 시작 알림 전송")

    # ── 매매 체결 알림 ──

    def notify_trade(
        self,
        side: str,
        stock_name: str,
        stock_code: str,
        qty: int,
        price: int,
        reason: str = "",
        is_long_term: bool = False,
        long_term_reason: str = "",
        realized_pnl: int | None = None,
        realized_rate: float | None = None,
    ):
        """매매 체결 알림.

        Args:
            side: "buy", "long_term_buy", "sell"
            realized_pnl: 매도 시 실현손익 (원)
            realized_rate: 매도 시 실현수익률 (%)
        """
        if side == "long_term_buy":
            title = "장기 매수 체결"
            color = 0x3498DB  # 파랑
        elif side == "buy":
            title = "단기 매수 체결"
            color = 0x2ECC71  # 초록
        else:
            title = "매도 체결"
            color = 0xE74C3C  # 빨강

        total = qty * price
        fields = [
            {"name": "종목", "value": f"{stock_name} (`{stock_code}`)", "inline": True},
            {"name": "수량", "value": f"{qty:,}주", "inline": True},
            {"name": "가격", "value": f"{price:,}원", "inline": True},
            {"name": "금액", "value": f"{total:,}원", "inline": True},
        ]

        # 매도 시 실현손익 표시
        if side == "sell" and realized_pnl is not None:
            pnl_emoji = "+" if realized_pnl >= 0 else ""
            fields.append({
                "name": "실현손익 (확정)",
                "value": f"**{pnl_emoji}{realized_pnl:,}원** ({realized_rate:+.2f}%)",
                "inline": False,
            })

        # 판단 근거 / 장기투자 사유 — 전체 표시.
        # Discord embed 총 6,000 chars 한도 → 메인 embed에는 5 field까지만 담고
        # 초과분은 후속 content 메시지로 분리 송신해 어떤 길이라도 잘리지 않게.
        EMBED_FIELD_BUDGET = 5  # 메인 embed에 들어가는 reasoning field 최대
        overflow_lines: list[str] = []

        if reason:
            chunks = _split_for_discord(reason, 1000)
            for i, chunk in enumerate(chunks[:EMBED_FIELD_BUDGET]):
                fields.append({
                    "name": "판단 근거" if i == 0 else f"판단 근거 ({i+1})",
                    "value": chunk,
                    "inline": False,
                })
            if len(chunks) > EMBED_FIELD_BUDGET:
                overflow_lines.append("**판단 근거 (이어서)**")
                overflow_lines.extend(chunks[EMBED_FIELD_BUDGET:])

        if is_long_term and long_term_reason:
            lt_chunks = _split_for_discord(long_term_reason, 1000)
            for i, chunk in enumerate(lt_chunks[:EMBED_FIELD_BUDGET]):
                fields.append({
                    "name": "장기투자 사유" if i == 0 else f"장기투자 사유 ({i+1})",
                    "value": chunk,
                    "inline": False,
                })
            if len(lt_chunks) > EMBED_FIELD_BUDGET:
                overflow_lines.append("**장기투자 사유 (이어서)**")
                overflow_lines.extend(lt_chunks[EMBED_FIELD_BUDGET:])

        embed = {
            "title": title,
            "color": color,
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])
        logger.info("Discord: %s 체결 알림 전송", side)

        # 메인 embed에 다 안 들어간 분량은 후속 plain text 메시지로 이어서 송신.
        # Discord 일반 메시지 한도 2,000 chars라 _split_plain_message로 한 번 더 분할.
        if overflow_lines:
            overflow_text = "\n".join(overflow_lines)
            for part in _split_plain_message(overflow_text, max_per_msg=1900):
                self._send(content=part)

    # ── 장 마감 일일 리포트 ──

    def notify_daily_report(
        self,
        balance: dict,
        realized_today: dict,
        realized_total: dict,
        trades_today: list[dict],
        long_term_holdings: list[dict],
        analyst_standings: dict | None = None,
    ):
        """장 마감 후 일일 리포트.

        핵심: 실현손익과 미실현(평가)손익을 명확히 분리.

        Args:
            balance: KISClient.get_balance() 결과
            realized_today: 오늘 실현손익 {realized_pnl, sell_count, details}
            realized_total: 누적 실현손익 {total_realized_pnl, total_sell_count}
            trades_today: 오늘 매매 기록
            long_term_holdings: 장기투자 종목 목록
        """
        today = datetime.now().strftime("%Y-%m-%d (%a)")
        cash = balance.get("cash", 0)
        total_eval = balance.get("total_eval", 0)
        total_asset = cash + total_eval

        # 미실현 평가손익 (보유 중 → 아직 수익 아님)
        unrealized_pnl = balance.get("total_profit", 0)
        unrealized_rate = balance.get("total_profit_rate", 0)

        # 실현손익 (매도 확정 → 진짜 수익)
        realized_pnl_today = realized_today.get("realized_pnl", 0)
        realized_pnl_total = realized_total.get("total_realized_pnl", 0)

        # 색상: 실현손익 기준
        if realized_pnl_today > 0:
            color = 0x2ECC71
        elif realized_pnl_today < 0:
            color = 0xE74C3C
        else:
            color = 0x95A5A6

        fields = [
            {"name": "총 자산", "value": f"**{total_asset:,}원**", "inline": True},
            {"name": "예수금", "value": f"{cash:,}원", "inline": True},
            {"name": "주식 평가액", "value": f"{total_eval:,}원", "inline": True},
        ]

        # ── 실현손익 (확정 수익) ──
        fields.append({
            "name": "오늘 실현손익 (확정 수익)",
            "value": f"**{realized_pnl_today:+,}원** ({realized_today.get('sell_count', 0)}건 매도)",
            "inline": False,
        })
        fields.append({
            "name": "누적 실현손익 (확정 수익)",
            "value": f"**{realized_pnl_total:+,}원**",
            "inline": True,
        })

        # ── 미실현 평가손익 (보유 중, 확정 아님) ──
        fields.append({
            "name": "미실현 평가손익 (미확정)",
            "value": f"{unrealized_pnl:+,}원 ({unrealized_rate:+.2f}%)",
            "inline": True,
        })

        # ── 보유 종목 (단기) ──
        long_term_codes = {lt["code"] for lt in long_term_holdings}
        short_holdings = [h for h in balance.get("holdings", []) if h["code"] not in long_term_codes]

        if short_holdings:
            lines = []
            for h in short_holdings:
                rate = h.get("profit_rate", 0)
                marker = "+" if rate >= 0 else ""
                lines.append(
                    f"  {h['name']} — {h['qty']}주 | "
                    f"{h['avg_price']:,} -> {h['current_price']:,} | "
                    f"평가 {marker}{rate:.1f}% (미실현)"
                )
            fields.append({"name": "단기 보유 종목", "value": "\n".join(lines)[:1024], "inline": False})

        # ── 장기투자 종목 ──
        if long_term_holdings:
            lines = []
            for lt in long_term_holdings:
                # balance에서 현재가 찾기
                bh = next((h for h in balance.get("holdings", []) if h["code"] == lt["code"]), None)
                if bh:
                    rate = bh.get("profit_rate", 0)
                    marker = "+" if rate >= 0 else ""
                    lines.append(
                        f"  {lt['name']} — {lt['qty']}주 | "
                        f"평가 {marker}{rate:.1f}% (미실현)\n"
                        f"    사유: {lt.get('reason', '-')[:80]}"
                    )
                else:
                    lines.append(f"  {lt['name']} — {lt['qty']}주\n    사유: {lt.get('reason', '-')[:80]}")
            fields.append({"name": "장기투자 종목 (전체 자산의 20% 한도)", "value": "\n".join(lines)[:1024], "inline": False})

        # ── 오늘 매매 내역 ──
        if trades_today:
            lines = []
            for t in trades_today:
                if t.get("type") == "sell":
                    pnl = t.get("realized_pnl", 0)
                    lines.append(f"  매도 {t['name']} {t['qty']}주 | 실현 {pnl:+,}원")
                else:
                    lt_tag = " [장기]" if t.get("is_long_term") else ""
                    lines.append(f"  매수 {t['name']} {t['qty']}주 × {t['price']:,}원{lt_tag}")
            fields.append({"name": "오늘 매매 내역", "value": "\n".join(lines)[:1024], "inline": False})
        else:
            fields.append({"name": "오늘 매매 내역", "value": "매매 없음", "inline": False})

        # 애널리스트 경쟁 성적
        if analyst_standings:
            lines = []
            for name_kr, s in analyst_standings.items():
                "+" * s["correct"] + "-" * (s["total"] - s["correct"])
                lines.append(f"  {name_kr}: {s['total']}전 {s['correct']}승 ({s['accuracy']:.0f}%) 가중치 {s['weight']}")
            fields.append({"name": "애널리스트 경쟁 성적", "value": "\n".join(lines)[:1024], "inline": False})

        embed = {
            "title": f"일일 리포트 — {today}",
            "color": color,
            "fields": fields,
            "footer": {"text": "실현손익 = 매도 체결로 확정된 손익만 집계"},
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])
        logger.info("Discord: 일일 리포트 전송")

    # ── 긴급 홀딩 알림 ──

    def notify_emergency_hold(self, reason: str):
        """위기 감지 → 긴급 홀딩 모드 활성화 알림."""
        embed = {
            "title": "긴급 홀딩 모드 활성화",
            "description": (
                "급락/위기 상황이 감지되어 **모든 매매를 즉시 중단**합니다.\n"
                "보유 종목은 그대로 유지되며, 상황이 안정되면 자동 해제됩니다."
            ),
            "color": 0xE74C3C,
            "fields": [
                {"name": "감지 사유", "value": reason[:1000], "inline": False},
                {"name": "현재 조치", "value": "신규 매수/매도 전면 중단, 보유 종목 홀딩", "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    def notify_emergency_release(self):
        """긴급 홀딩 해제 알림."""
        embed = {
            "title": "긴급 홀딩 모드 해제",
            "description": "시장이 안정되어 자동매매를 재개합니다.",
            "color": 0x2ECC71,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    # ── 전략 교체 알림 ──

    def notify_strategy_switch(self, old_strategy: str, new_strategy: str, reason: str):
        embed = {
            "title": "전략 자동 교체",
            "description": "실현손실 누적으로 전략을 교체합니다.",
            "color": 0xF39C12,
            "fields": [
                {"name": "사유", "value": reason[:500], "inline": False},
                {"name": "기존 전략", "value": old_strategy, "inline": True},
                {"name": "새 전략", "value": new_strategy, "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    # ── 종목 위험 경고 ──

    def notify_stock_danger(self, stock_name: str, stock_code: str, danger_level: str, reasons: list, action: str):
        color = 0xE74C3C if danger_level == "critical" else 0xF39C12
        action_text = {
            "sell_immediately": "즉시 전량 매도 실행",
            "hold": "매수 금지, 보유분 감시 강화",
            "none": "정상",
        }.get(action, action)

        embed = {
            "title": f"종목 위험 감지 — {stock_name}",
            "color": color,
            "fields": [
                {"name": "위험 수준", "value": danger_level.upper(), "inline": True},
                {"name": "종목", "value": f"{stock_name} (`{stock_code}`)", "inline": True},
                {"name": "감지 키워드", "value": ", ".join(reasons), "inline": False},
                {"name": "자동 조치", "value": action_text, "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    # ── 강제 손절 알림 ──

    def notify_forced_stop_loss(self, stock_name: str, stock_code: str, loss_rate: float, qty: int):
        embed = {
            "title": "강제 손절 실행",
            "color": 0xE74C3C,
            "fields": [
                {"name": "종목", "value": f"{stock_name} (`{stock_code}`)", "inline": True},
                {"name": "손실률", "value": f"{loss_rate:+.1f}%", "inline": True},
                {"name": "매도 수량", "value": f"{qty:,}주 (전량)", "inline": True},
                {"name": "사유", "value": "종목별 손절선 도달로 추가 손실 방지", "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    # ── 일일 목표수익 도달 알림 ──

    def notify_effective_pnl(self, date: str, summary: dict):
        """실효 수익 분해 리포트 (실현/미실현/환율효과/명목증가 구분)."""
        if not summary:
            return
        effective = summary.get("effective_total", 0)
        apparent = summary.get("apparent_gain", 0)
        color = 0x2ECC71 if effective >= 0 else 0xE74C3C
        embed = {
            "title": f"실효 수익 분해 — {date}",
            "description": (f"실효 순수익 (실현+미실현): **{effective:+,}원**\n"
                            f"명목 증가 (자산-입금): {apparent:+,}원"),
            "color": color,
            "fields": [
                {"name": "실현 누적", "value": f"{summary.get('realized_total',0):+,}원", "inline": True},
                {"name": "미실현 평가차익", "value": f"{summary.get('unrealized_krw',0):+,}원", "inline": True},
                {"name": "환율·집계 효과", "value": f"{summary.get('fx_and_other_effect',0):+,}원", "inline": True},
                {"name": "현재 총자산", "value": f"{summary.get('total_equity_now',0):,}원", "inline": True},
                {"name": "누적 입금", "value": f"{summary.get('total_deposits',0):,}원", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    def notify_monthly_report(self, stats: dict):
        """매월 1일 발송 — 완료된 '지난 달' 성과. stats = get_monthly_stats() 결과.

        종목별 손익(by_stock)·승률까지 담은 상세 버전. 월 라벨을 '지난 달 결산'으로 명확히 해
        '이번 달'로 오해되던 문제(직전 달 데이터인데 라벨이 이번 달) 제거."""
        if not stats or stats.get("days", 0) == 0:
            return
        from zusik.reporting.monthly_text import _basis_label, monthly_embed_fields
        color = 0x2ECC71 if stats.get("return_pct", 0) >= 0 else 0xE74C3C
        embed = {
            "title": f"월간 리포트 — {stats.get('month', '')} (지난 달 결산)",
            "description": f"**{stats.get('month', '')}** 한 달 성과 · 기준 {_basis_label(stats)}",
            "color": color,
            "fields": monthly_embed_fields(stats),
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    def notify_monthly_html_ready(self, stats: dict, path: str = ""):
        """매달 마지막 날 — 월간 HTML 요약 저장 통지(요약 + 파일 경로)."""
        if not stats or stats.get("days", 0) == 0:
            return
        color = 0x2ECC71 if stats["return_pct"] >= 0 else 0xE74C3C
        embed = {
            "title": f"월간 HTML 리포트 저장됨 — {stats['month']}",
            "description": f"이번 달 수익률: **{stats['return_pct']:+.2f}%**",
            "color": color,
            "fields": [
                {"name": "실현 수익", "value": f"{stats['realized']:+,}원", "inline": True},
                {"name": "최대 Drawdown", "value": f"{stats['max_drawdown']:+.2f}%", "inline": True},
                {"name": "파일 위치", "value": f"`{path}`", "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    def notify_pattern_report(self, date: str, stats: dict, total_pnl: int):
        """EOD 매도 패턴 요약. stats = {pattern: {count, wins, win_rate, pnl_sum, avg_pnl}}."""
        if not stats:
            return
        fields = []
        for pat, s in sorted(stats.items(), key=lambda x: -x[1]["pnl_sum"])[:8]:
            fields.append({
                "name": f"{pat}  ({s['count']}건, 승률 {s['win_rate']:.0f}%)",
                "value": f"총 **{s['pnl_sum']:+,}원** · 건당 {int(s['avg_pnl']):+,}원",
                "inline": False,
            })
        embed = {
            "title": f"일일 매도 패턴 리포트 — {date}",
            "description": f"오늘 실현 합계: **{total_pnl:+,}원**",
            "color": 0x3498DB,
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    def notify_daily_target_reached(self, realized_pnl: int, realized_rate: float, target_rate: float):
        embed = {
            "title": "일일 목표 도달",
            "description": "목표 수익에 도달했으나, 추가 수익을 위해 계속 동작합니다.",
            "color": 0x2ECC71,
            "fields": [
                {"name": "오늘 실현수익", "value": f"**{realized_pnl:+,}원** ({realized_rate:+.2f}%)", "inline": True},
                {"name": "목표", "value": f"{target_rate:.1f}%", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    # ── 종목 자동 교체 알림 ──

    def notify_mode_upgrade(self, old_mode: str, new_mode: str, total_asset: int, tier: dict):
        """모드 자동 승격 알림."""
        embed = {
            "title": "모드 자동 승격",
            "description": "자산 증가로 트레이딩 모드가 업그레이드됩니다.",
            "color": 0x2ECC71,
            "fields": [
                {"name": "기존 모드", "value": old_mode.upper(), "inline": True},
                {"name": "새 모드", "value": new_mode.upper(), "inline": True},
                {"name": "현재 자산", "value": f"{total_asset:,}원", "inline": True},
                {"name": "종목 수 변경", "value": f"KR {tier['kr_count']}종목 + US {tier['us_count']}종목", "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    def notify_stock_rotation(self, changes: str, kr_list: str, us_list: str):
        fields = [
            {"name": "변경 사항", "value": changes[:1000], "inline": False},
        ]
        if kr_list:
            fields.append({"name": "KR 종목", "value": kr_list[:1000], "inline": False})
        if us_list:
            fields.append({"name": "US 종목", "value": us_list[:1000], "inline": False})

        embed = {
            "title": "Claude 종목 자동 교체",
            "description": "Claude AI가 시장 분석 후 매매 종목을 변경했습니다.",
            "color": 0x9B59B6,
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])

    # ── 에러 알림 ──

    def notify_error(self, message: str):
        embed = {
            "title": "봇 오류 발생",
            "description": message[:2000],
            "color": 0xE67E22,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._send(embeds=[embed])
