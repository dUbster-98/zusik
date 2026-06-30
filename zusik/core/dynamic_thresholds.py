from __future__ import annotations
"""동적 임계 — 종목 변동성 + 시장 condition + MC 결과로 자동 조정.

기존 고정 임계 (-10% 손절, +10% 익절)는:
  - 저변동 종목엔 너무 늦음 (작은 출렁임도 -10%까지 못 감)
  - 고변동 종목엔 너무 빠름 (자연스러운 변동에 손절 발동)

해법: 종목 20일 변동성을 보고 임계를 자동 스케일링 + 시장 위기 시 단축.

흐름:
    df → 20일 변동성 σ 계산
    σ 기반 base 임계 (저변동 -5% / 고변동 -12%)
    market_condition (peace/tension/crisis/war) 곱셈 보정
    MC 결과 있으면 VaR(95%)와 비교해 보수적 쪽 채택
    → {stop_loss, trailing_stop, target_profit, z_entry, z_exit}
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# 변동성 구간별 base 임계
# (vol_max, stop_loss, trailing_stop, target_profit, z_entry, z_exit)
_VOL_TIERS = [
    (0.015, -0.05, -0.03, 0.05, 1.5, 0.4),   # 저변동: 짧게 진입/청산
    (0.025, -0.08, -0.04, 0.08, 2.0, 0.5),   # 중변동: 표준
    (0.040, -0.10, -0.05, 0.10, 2.0, 0.5),   # 고변동: 표준
    (1.000, -0.12, -0.06, 0.12, 2.5, 0.5),   # 극변동: 보수적 진입
]

# 시장 condition별 곱셈 보정
_MARKET_FACTORS = {
    "peace":   {"stop": 1.0, "target": 1.0},
    "tension": {"stop": 0.8, "target": 0.9},   # 손절 빠르게, 익절 보수
    "crisis":  {"stop": 0.6, "target": 0.7},   # 즉시 컷, 빠른 익절
    "war":     {"stop": 0.5, "target": 0.6},   # 매우 빠른 컷
}


def compute_volatility(df, lookback: int = 20) -> float:
    """일별 raw return 표준편차 (변동성 σ)."""
    if df is None or len(df) < lookback + 1:
        return 0.025  # 기본값 중변동
    try:
        close = df["close"].astype(float).values
        returns = (close[1:] / close[:-1] - 1.0)
        if len(returns) < lookback:
            return 0.025
        import numpy as np
        return float(np.std(returns[-lookback:]))
    except Exception:
        return 0.025


def get_base_thresholds(vol: float) -> dict:
    """변동성 σ로 base 임계 결정 (선형 매핑 4 구간)."""
    for vol_max, stop, trail, target, z_in, z_out in _VOL_TIERS:
        if vol < vol_max:
            return {
                "stop_loss": stop, "trailing_stop": trail, "target_profit": target,
                "z_entry": z_in, "z_exit": z_out, "vol_tier": vol_max,
            }
    last = _VOL_TIERS[-1]
    return {
        "stop_loss": last[1], "trailing_stop": last[2], "target_profit": last[3],
        "z_entry": last[4], "z_exit": last[5], "vol_tier": last[0],
    }


def apply_market_factor(thresholds: dict, market_condition: str = "peace") -> dict:
    """시장 condition으로 임계 보정 (위기일수록 손절 빠르게/익절 보수)."""
    f = _MARKET_FACTORS.get(market_condition, _MARKET_FACTORS["peace"])
    out = dict(thresholds)
    out["stop_loss"] = thresholds["stop_loss"] * f["stop"]
    out["trailing_stop"] = thresholds["trailing_stop"] * f["stop"]
    out["target_profit"] = thresholds["target_profit"] * f["target"]
    out["market_factor"] = f
    return out


def adjust_with_mc(thresholds: dict, mc: Optional[dict]) -> dict:
    """MC VaR(95%)이 base 손절보다 보수적이면 그걸 손절선으로.

    MC가 1만 시뮬로 5% 최악 손실을 추정했으면 그걸 진짜 꼬리 위험.
    base 임계가 그보다 너긋하면 MC 기준으로 단축.
    """
    if not mc:
        return thresholds
    var95 = mc.get("var95", 0)
    if var95 < 0:
        # base보다 더 보수적 (var95가 더 음수)이면 MC 우선
        if var95 < thresholds["stop_loss"]:
            adjusted = max(var95 * 1.05, -0.15)  # 최대 -15%
            thresholds = dict(thresholds)
            thresholds["stop_loss"] = adjusted
            thresholds["mc_adjusted"] = True
    return thresholds


def compute_dynamic_thresholds(df, market_condition: str = "peace",
                                mc: Optional[dict] = None) -> dict:
    """종합: 변동성 + 시장 + MC → 최종 임계.

    Returns:
        {
          "stop_loss": -0.05 ~ -0.15,
          "trailing_stop": -0.03 ~ -0.06,
          "target_profit": +0.04 ~ +0.12,
          "z_entry": 1.5 ~ 2.5,
          "z_exit": 0.4 ~ 0.5,
          "vol": 측정 변동성,
          "vol_tier": 적용된 구간,
        }
    """
    vol = compute_volatility(df)
    base = get_base_thresholds(vol)
    base["vol"] = vol
    market_adjusted = apply_market_factor(base, market_condition)
    final = adjust_with_mc(market_adjusted, mc)
    return final
