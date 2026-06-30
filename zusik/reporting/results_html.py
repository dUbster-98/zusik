from __future__ import annotations
"""지난 투자결과 종합 리포트 — 누적 성과 + 월별 + 매도 패턴.

`PortfolioTracker` 의 거래/자산곡선에서 종합 결과를 모아 깔끔한 자가완결 HTML 로 렌더한다.
effective(실효: 입금+누적실현+보유평가, 결제타이밍 무관) 기준이라 T+2 팬텀에 흔들리지 않는다.

  - build_results_summary(tracker) -> dict
  - render_results_html(summary, generated_at) -> str
  - write_results_html(summary, out_dir, generated_at) -> 저장 경로

PDF 는 zusik/reporting/pdf.py 로 변환(헤드리스 Chrome 등). 예시: docs/examples/.
"""

import html as _html
import os

from zusik.reporting.monthly_html import _INK, _LINE, _MUTE, _color, _pct, _won, _won_signed


def _eff_equity(c: dict) -> int:
    v = c.get("effective_equity")
    return int(v) if isinstance(v, (int, float)) else int(c.get("total_equity", 0) or 0)


def _eff_dd(c: dict) -> float:
    v = c.get("effective_drawdown_pct")
    return float(v) if isinstance(v, (int, float)) else float(c.get("drawdown_pct", 0.0) or 0.0)


def build_results_summary(tracker) -> dict:
    """PortfolioTracker → 종합 결과 dict (누적·월별·패턴). 읽기 전용."""
    from zusik.storage.portfolio_tracker import EQUITY_CURVE_FILE, _load_json
    curve = _load_json(EQUITY_CURVE_FILE)
    if not isinstance(curve, list):
        curve = []
    # 매도 타이밍/선택 alpha 캐시(운영자 스크립트가 생성). 없으면 섹션 생략.
    _ddir = os.path.dirname(EQUITY_CURVE_FILE)
    sell_timing = _load_json(os.path.join(_ddir, "sell_timing.json"))
    selection_alpha = _load_json(os.path.join(_ddir, "selection_alpha.json"))
    curve = sorted([c for c in curve if c.get("date")], key=lambda c: c["date"])

    trades = list(getattr(tracker, "_trades", []) or [])
    sells = [t for t in trades if t.get("type") == "sell"]
    buys = [t for t in trades if t.get("type") == "buy"]
    wins = sum(1 for t in sells if (t.get("realized_pnl") or 0) > 0)
    realized_total = sum((t.get("realized_pnl") or 0) for t in sells)
    deposits = tracker.get_total_deposits()

    latest = curve[-1] if curve else {}
    unrealized = int(latest.get("unrealized_krw", 0) or 0)
    eff_equity = _eff_equity(latest) if curve else int(deposits + realized_total + unrealized)
    effective_total = realized_total + unrealized
    return_pct = ((eff_equity - deposits) / deposits * 100) if deposits > 0 else 0.0
    max_dd = min((_eff_dd(c) for c in curve), default=0.0)

    months = sorted({c["date"][:7] for c in curve})
    month_stats = []
    for m in months:
        try:
            ms = tracker.get_monthly_stats(int(m[:4]), int(m[5:7]))
        except Exception:
            continue
        if ms.get("days", 0):
            month_stats.append(ms)

    patterns = tracker.get_pattern_stats(days=None)
    pat_list = sorted(({"pattern": k, **v} for k, v in patterns.items()),
                      key=lambda x: -x["pnl_sum"])

    # 종목별 손익 — 어떤 종목이 벌고 잃었나 (매도 기록 집계)
    from collections import defaultdict
    by = defaultdict(lambda: {"name": "", "code": "", "count": 0, "wins": 0, "pnl": 0})
    for t in sells:
        code = t.get("code") or t.get("ticker") or "?"
        g = by[code]
        g["code"] = code
        g["name"] = t.get("name") or g["name"] or code
        g["count"] += 1
        p = t.get("realized_pnl") or 0
        g["pnl"] += p
        if p > 0:
            g["wins"] += 1
    by_stock = sorted(by.values(), key=lambda x: -x["pnl"])

    n_sell = len(sells)
    return {
        "period": {"start": curve[0]["date"] if curve else "",
                   "end": latest.get("date", "") if curve else "",
                   "days": len(curve)},
        "deposits": deposits,
        "realized_total": realized_total,
        "unrealized": unrealized,
        "effective_total": effective_total,
        "effective_equity": eff_equity,
        "return_pct": round(return_pct, 2),
        "max_drawdown": round(max_dd, 2),
        "buys": len(buys),
        "sells": n_sell,
        "wins": wins,
        "losses": n_sell - wins,
        "win_rate": round(wins / n_sell * 100, 1) if n_sell else 0.0,
        "patterns": pat_list,
        "by_stock": by_stock,
        "months": month_stats,
        "sell_timing": sell_timing if isinstance(sell_timing, dict) else None,
        "selection_alpha": selection_alpha if isinstance(selection_alpha, dict) else None,
    }


def _card(k: str, v: str, color: str = _INK) -> str:
    return (f'<div class="card"><div class="k">{_html.escape(k)}</div>'
            f'<div class="v" style="color:{color}">{_html.escape(v)}</div></div>')


def render_results_html(summary: dict, generated_at: str = "") -> str:
    s = summary or {}
    per = s.get("period", {})
    rng = f"{per.get('start', '')} ~ {per.get('end', '')} ({per.get('days', 0)}일)"
    ret = float(s.get("return_pct", 0.0) or 0.0)
    accent = _color(ret)

    cards = "\n".join([
        _card("현재 실효자산", _won(s.get("effective_equity", 0))),
        _card("누적 입금", _won(s.get("deposits", 0))),
        _card("실현손익(누적)", _won_signed(s.get("realized_total", 0)), _color(s.get("realized_total", 0))),
        _card("미실현(평가)", _won_signed(s.get("unrealized", 0)), _color(s.get("unrealized", 0))),
        _card("실효 순수익", _won_signed(s.get("effective_total", 0)), _color(s.get("effective_total", 0))),
        _card("최대 낙폭(MaxDD)", _pct(s.get("max_drawdown", 0.0)), "#cf222e"),
        _card("승률", f"{s.get('win_rate', 0)}% ({s.get('wins', 0)}/{s.get('sells', 0)})", _MUTE),
        _card("매수 / 매도", f"{s.get('buys', 0)} / {s.get('sells', 0)} 건", _MUTE),
    ])

    # 종목별 손익 표 (어떤 종목이 벌었나)
    srows = "\n".join(
        f'      <tr><td>{_html.escape(str(st.get("name") or st.get("code", "")))}'
        f' <span style="color:{_MUTE}">({_html.escape(str(st.get("code", "")))})</span></td>'
        f'<td style="text-align:right">{st.get("count", 0)}</td>'
        f'<td style="text-align:right">'
        f'{(st.get("wins", 0) / st["count"] * 100) if st.get("count") else 0:.0f}%</td>'
        f'<td style="color:{_color(st.get("pnl", 0))};text-align:right">{_won_signed(st.get("pnl", 0))}</td></tr>'
        for st in s.get("by_stock", [])
    ) or '      <tr><td colspan="4" style="color:#656d76">매도 기록 없음</td></tr>'

    # 월별 표
    mrows = "\n".join(
        f'      <tr><td>{_html.escape(m.get("month", ""))}</td>'
        f'<td style="color:{_color(m.get("return_pct", 0))};text-align:right">{_pct(m.get("return_pct", 0))}</td>'
        f'<td style="color:{_color(m.get("realized", 0))};text-align:right">{_won_signed(m.get("realized", 0))}</td>'
        f'<td style="color:#cf222e;text-align:right">{_pct(m.get("max_drawdown", 0))}</td></tr>'
        for m in s.get("months", [])
    ) or '      <tr><td colspan="4" style="color:#656d76">월별 데이터 없음</td></tr>'

    # 매도 패턴 표 (어떤 패턴이 돈을 벌었나)
    prows = "\n".join(
        f'      <tr><td>{_html.escape(str(p.get("pattern", "")))}</td>'
        f'<td style="text-align:right">{p.get("count", 0)}</td>'
        f'<td style="text-align:right">{p.get("win_rate", 0):.0f}%</td>'
        f'<td style="color:{_color(p.get("pnl_sum", 0))};text-align:right">{_won_signed(p.get("pnl_sum", 0))}</td></tr>'
        for p in s.get("patterns", [])
    ) or '      <tr><td colspan="4" style="color:#656d76">매도 기록 없음</td></tr>'

    # 매도 타이밍 사후분석 섹션 (캐시 있을 때만) — 팔고 난 뒤 놓친 상승/막은 하락
    st_section = ""
    st = s.get("sell_timing")
    if isinstance(st, dict) and st.get("by_pattern"):
        prim = st.get("primary", 5)
        strows = "\n".join(
            f'      <tr><td>{_html.escape(str(pat))}</td>'
            f'<td style="text-align:right">{v.get("count", 0)}</td>'
            f'<td style="text-align:right">{v.get("avg_same_day_missed", 0):+.1f}%</td>'
            f'<td style="color:{_color(v.get("avg_missed_upside", 0))};text-align:right">{v.get("avg_missed_upside", 0):+.1f}%</td>'
            f'<td style="color:{_color(-v.get("avg_avoided_drop", 0))};text-align:right">{v.get("avg_avoided_drop", 0):+.1f}%</td>'
            f'<td style="color:{_color(v.get("avg_net_if_held", 0))};text-align:right">{v.get("avg_net_if_held", 0):+.1f}%</td>'
            f'<td style="color:{_MUTE}">{_html.escape(str(v.get("verdict", "")))}</td></tr>'
            for pat, v in st["by_pattern"].items())
        st_section = f"""
    <h2>매도 타이밍 사후분석 (팔고 난 뒤 얼마나 더 올랐나 / 빠졌나)</h2>
    <table>
      <tr><th>패턴</th><th style="text-align:right">건수</th><th style="text-align:right">당일놓침</th>
      <th style="text-align:right">놓친상승({prim}일)</th><th style="text-align:right">막은하락</th>
      <th style="text-align:right">홀드종가</th><th>판정</th></tr>
{strows}
    </table>
    <p style="color:{_MUTE};font-size:12px;margin:6px 2px">당일놓침=매도 후 같은 날 장중 최대 추가 상승 · 놓친상승/막은하락=매도 후 {prim}거래일 forward · '조기매도'는 더 늦게, '보호 성공'은 유지가 데이터 권고</p>"""

    # 종목선택 alpha 섹션 (캐시 있을 때만) — 지수 대비 초과수익 + 놓친 최고종목
    sel_section = ""
    sel = s.get("selection_alpha")
    if isinstance(sel, dict) and sel.get("alpha"):
        a = sel["alpha"]
        mrows2 = "\n".join(
            f'      <tr><td>{_html.escape(str(mk))}</td>'
            f'<td style="text-align:right">{m.get("count", 0)}</td>'
            f'<td style="color:{_color(m.get("avg_pick_return", 0))};text-align:right">{m.get("avg_pick_return", 0):+.1f}%</td>'
            f'<td style="color:{_color(m.get("avg_alpha", 0))};text-align:right">{m.get("avg_alpha", 0):+.1f}%p</td>'
            f'<td style="text-align:right">{m.get("beat_index_rate", 0)}%</td></tr>'
            for mk, m in (sel.get("by_market") or {}).items())
        mb = sel.get("missed_best") or {}
        mb_html = ""
        if mb.get("days"):
            mb_html = (f'<p style="color:{_MUTE};font-size:12px;margin:6px 2px">'
                       f'놓친 최고종목({mb.get("days")}일): 봇 최선 픽 평균 {mb.get("avg_bot_best_return", 0):+.1f}% '
                       f'vs 놓친 최고 평균 {mb.get("avg_missed_best_return", 0):+.1f}% → gap {mb.get("avg_gap", 0):+.1f}%p</p>')
        sel_section = f"""
    <h2>종목선택 평가 (지수 대비 초과수익 alpha, {a.get('window', 10)}거래일)</h2>
    <div class="grid">
{_card("평균 종목수익", f"{a.get('avg_pick_return', 0):+.2f}%", _color(a.get('avg_pick_return', 0)))}
{_card("지수 대비 alpha", f"{a.get('avg_alpha', 0):+.2f}%p", _color(a.get('avg_alpha', 0)))}
{_card("지수 초과 비율", f"{a.get('beat_index_rate', 0)}%", _MUTE)}
{_card("평가 매수건수", f"{a.get('count', 0)}건", _MUTE)}
    </div>
    <table style="margin-top:12px">
      <tr><th>시장</th><th style="text-align:right">건수</th><th style="text-align:right">종목수익</th><th style="text-align:right">alpha</th><th style="text-align:right">초과율</th></tr>
{mrows2}
    </table>{mb_html}"""

    gen = _html.escape(generated_at or "")
    gen_html = f'<div class="gen">생성: {gen}</div>' if gen else ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>투자결과 리포트</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #f6f8fa; color: {_INK};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo",
      "Malgun Gothic", "Noto Sans CJK KR", Roboto, sans-serif; line-height: 1.5; }}
  .wrap {{ max-width: 760px; margin: 0 auto; padding: 32px 20px 48px; }}
  .head {{ display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 1px solid {_LINE}; padding-bottom: 12px; margin-bottom: 20px; }}
  .head h1 {{ font-size: 20px; margin: 0; font-weight: 700; }}
  .head .sub {{ color: {_MUTE}; font-size: 13px; }}
  .hero {{ text-align: center; padding: 22px 0 26px; }}
  .hero .label {{ color: {_MUTE}; font-size: 13px; letter-spacing: .04em; }}
  .hero .ret {{ font-size: 52px; font-weight: 800; color: {accent}; line-height: 1.1; margin-top: 6px; }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
  .card {{ background: #fff; border: 1px solid {_LINE}; border-radius: 10px; padding: 14px 16px; }}
  .card .k {{ color: {_MUTE}; font-size: 12px; margin-bottom: 5px; }}
  .card .v {{ font-size: 18px; font-weight: 700; }}
  h2 {{ font-size: 15px; margin: 28px 0 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
    border: 1px solid {_LINE}; border-radius: 10px; overflow: hidden; font-size: 13px; }}
  th, td {{ padding: 9px 12px; border-bottom: 1px solid {_LINE}; }}
  th {{ background: #f0f3f6; color: {_MUTE}; font-weight: 600; text-align: left; }}
  tr:last-child td {{ border-bottom: none; }}
  .foot {{ margin-top: 26px; color: {_MUTE}; font-size: 12px; text-align: center; }}
  .gen {{ margin-top: 4px; }}
  @media (max-width: 480px) {{ .grid {{ grid-template-columns: 1fr; }} .hero .ret {{ font-size: 40px; }} }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h1>투자결과 종합 리포트</h1>
      <div class="sub">{_html.escape(rng)}</div>
    </div>
    <div class="hero">
      <div class="label">실효 누적 수익률 (입금 대비)</div>
      <div class="ret">{_pct(ret)}</div>
    </div>
    <div class="grid">
{cards}
    </div>

    <h2>종목별 손익 (어떤 종목이 벌었나)</h2>
    <table>
      <tr><th>종목</th><th style="text-align:right">매도건수</th><th style="text-align:right">승률</th><th style="text-align:right">총 손익</th></tr>
{srows}
    </table>

    <h2>월별 성과</h2>
    <table>
      <tr><th>월</th><th style="text-align:right">수익률</th><th style="text-align:right">실현손익</th><th style="text-align:right">최대낙폭</th></tr>
{mrows}
    </table>

    <h2>매도 패턴별 손익 (무엇이 돈을 벌었나)</h2>
    <table>
      <tr><th>패턴</th><th style="text-align:right">건수</th><th style="text-align:right">승률</th><th style="text-align:right">총 손익</th></tr>
{prows}
    </table>
{st_section}
{sel_section}

    <div class="foot">
      zusik 자동매매 · 투자결과 종합 (실효 기준: 입금 + 누적실현 + 보유평가, 결제타이밍 보정)
      {gen_html}
    </div>
  </div>
</body>
</html>
"""


def write_results_html(summary: dict, out_dir: str, generated_at: str = "") -> str:
    """결과 리포트 HTML 을 {out_dir}/results.html 로 원자적 저장. 경로 반환."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "results.html")
    body = render_results_html(summary, generated_at)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)
    return path
