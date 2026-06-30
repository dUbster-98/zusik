from __future__ import annotations
"""수정 효과 추적 — baseline(기본 hold-through 모델선택 whitelist) 전/후
신규 거래만으로 효과를 측정. 조기손절(crash/bleed) 비중·손익이 줄고 실현·승률이 개선되는지.

실행: python3 -m zusik.analysis.fix_effect [YYYY-MM-DD]
봇도 매 장마감(post_market)에 같은 리포트를 Discord로 자동 발송 (_send_fix_effect_report).
"""
import sys


def format_report(eff: dict) -> str:
    pre, post = eff["pre"], eff["post"]
    lines = [
        f"수정 효과 추적 (baseline {eff['baseline']}, 신규=baseline 이후)",
        f"{'구간':5} {'매도':>4} {'실현손익':>11} {'승률':>5} {'건당':>8} {'조기손절n(비중)':>14} {'조기손절손익':>11}",
    ]
    for label, g in (("수정전", pre), ("수정후", post)):
        lines.append(
            f"{label:5} {g['n']:>4} {g['pnl']:>+10,} {g['win_rate']*100:>4.0f}% "
            f"{int(g['avg']):>+8,} {g['cut_n']:>3}건({g['cut_share']*100:>3.0f}%) {g['cut_pnl']:>+10,}"
        )
    # 핵심 해석
    if post["n"] == 0:
        lines.append("→ 수정후 신규 매도 없음(표본 0). 거래가 쌓이면 효과가 드러남.")
    else:
        cut_drop = pre["cut_share"] - post["cut_share"]
        lines.append(
            f"→ 조기손절 비중 {pre['cut_share']*100:.0f}% → {post['cut_share']*100:.0f}% "
            f"({cut_drop*100:+.0f}%p). rsi익절 수정후 {post['rsi_n']}건 {post['rsi_pnl']:+,}원."
        )
        lines.append(
            "  (조기손절 비중↓ + 실현/승률↑ = hold-through·모델선택·whitelist 재설계 효과 입증)"
        )
    return "\n".join(lines)


def main():
    from zusik.storage.portfolio_tracker import PortfolioTracker
    baseline = sys.argv[1] if len(sys.argv) > 1 else "2026-06-03"
    eff = PortfolioTracker().get_fix_effect(baseline)
    print(format_report(eff))
    # 패턴 분포 상세
    print("\n── 수정후 패턴 분포 ──")
    for pat, n in sorted(eff["post"]["patterns"].items(), key=lambda x: -x[1]):
        print(f"  {pat:18} {n}건")


if __name__ == "__main__":
    main()
