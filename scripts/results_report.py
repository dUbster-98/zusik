#!/usr/bin/env python3
"""지난 투자결과 종합 리포트 생성 — HTML + PDF.

누적 실효 수익·월별 성과·매도 패턴별 손익을 한 장에 모은다(effective 기준, T+2 팬텀 보정).
PDF 는 시스템의 헤드리스 Chrome/Chromium 등으로 변환(없으면 HTML 만; docs/LOGS_AND_REPORTS.md).

  python3 scripts/results_report.py             # 실데이터 → reports/results.{html,pdf}
  python3 scripts/results_report.py --no-pdf    # HTML 만
  python3 scripts/results_report.py --example   # 샘플로 docs/examples/ 예시(HTML+PDF)

읽기 전용(data/). 매매 동작을 바꾸지 않는다. ExecStartPre/CI 대상 아님.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 예시용 가상 데이터 — 실제 계좌/보유종목과 무관한 더미값(레이아웃/공유 확인용).
_EXAMPLE = {
    "period": {"start": "2024-01-02", "end": "2024-01-31", "days": 21},
    "deposits": 10_000_000, "realized_total": 520_000, "unrealized": 130_000,
    "effective_total": 650_000, "effective_equity": 10_650_000,
    "return_pct": 6.50, "max_drawdown": -4.20,
    "buys": 40, "sells": 36, "wins": 25, "losses": 11, "win_rate": 69.4,
    "patterns": [
        {"pattern": "rsi_overbought", "count": 12, "win_rate": 100, "pnl_sum": 360_000, "avg_pnl": 30_000},
        {"pattern": "split_profit", "count": 8, "win_rate": 100, "pnl_sum": 200_000, "avg_pnl": 25_000},
        {"pattern": "ambiguous_take", "count": 5, "win_rate": 80, "pnl_sum": 90_000, "avg_pnl": 18_000},
        {"pattern": "trailing_stop", "count": 4, "win_rate": 50, "pnl_sum": 20_000, "avg_pnl": 5_000},
        {"pattern": "slow_bleed", "count": 3, "win_rate": 0, "pnl_sum": -40_000, "avg_pnl": -13_333},
    ],
    "by_stock": [
        {"name": "예시 종목 A", "code": "000001", "count": 8, "wins": 7, "pnl": 240_000},
        {"name": "예시 종목 B", "code": "000002", "count": 6, "wins": 5, "pnl": 160_000},
        {"name": "Example Co", "code": "DEMO", "count": 7, "wins": 5, "pnl": 110_000},
        {"name": "예시 종목 C", "code": "000003", "count": 5, "wins": 4, "pnl": 70_000},
        {"name": "예시 종목 D", "code": "000004", "count": 4, "wins": 1, "pnl": -60_000},
    ],
    "months": [
        {"month": "2023-12", "return_pct": 1.80, "realized": 150_000, "max_drawdown": -3.10, "days": 6},
        {"month": "2024-01", "return_pct": 4.60, "realized": 370_000, "max_drawdown": -4.20, "days": 15},
    ],
    # 매도 타이밍 사후분석 — 가상 더미(레이아웃/공유용). 실데이터는 sell_timing_review.py 가 생성.
    "sell_timing": {
        "primary": 5,
        "by_pattern": {
            "crash_instant": {"count": 5, "avg_same_day_missed": 5.4, "avg_missed_upside": 15.1,
                              "avg_avoided_drop": 4.2, "avg_net_if_held": 8.0, "too_early": 4,
                              "protected": 1, "verdict": "조기매도(상승 놓침)"},
            "rsi_overbought": {"count": 12, "avg_same_day_missed": 2.6, "avg_missed_upside": 9.4,
                               "avg_avoided_drop": 3.0, "avg_net_if_held": 3.1, "too_early": 5,
                               "protected": 2, "verdict": "조기매도(상승 놓침)"},
            "breakeven_protect": {"count": 8, "avg_same_day_missed": 2.0, "avg_missed_upside": 4.8,
                                  "avg_avoided_drop": 6.1, "avg_net_if_held": -1.9, "too_early": 2,
                                  "protected": 5, "verdict": "보호 성공(하락 회피)"},
            "slow_bleed": {"count": 3, "avg_same_day_missed": 1.2, "avg_missed_upside": 1.0,
                           "avg_avoided_drop": 9.5, "avg_net_if_held": -6.8, "too_early": 0,
                           "protected": 3, "verdict": "보호 성공(하락 회피)"},
        },
        "overall": {"analyzed": 28, "pending": 4, "avg_same_day_missed": 2.9,
                    "avg_missed_upside": 8.1, "avg_avoided_drop": 5.3},
    },
    # 종목선택 alpha — 가상 더미. 실데이터는 selection_alpha_review.py 가 생성.
    "selection_alpha": {
        "alpha": {"count": 40, "avg_pick_return": 3.10, "avg_alpha": 1.80,
                  "beat_index_rate": 55.0, "window": 10},
        "by_market": {
            "KR": {"count": 22, "avg_pick_return": 2.10, "avg_alpha": 0.60, "beat_index_rate": 50.0},
            "US": {"count": 18, "avg_pick_return": 4.30, "avg_alpha": 3.20, "beat_index_rate": 61.0},
        },
        "missed_best": {"days": 12, "avg_bot_best_return": 4.20,
                        "avg_missed_best_return": 11.50, "avg_gap": 7.30},
    },
}


def _to_pdf(html_path: str, pdf_path: str) -> None:
    from zusik.reporting.pdf import html_to_pdf, pdf_backend
    if not pdf_backend():
        print(f"  (PDF 백엔드 없음 — HTML 만 생성. Chrome/Chromium 또는 wkhtmltopdf 설치 시 PDF 자동)")
        return
    out = html_to_pdf(html_path, pdf_path)
    print(f"  PDF: {out}" if out else "  PDF 변환 실패 — HTML 은 정상 생성됨")


def main() -> int:
    ap = argparse.ArgumentParser(description="투자결과 종합 리포트(HTML+PDF)")
    ap.add_argument("--no-pdf", action="store_true", help="HTML 만 생성")
    ap.add_argument("--example", action="store_true",
                    help="샘플로 docs/examples/results_report_example.{html,pdf} 생성")
    args = ap.parse_args()

    from zusik.reporting.results_html import render_results_html, write_results_html
    from zusik import paths

    if args.example:
        out = os.path.join(str(paths.ROOT), "docs", "examples")
        os.makedirs(out, exist_ok=True)
        html_path = os.path.join(out, "results_report_example.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_results_html(_EXAMPLE, generated_at="(예시 — 샘플 데이터)"))
        print(f"예시 HTML 생성: {html_path}")
        if not args.no_pdf:
            _to_pdf(html_path, os.path.join(out, "results_report_example.pdf"))
        return 0

    from zusik.storage.portfolio_tracker import PortfolioTracker
    from zusik.reporting.results_html import build_results_summary
    summary = build_results_summary(PortfolioTracker())
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    html_path = write_results_html(summary, paths.reports_path(), generated_at=now)
    print(f"투자결과 리포트 생성: {html_path}")
    print(f"  기간 {summary['period']['start']} ~ {summary['period']['end']} · "
          f"실효수익률 {summary['return_pct']:+.2f}% · 실효순수익 {summary['effective_total']:+,}원 · "
          f"승률 {summary['win_rate']}%")
    if not args.no_pdf:
        _to_pdf(html_path, os.path.join(paths.reports_path(), "results.pdf"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
