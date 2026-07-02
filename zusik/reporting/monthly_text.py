from __future__ import annotations

"""월간 리포트 공유 텍스트 포맷터.

여러 알림 경로(webhook embed / 멀티메신저 텍스트 / bot 텍스트)가 제각각 단순 요약을 만들던 걸
한 곳으로 모은다. `PortfolioTracker.get_monthly_stats()` 결과(by_stock·승률 포함)를 상세 텍스트로
렌더. 월 라벨은 '지난 달 결산'으로 명확히 — 리포트는 완료된 직전 달을 대상으로 발송되기 때문.
"""


def _winrate(by_stock: list) -> tuple:
    """(trades, wins, winrate%) — by_stock 의 건수/승수 집계."""
    trades = sum(int(g.get("count", 0)) for g in by_stock)
    wins = sum(int(g.get("wins", 0)) for g in by_stock)
    return trades, wins, (wins / trades * 100) if trades else 0.0


def _basis_label(stats: dict) -> str:
    return "실효(T+2 면역)" if stats.get("basis") == "effective" else "raw"


def stock_rows(by_stock: list, top: int = 5):
    """(winners, losers) — pnl 내림차순 수익 종목 / 오름차순 손실 종목, 각 top개."""
    winners = [g for g in by_stock if (g.get("pnl", 0) or 0) > 0][:top]
    losers = sorted([g for g in by_stock if (g.get("pnl", 0) or 0) < 0],
                    key=lambda g: g.get("pnl", 0))[:top]
    return winners, losers


def _fmt_rows(rows) -> str:
    if not rows:
        return "—"
    return "\n".join(
        f"· {str(g.get('name') or g.get('code') or '?')[:16]} "
        f"{int(g.get('pnl', 0) or 0):+,}원 ({int(g.get('count', 0))}건)"
        for g in rows)


def _entry_bucket_lines(stats: dict) -> str:
    """진입 버킷별 ROI 요약 — leftover/force_buy 등이 음수 전환하는지 상시 관측."""
    buckets = stats.get("entry_buckets") or {}
    if not buckets:
        return ""
    rows = []
    for b, s in sorted(buckets.items(), key=lambda x: -x[1].get("n", 0)):
        n = s.get("n", 0)
        if n <= 0:
            continue
        rows.append(f"· {b}: {n}건 승률 {s.get('wins', 0) / n * 100:.0f}% "
                    f"{int(s.get('pnl', 0)):+,}원")
    return "\n".join(rows)


def format_monthly_report(stats: dict, period_label: str = "지난 달") -> str:
    """월간 성과 상세 텍스트(멀티메신저/bot 공용). stats 가 비면 빈 문자열.

    period_label: 매월 1일 자동 발송은 '지난 달'(기본), 월중 온디맨드 생성은 '이번 달' 등으로 표기."""
    if not stats or stats.get("days", 0) == 0:
        return ""
    month = stats.get("month", "")
    by = stats.get("by_stock", []) or []
    trades, wins, winrate = _winrate(by)
    winners, losers = stock_rows(by)
    lines = [
        f"**{month} 월간 결산** ({period_label} · {_basis_label(stats)})",
        f"수익률 **{stats.get('return_pct', 0):+.2f}%** "
        f"({int(stats.get('net_growth', 0)):+,}원) · 승률 {winrate:.0f}% ({wins}/{trades})",
        f"시작 {int(stats.get('start_equity', 0)):,}원 → 종료 "
        f"{int(stats.get('end_equity', 0)):,}원 · 입금 {int(stats.get('deposits', 0)):,}원",
        f"실현 {int(stats.get('realized', 0)):+,}원 · 최대DD "
        f"{stats.get('max_drawdown', 0):+.2f}% · 기록 {stats.get('days', 0)}일",
    ]
    if winners:
        lines.append("\n수익 종목 TOP\n" + _fmt_rows(winners))
    if losers:
        lines.append("\n손실 종목\n" + _fmt_rows(losers))
    eb = _entry_bucket_lines(stats)
    if eb:
        lines.append(f"\n진입 유형별 ({month or '해당 월'})\n" + eb)
    return "\n".join(lines)


def monthly_embed_fields(stats: dict) -> list:
    """Discord embed fields (상세). discord_notifier 전용."""
    by = stats.get("by_stock", []) or []
    trades, wins, winrate = _winrate(by)
    winners, losers = stock_rows(by)
    fields = [
        {"name": "수익률", "value": f"**{stats.get('return_pct', 0):+.2f}%** "
         f"({int(stats.get('net_growth', 0)):+,}원)", "inline": True},
        {"name": "실현 손익", "value": f"{int(stats.get('realized', 0)):+,}원", "inline": True},
        {"name": "승률", "value": f"{winrate:.0f}% ({wins}/{trades})", "inline": True},
        {"name": "시작 → 종료", "value": f"{int(stats.get('start_equity', 0)):,} → "
         f"**{int(stats.get('end_equity', 0)):,}**원", "inline": False},
        {"name": "입금", "value": f"{int(stats.get('deposits', 0)):,}원", "inline": True},
        {"name": "최대 Drawdown", "value": f"{stats.get('max_drawdown', 0):+.2f}%", "inline": True},
        {"name": "기록 일수", "value": f"{stats.get('days', 0)}일", "inline": True},
    ]
    if winners:
        fields.append({"name": "수익 종목 TOP", "value": _fmt_rows(winners), "inline": False})
    if losers:
        fields.append({"name": "손실 종목", "value": _fmt_rows(losers), "inline": False})
    eb = _entry_bucket_lines(stats)
    if eb:
        fields.append({"name": f"진입 유형별 ({stats.get('month', '') or '해당 월'})",
                       "value": eb, "inline": False})
    return fields
