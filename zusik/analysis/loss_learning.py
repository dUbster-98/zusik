from __future__ import annotations

"""손실측 자가학습 — 손실 컷의 사후 회복 데이터로 hold floor 를 보정.

기존 자가보정(learned_params.json)은 승자 청산 파라미터(profit_ladder/giveback)만 학습했다.
이 모듈은 그 루프를 손실측으로 확장한다. sell_timing(매도 타이밍 사후분석)이 패턴별로
이미 계산해 둔 'net_if_held'(컷 대신 홀드했을 때의 수익률)를 읽어, floor 가 게이트하는
손실 컷이 조기였는지(홀드가 우월) 정당했는지(보호)를 데이터로 가린다.

  - 조기컷(홀드가 우월, net_if_held > 0) → floor 를 더 깊게: 같은 상황을 더 오래 홀드.
  - 정당컷(보호 성공, net_if_held < 0) → floor 를 얕게: 더 빨리 컷.

핵심 안전 원칙: 이 보정은 floor 가 게이트하는 pullback 컷(crash_instant/slow_bleed/quick_loss)
에만 작용한다. 하드스톱(-15%)·deep_collapse(crash_from_high)·forced_stop 은 floor 밖이라
영향을 받지 않는다(자본보호 불변). 결과는 항상 [cap, shallow] 로 클램프하며, cap 은
하드스톱(-15%)보다 얕게 둬 학습이 자본보호 백스톱을 절대 잠식하지 못하게 한다.
"""

# floor 가 실제로 게이트하는 손실 컷 패턴만 학습에 쓴다.
# (forced_stop=하드스톱, breakeven_protect/split_profit/rsi_overbought=익절 → 제외)
FLOOR_GATED_PATTERNS = ("crash_instant", "slow_bleed", "quick_loss", "tick_pullback")


def learn_hold_floor(by_pattern, *, default: float = -0.09,
                     cap: float = -0.13, shallow: float = -0.07,
                     min_count: int = 8, slope: float = 0.2,
                     gated=FLOOR_GATED_PATTERNS) -> dict:
    """sell_timing 패턴 통계로 비핵심 pullback hold floor 를 자가 보정.

    floor 가 게이트하는 손실 패턴들의 net_if_held(%) 를 건수 가중 평균해 floor 를 조정한다.
    표본이 min_count 미만이면 default 를 그대로 둔다(섣부른 보정 방지).

    Args:
        by_pattern: {pattern: {"count": int, "avg_net_if_held": float(% 단위)}} — sell_timing 산출.
        default:    config 기본 floor (보통 -0.09).
        cap:        가장 깊게 허용하는 floor (하드스톱 -0.15 보다 얕게 — 자본보호 잠식 금지).
        shallow:    가장 얕게 허용하는 floor.
        min_count:  보정을 켜는 최소 표본 수.
        slope:      net_if_held → floor 보정 민감도(보수적으로 작게).

    Returns:
        {"floor": float, "weighted_net": float, "n": int, "reason": str}
    """
    rows = [by_pattern[p] for p in gated
            if isinstance(by_pattern.get(p), dict) and by_pattern[p].get("count")]
    n = sum(int(b.get("count", 0)) for b in rows)
    if n < min_count:
        return {"floor": round(default, 4), "weighted_net": 0.0, "n": n,
                "reason": "표본 부족 — default 유지"}

    # net_if_held 는 % 단위(예: +12.2) → 소수로 환산해 가중 평균
    wnet = sum(int(b["count"]) * float(b.get("avg_net_if_held", 0.0)) for b in rows) / n / 100.0
    # wnet>0(홀드가 우월=조기컷) → floor 를 더 깊게(더 음수). wnet<0(정당컷) → 얕게.
    floor = default - wnet * slope
    floor = max(cap, min(shallow, floor))   # 자본보호 백스톱(cap) 절대 잠식 금지
    if wnet > 0.005:
        reason = "조기컷(홀드 우월) — floor 심화"
    elif wnet < -0.005:
        reason = "정당컷(보호 성공) — floor 완화"
    else:
        reason = "중립 — default 부근"
    return {"floor": round(floor, 4), "weighted_net": round(wnet, 4), "n": n, "reason": reason}


# 인버스 빠른익절 학습에 쓰는 패턴(인버스를 +X%에서 익절한 매도).
INVERSE_PROFIT_PATTERNS = ("inverse_take",)


def learn_inverse_quick_profit(by_pattern, *, default: float = 0.015,
                               min_th: float = 0.005, max_th: float = 0.035,
                               min_count: int = 8, slope: float = 0.15,
                               gated=INVERSE_PROFIT_PATTERNS) -> dict:
    """인버스 빠른익절 임계(quick_profit_pct)를 inverse_take 사후데이터로 자가 보정.

    인버스는 감쇠+지수 우상향이라 '+X% 에서 바로 챙기는' 게 수익 전략이다. 고정 임계가 옳은지
    데이터로 검증한다: inverse_take(인버스를 +X%에서 익절) 거래의 net_if_held(컷 대신 홀드했을 때
    수익률, %)를 건수 가중 평균해 임계를 조정한다.
      - net_if_held > 0 (홀드가 더 나았음=너무 빨리 팖) → 임계 상향(다음엔 더 들고 간다).
      - net_if_held < 0 (되돌려져 팔길 잘함) → 임계 하향(더 빨리 챙긴다).
    표본이 min_count 미만이면 default 를 그대로 둔다. 결과는 [min_th, max_th] 로 클램프(런어웨이 방지).

    Returns: {"threshold": float, "weighted_net": float, "n": int, "reason": str}
    """
    rows = [by_pattern[p] for p in gated
            if isinstance(by_pattern.get(p), dict) and by_pattern[p].get("count")]
    n = sum(int(b.get("count", 0)) for b in rows)
    if n < min_count:
        return {"threshold": round(default, 4), "weighted_net": 0.0, "n": n,
                "reason": "표본 부족 — default 유지"}
    wnet = sum(int(b["count"]) * float(b.get("avg_net_if_held", 0.0)) for b in rows) / n / 100.0
    threshold = default + wnet * slope
    threshold = max(min_th, min(max_th, threshold))
    if wnet > 0.005:
        reason = "조기 익절(더 갔음) — 임계 상향"
    elif wnet < -0.005:
        reason = "되돌림(팔길 잘함) — 임계 하향"
    else:
        reason = "중립 — default 부근"
    return {"threshold": round(threshold, 4), "weighted_net": round(wnet, 4), "n": n, "reason": reason}
