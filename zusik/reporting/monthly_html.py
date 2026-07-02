from __future__ import annotations
"""월간 성과 요약 HTML 렌더러.

`PortfolioTracker.get_monthly_stats()` 결과를 깔끔한 단일 HTML(자가완결·inline CSS,
외부 폰트/JS/CDN 0)로 렌더한다. 오프라인에서 그대로 열리고, 첨부/공유해도 안전하다.

  - render_monthly_html(stats, generated_at) -> str
  - write_monthly_html(stats, out_dir, generated_at) -> 저장 경로

봇은 매달 마지막 날 post_market 에서 reports/monthly/{YYYY-MM}.html 로 저장한다
(zusik/core/bot_reporting.py:_generate_monthly_html_report). 예시: docs/examples/.
"""

import html as _html
import os

_POS = "#1a7f37"   # 이익(초록)
_NEG = "#cf222e"   # 손실(빨강)
_INK = "#1f2328"
_MUTE = "#656d76"
_LINE = "#d0d7de"
_BG = "#f6f8fa"

# 매도 패턴 태그 → 한국어 라벨 (PortfolioTracker._classify_sell_pattern 와 동일 키)
_PATTERN_LABEL = {
    "split_profit": "분할 익절",
    "rsi_overbought": "RSI 과매수 익절",
    "ambiguous_take": "모호구간 익절",
    "trailing_stop": "트레일링 스톱",
    "breakeven_protect": "본전 보호",
    "forced_stop": "손절선(-15%)",
    "crash_instant": "급락 즉시매도",
    "slow_bleed": "느린 출혈",
    "inverse_eod_lock": "인버스 마감 락인",
    "inverse_take": "인버스 빠른 익절",
    "inverse_exit": "인버스 강제청산",
    "rotate": "종목 교체",
    "manual": "수동 매도",
    "other": "기타",
}


def _won(n) -> str:
    try:
        return f"{int(round(float(n))):,}원"
    except (TypeError, ValueError):
        return "-"


def _won_signed(n) -> str:
    """손익 표기 — 부호 포함 (예: +1,028,400원)."""
    try:
        return f"{int(round(float(n))):+,}원"
    except (TypeError, ValueError):
        return "-"


def _pct(n) -> str:
    try:
        return f"{float(n):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def _color(n) -> str:
    try:
        return _POS if float(n) >= 0 else _NEG
    except (TypeError, ValueError):
        return _INK


def render_monthly_html(stats: dict, generated_at: str = "") -> str:
    """월간 통계 dict → 자가완결 HTML 문자열. 외부 의존성 없음."""
    stats = stats or {}
    month = _html.escape(str(stats.get("month", "")))
    days = int(stats.get("days", 0) or 0)
    ret = float(stats.get("return_pct", 0.0) or 0.0)
    accent = _color(ret)

    # 지표 카드 (라벨, 값, 강조색)
    cards = [
        ("시작 자산", _won(stats.get("start_equity", 0)), _INK),
        ("종료 자산", _won(stats.get("end_equity", 0)), _INK),
        ("입금", _won(stats.get("deposits", 0)), _INK),
        ("실현손익", _won_signed(stats.get("realized", 0)), _color(stats.get("realized", 0))),
        ("순증 (자산 − 투입)", _won_signed(stats.get("net_growth", 0)), _color(stats.get("net_growth", 0))),
        ("최대 낙폭 (MaxDD)", _pct(stats.get("max_drawdown", 0.0)), _NEG),
        ("기록 일수", f"{days}일", _MUTE),
    ]
    card_html = "\n".join(
        f'      <div class="card"><div class="k">{_html.escape(k)}</div>'
        f'<div class="v" style="color:{c}">{_html.escape(v)}</div></div>'
        for k, v, c in cards
    )
    gen = _html.escape(generated_at or "")
    gen_html = f'<div class="gen">생성: {gen}</div>' if gen else ""
    basis_label = ("실효 기준 (총자산 − 투입, 결제타이밍 보정)"
                   if stats.get("basis", "effective") == "effective"
                   else "명목 기준 (실효 데이터 없음 — 결제타이밍 영향 가능)")

    # 종목별 손익 (이 달 매도 집계) — 매도가 있을 때만 섹션 표시
    srows = "\n".join(
        f'      <tr><td>{_html.escape(str(st.get("name") or st.get("code", "")))}'
        f' <span style="color:{_MUTE}">({_html.escape(str(st.get("code", "")))})</span></td>'
        f'<td style="text-align:right">{st.get("count", 0)}</td>'
        f'<td style="text-align:right">'
        f'{(st.get("wins", 0) / st["count"] * 100) if st.get("count") else 0:.0f}%</td>'
        f'<td style="color:{_color(st.get("pnl", 0))};text-align:right">'
        f'{_won_signed(st.get("pnl", 0))}</td></tr>'
        for st in stats.get("by_stock", [])
    )
    stock_section = ("" if not srows else
                     '\n    <h2>종목별 손익</h2>\n    <table>\n'
                     '      <tr><th>종목</th><th style="text-align:right">매도건수</th>'
                     '<th style="text-align:right">승률</th>'
                     '<th style="text-align:right">총 손익</th></tr>\n'
                     f'{srows}\n    </table>')

    # 매도 패턴별 손익 (이 달) — "무엇이 돈을 벌었나". 종합 리포트와 동일 축
    prows = "\n".join(
        f'      <tr><td>{_html.escape(_PATTERN_LABEL.get(p.get("pattern", ""), p.get("pattern", "")))}</td>'
        f'<td style="text-align:right">{p.get("count", 0)}</td>'
        f'<td style="text-align:right">'
        f'{(p.get("wins", 0) / p["count"] * 100) if p.get("count") else 0:.0f}%</td>'
        f'<td style="color:{_color(p.get("pnl", 0))};text-align:right">'
        f'{_won_signed(p.get("pnl", 0))}</td></tr>'
        for p in stats.get("by_pattern", [])
    )
    pattern_section = ("" if not prows else
                       '\n    <h2>매도 패턴별 손익 (무엇이 돈을 벌었나)</h2>\n    <table>\n'
                       '      <tr><th>패턴</th><th style="text-align:right">건수</th>'
                       '<th style="text-align:right">승률</th>'
                       '<th style="text-align:right">총 손익</th></tr>\n'
                       f'{prows}\n    </table>')

    # 진입 유형별 손익 (이 달) — leftover/force_buy 등이 음수 전환하는지 관측
    _entry_label = {"normal": "일반", "leftover": "잔금소진", "force_buy": "강제매수",
                    "idle_cash": "유휴진입", "manual": "수동", "unknown": "미상"}
    ebuckets = stats.get("entry_buckets") or {}
    erows = "\n".join(
        f'      <tr><td>{_html.escape(_entry_label.get(b, b))}</td>'
        f'<td style="text-align:right">{s.get("n", 0)}</td>'
        f'<td style="text-align:right">'
        f'{(s.get("wins", 0) / s["n"] * 100) if s.get("n") else 0:.0f}%</td>'
        f'<td style="color:{_color(s.get("pnl", 0))};text-align:right">'
        f'{_won_signed(s.get("pnl", 0))}</td></tr>'
        for b, s in sorted(ebuckets.items(), key=lambda x: -x[1].get("n", 0))
        if s.get("n", 0) > 0
    )
    entry_section = ("" if not erows else
                     '\n    <h2>진입 유형별 손익</h2>\n    <table>\n'
                     '      <tr><th>진입</th><th style="text-align:right">건수</th>'
                     '<th style="text-align:right">승률</th>'
                     '<th style="text-align:right">총 손익</th></tr>\n'
                     f'{erows}\n    </table>')

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>월간 리포트 {month}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: {_BG}; color: {_INK};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo",
      "Malgun Gothic", Roboto, sans-serif; line-height: 1.5; }}
  .wrap {{ max-width: 720px; margin: 0 auto; padding: 32px 20px 48px; }}
  .head {{ display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 1px solid {_LINE}; padding-bottom: 12px; margin-bottom: 24px; }}
  .head h1 {{ font-size: 20px; margin: 0; font-weight: 700; }}
  .head .sub {{ color: {_MUTE}; font-size: 13px; }}
  .hero {{ text-align: center; padding: 28px 0 32px; }}
  .hero .label {{ color: {_MUTE}; font-size: 13px; letter-spacing: .04em; }}
  .hero .ret {{ font-size: 56px; font-weight: 800; color: {accent};
    line-height: 1.1; margin-top: 6px; }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
  .card {{ background: #fff; border: 1px solid {_LINE}; border-radius: 10px;
    padding: 16px 18px; }}
  .card .k {{ color: {_MUTE}; font-size: 12px; margin-bottom: 6px; }}
  .card .v {{ font-size: 19px; font-weight: 700; }}
  h2 {{ font-size: 15px; margin: 26px 0 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
    border: 1px solid {_LINE}; border-radius: 10px; overflow: hidden; font-size: 13px; }}
  th, td {{ padding: 9px 12px; border-bottom: 1px solid {_LINE}; }}
  th {{ background: #f0f3f6; color: {_MUTE}; font-weight: 600; text-align: left; }}
  tr:last-child td {{ border-bottom: none; }}
  .foot {{ margin-top: 28px; color: {_MUTE}; font-size: 12px; text-align: center; }}
  .gen {{ margin-top: 4px; }}
  @media (max-width: 480px) {{ .grid {{ grid-template-columns: 1fr; }}
    .hero .ret {{ font-size: 44px; }} }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h1>월간 성과 리포트</h1>
      <div class="sub">{month}</div>
    </div>
    <div class="hero">
      <div class="label">이번 달 수익률</div>
      <div class="ret">{_pct(ret)}</div>
    </div>
    <div class="grid">
{card_html}
    </div>
{stock_section}
{pattern_section}
{entry_section}
    <div class="foot">
      zusik 자동매매 · 월간 요약 · {_html.escape(basis_label)}
      {gen_html}
    </div>
  </div>
</body>
</html>
"""


def write_monthly_html(stats: dict, out_dir: str, generated_at: str = "") -> str:
    """월간 HTML 을 {out_dir}/{month}.html 로 원자적 저장. 저장 경로 반환."""
    os.makedirs(out_dir, exist_ok=True)
    month = str((stats or {}).get("month") or "report")
    path = os.path.join(out_dir, f"{month}.html")
    body = render_monthly_html(stats, generated_at)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)
    return path
