#!/usr/bin/env python3
"""월간 성과 요약 HTML 생성 (온디맨드).

봇은 매달 마지막 날 자동 생성하지만, 이 CLI로 아무 달이나 즉시 뽑을 수 있다.

  python3 scripts/monthly_report.py                    # 이번 달 → reports/monthly/{YYYY-MM}.html
  python3 scripts/monthly_report.py --year 2026 --month 5
  python3 scripts/monthly_report.py --example          # 샘플 데이터로 docs/examples/ 예시 생성

읽기 전용(data/equity_curve.json). 매매 동작을 바꾸지 않는다. ExecStartPre/CI 대상 아님.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 예시 HTML 용 가상 데이터 — 실제 계좌/보유종목과 무관한 더미값(레이아웃 확인/공유용)
_EXAMPLE_STATS = {
    "month": "2024-01",
    "days": 21,
    "start_equity": 10_000_000,
    "end_equity": 10_520_000,
    "deposits": 0,
    "realized": 372_000,
    "net_growth": 520_000,
    "return_pct": 5.20,
    "max_drawdown": -3.40,
    "basis": "effective",
    "by_stock": [
        {"name": "예시 종목 A", "code": "000001", "count": 5, "wins": 5, "pnl": 210_000},
        {"name": "예시 종목 B", "code": "000002", "count": 3, "wins": 2, "pnl": 120_000},
        {"name": "Example Co", "code": "DEMO", "count": 4, "wins": 3, "pnl": 80_000},
        {"name": "예시 종목 C", "code": "000003", "count": 2, "wins": 0, "pnl": -38_000},
    ],
    "by_pattern": [
        {"pattern": "rsi_overbought", "count": 6, "wins": 6, "pnl": 240_000},
        {"pattern": "split_profit", "count": 4, "wins": 4, "pnl": 150_000},
        {"pattern": "breakeven_protect", "count": 3, "wins": 2, "pnl": 20_000},
        {"pattern": "crash_instant", "count": 1, "wins": 0, "pnl": -38_000},
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser(description="월간 성과 요약 HTML 생성")
    ap.add_argument("--year", type=int, help="연도 (기본: 올해)")
    ap.add_argument("--month", type=int, help="월 (기본: 이번 달)")
    ap.add_argument("--example", action="store_true",
                    help="샘플 데이터로 docs/examples/monthly_report_example.html 생성")
    ap.add_argument("--no-pdf", action="store_true", help="HTML 만 (PDF 변환 생략)")
    args = ap.parse_args()

    from zusik.reporting.monthly_html import render_monthly_html, write_monthly_html
    from zusik import paths

    if args.example:
        out = os.path.join(str(paths.ROOT), "docs", "examples")
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, "monthly_report_example.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_monthly_html(_EXAMPLE_STATS, generated_at="(예시 — 샘플 데이터)"))
        print(f"예시 HTML 생성: {path}")
        return 0

    now = datetime.now()
    year = args.year or now.year
    month = args.month or now.month

    from zusik.storage.portfolio_tracker import PortfolioTracker
    stats = PortfolioTracker().get_monthly_stats(year, month)
    if stats.get("days", 0) == 0:
        print(f"{year:04d}-{month:02d}: equity 기록 없음 — 리포트를 만들 데이터가 없습니다.")
        return 1

    path = write_monthly_html(stats, paths.reports_path("monthly"),
                              generated_at=now.strftime("%Y-%m-%d %H:%M"))
    print(f"월간 HTML 리포트 생성: {path}")
    print(f"  {stats['month']} · 수익률 {stats['return_pct']:+.2f}% · "
          f"실현 {stats['realized']:+,}원 · 최대DD {stats['max_drawdown']:+.2f}%")
    if not args.no_pdf:
        from zusik.reporting.pdf import html_to_pdf, pdf_backend
        if pdf_backend():
            out = html_to_pdf(path, path[:-5] + ".pdf")
            print(f"  PDF: {out}" if out else "  PDF 변환 실패 — HTML 은 정상")
        else:
            print("  (PDF 백엔드 없음 — Chrome/Chromium/wkhtmltopdf 설치 시 PDF 자동)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
