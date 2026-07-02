#!/usr/bin/env python3
"""진입 사유(entry reason) 버킷별 실현손익 집계.

잔금소진/강제매수/수동/일반 진입이 실제로 +EV인지 빠르게 확인하는 도구.
집계 로직은 PortfolioTracker.get_entry_bucket_stats 공용(월간 리포트와 동일).

사용: python3 scripts/entry_roi.py [--days 30]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zusik.storage.portfolio_tracker import PortfolioTracker  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="최근 N일만 (0=전체)")
    args = ap.parse_args()

    stats = PortfolioTracker().get_entry_bucket_stats(days=args.days or None)

    span = f"최근 {args.days}일" if args.days else "전체"
    print(f"진입 버킷별 실현손익 ({span})")
    print(f"{'bucket':10} {'n':>5} {'승률':>6} {'총손익':>14} {'건당':>12}")
    for b, s in sorted(stats.items(), key=lambda x: -x[1]["n"]):
        n, w, p = s["n"], s["wins"], s["pnl"]
        print(f"{b:10} {n:5d} {w / n * 100 if n else 0:5.0f}% {p:14,.0f} {p / n if n else 0:12,.0f}")


if __name__ == "__main__":
    main()
