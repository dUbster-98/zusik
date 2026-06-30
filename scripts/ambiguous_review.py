#!/usr/bin/env python3
"""모호 케이스 LLM 라우팅(pop-then-fade 익절 타이브레이크) 효과 누적 리뷰.

`ai_routing` 으로 추가된 익절 타이브레이크가 실제로 돈을 버는지(승률·건당·총 실현손익)를
EOD 단발이 아니라 **누적**으로 본다. `ambiguous_take` 패턴을 다른 익절 패턴(실증 100% 승률인
split_profit/rsi_overbought)·전체 매도와 나란히 놓아 판단을 돕는다.

  python3 scripts/ambiguous_review.py            # 전체 기간 누적
  python3 scripts/ambiguous_review.py --days 7   # 최근 7일

읽기 전용(data/trades.json). 매매 동작을 바꾸지 않는다. ExecStartPre/CI 대상 아님.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description="모호 익절 라우팅 효과 누적 리뷰")
    ap.add_argument("--days", type=int, default=None, help="최근 N일 (기본: 전체 기간)")
    args = ap.parse_args()

    from zusik.storage.portfolio_tracker import PortfolioTracker
    stats = PortfolioTracker().get_pattern_stats(days=args.days)

    period = f"최근 {args.days}일" if args.days else "전체 기간"
    print(f"모호 익절 라우팅(ambiguous_take) 효과 리뷰 — {period}")
    print("=" * 72)

    amb = stats.get("ambiguous_take")
    if not amb or amb["count"] == 0:
        print("아직 ambiguous_take 매도가 없습니다 — 배포 직후이거나 모호(pop-then-fade) 구간 미발생.")
        print("며칠 더 돌린 뒤 다시 확인하세요. 일일 현황은 봇의 Discord EOD 패턴 리포트에도 표시됩니다.")
        return 0

    def _line(tag: str, s: dict) -> str:
        return (f"  {tag:<16} 건수 {s['count']:>3} · 승률 {s['win_rate']:>3.0f}% · "
                f"건당 {s['avg_pnl']:>+10,.0f}원 · 평균 {s['avg_pct']:>+5.2f}% · "
                f"총 {s['pnl_sum']:>+12,.0f}원")

    print(_line("ambiguous_take", amb))
    print("\n비교(익절 계열 · 실증 100% 승률 패턴):")
    for tag in ("split_profit", "rsi_overbought"):
        if tag in stats:
            print(_line(tag, stats[tag]))

    total_pnl = sum(s["pnl_sum"] for s in stats.values())
    total_n = sum(s["count"] for s in stats.values())
    print(f"\n전체 매도: 건수 {total_n} · 총 실현 {total_pnl:+,.0f}원 · 패턴 {len(stats)}종")

    print("\n해석:")
    if amb["count"] < 5:
        print(f"  표본 {amb['count']}건 — 아직 판단하기 이름. 더 쌓인 뒤 재평가.")
    elif amb["pnl_sum"] > 0 and amb["win_rate"] >= 60:
        print("  승률·총손익 양호 → 라우팅이 본전 흘림을 잡고 있음. 유지 권장.")
    elif amb["pnl_sum"] <= 0:
        print("  총손익 음(-) → 효과 의문. 밴드/임계(hold_score_hi·min_giveback·take_min_conf) "
              "재검토 또는 ai_routing.ambiguous_sell_enabled=false 검토.")
    else:
        print("  중립 — 승률은 낮지만 손익은 +. 표본 더 보고 임계 미세조정 고려.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
