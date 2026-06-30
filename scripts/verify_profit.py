#!/usr/bin/env python3
"""수익률 검증 — 실제 청산 로직(PositionManager)을 합성 가격 시나리오로 돌려
'수익을 실제로 포착하는가 / 손실을 섣불리 확정하지 않는가'를 결정론적으로 검증한다.

CI(Actions)용: KIS API·data/ 없이 오프라인으로 실행. 합성 가격 경로를 record_buy 한
포지션에 흘려보내며 매 스텝 check_surge → update_trailing_stop 를 호출, 첫 청산 신호의
체결가로 실현수익률을 계산한다. 누군가 익절/본전보호를 약화시키거나 hold-floor 를
깨면(손실 컷 부활) 아래 시나리오가 실패한다.

  python3 verify_profit.py        # 통과=exit 0, 회귀=exit 1
"""
from __future__ import annotations

import sys
import os
# scripts/ 이동 — 저장소 루트를 import 경로에 추가 (`import zusik`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import yaml
    from zusik.core.position_manager import PositionManager
except Exception as e:  # pragma: no cover
    print(f"import 실패: {e}")
    sys.exit(2)


def _config() -> dict:
    try:
        with open("config.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _simulate(entry: int, path: list) -> tuple:
    """entry 매수 후 path 가격을 흘리며 첫 청산 신호의 (체결가, 사유)를 반환.

    청산 신호가 없으면 (마지막가, 'no_exit') — '끝까지 보유'.
    매 시나리오마다 독립 PositionManager 로 상태 격리.
    """
    pm = PositionManager(_config())
    code = "VERIFY"
    pm.record_buy(code, "검증종목", 100, entry)
    for px in path:
        px = int(px)
        surge = pm.check_surge(code, px)
        if surge:
            return px, f"surge:{surge.get('action', '익절')}"
        tr = pm.update_trailing_stop(code, px)
        if tr and tr.get("action") in ("stop_triggered", "breakeven_protect"):
            return px, f"trailing:{tr.get('action')}"
    return int(path[-1]), "no_exit"


def main() -> int:
    entry = 10_000
    # (이름, 가격경로, 검증함수, 설명)
    scenarios = [
        (
            "급등 익절 포착",
            [10_800, 11_600, 11_000, 10_200],   # +16% 급등 후 되돌림
            lambda px, why, e: px >= int(e * 1.08) and why != "no_exit",
            "급등(+16%)을 되돌리기 전에 익절해 +8%↑ 포착해야 함 (못 팔면 +2%로 흘러내림)",
        ),
        (
            "본전 보호 (피크 반납 차단)",
            [10_400, 10_800, 10_600, 10_500, 10_300],  # +8% 피크 후 하락
            lambda px, why, e: px >= int(e * 1.03) and why != "no_exit",
            "+8% 피크가 본전으로 흘러내리기 전 +3%↑ 구간에서 보호 매도해야 함",
        ),
        (
            "손실 미실현 (hold-floor)",
            [9_700, 9_500, 9_700, 9_800],   # -5% 딥 후 부분 회복
            lambda px, why, e: why == "no_exit",
            "얕은 손실(-5%)은 컷하지 않고 보유해야 함 (컷 0%승률 교훈 — 손실 확정 금지)",
        ),
    ]

    print("=" * 60)
    print("수익률 검증 — 실제 청산 로직 합성 백테스트")
    print("=" * 60)
    failures = 0
    for name, path, check, desc in scenarios:
        exit_px, why = _simulate(entry, path)
        ret = (exit_px - entry) / entry * 100
        ok = check(exit_px, why, entry)
        mark = "PASS" if ok else "FAIL"
        print(f"\n[{mark}] {name}")
        print(f"      경로 {entry}→{'→'.join(str(int(p)) for p in path)}")
        print(f"      청산: {exit_px} ({ret:+.1f}%) 사유={why}")
        print(f"      기대: {desc}")
        if not ok:
            failures += 1
            print("      회귀 — 수익 포착/보호 로직이 기대대로 동작하지 않음")

    print("\n" + "=" * 60)
    if failures:
        print(f"수익률 검증 실패: {failures}/{len(scenarios)} 시나리오 회귀")
        return 1
    print(f"수익률 검증 통과: {len(scenarios)}/{len(scenarios)} 시나리오")
    return 0


if __name__ == "__main__":
    sys.exit(main())
