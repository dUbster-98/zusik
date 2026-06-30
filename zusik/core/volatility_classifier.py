from __future__ import annotations
"""변동성 + 시장 상황 기반 timeframe 자동 선택.

종목별 변동성 점수 + 시장 condition으로 봇이 어떤 봉/실시간 데이터를 사용할지 결정.
LLM/API 비용은 변동성 큰 종목에 집중하고, 안정 종목은 일봉으로 간소화.

Tier:
  low      → 일봉만 (vol < 1.5%, peace)
  medium   → 일봉 + 5분봉
  high     → + 1분봉
  extreme  → + WebSocket 실시간 틱 (vol ≥ 4% 또는 war)
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# vol_20d 임계 (일별 std 기준)
_VOL_LOW = 0.015
_VOL_MEDIUM = 0.025
_VOL_HIGH = 0.040

# 시장 상황 → tier 부스트 (한 단계 위로)
_CONDITION_BOOST = {
    "peace": 0,
    "tension": 1,
    "crisis": 2,
    "war": 3,
}

_TIERS = ["low", "medium", "high", "extreme"]

# tier → 사용 timeframe 매핑
_TIMEFRAME_MAP = {
    "low": ["D"],
    "medium": ["D", "5m"],
    "high": ["D", "5m", "1m"],
    "extreme": ["D", "5m", "1m", "tick"],
}


def classify(df, market_condition: str = "peace",
             holding: bool = False) -> dict[str, Any]:
    """종목 변동성 + 시장 상황으로 timeframe 결정.

    Args:
        df: OHLCV DataFrame (일봉)
        market_condition: peace/tension/crisis/war
        holding: 보유 종목이면 한 단계 더 적극적으로 (extreme까지 올라감)

    Returns:
        {
          "tier": "low"/"medium"/"high"/"extreme",
          "vol_20d": 변동성 비율 (None 가능),
          "timeframes": ["D", "5m", ...],
          "use_minute_5": bool,
          "use_minute_1": bool,
          "use_websocket": bool,
          "reason": str,
        }
    """
    if df is None or len(df) < 20:
        return _default_tier("medium", "데이터 부족 → medium 기본값")

    try:
        returns = df["close"].pct_change().dropna()
        vol_20d = float(returns.tail(20).std()) if len(returns) >= 20 else None
    except Exception:
        vol_20d = None

    if vol_20d is None or vol_20d <= 0:
        return _default_tier("medium", "변동성 산출 실패 → medium 기본값")

    # 변동성 기반 tier
    if vol_20d < _VOL_LOW:
        base_idx = 0
    elif vol_20d < _VOL_MEDIUM:
        base_idx = 1
    elif vol_20d < _VOL_HIGH:
        base_idx = 2
    else:
        base_idx = 3

    boost = _CONDITION_BOOST.get(market_condition, 0)
    # 보유 중이면 추가 +1 (손익 변동 직접 영향 → 더 빠른 반응)
    if holding:
        boost += 1

    final_idx = min(base_idx + boost, len(_TIERS) - 1)
    tier = _TIERS[final_idx]
    timeframes = _TIMEFRAME_MAP[tier]

    return {
        "tier": tier,
        "vol_20d": vol_20d,
        "market_condition": market_condition,
        "holding": holding,
        "timeframes": timeframes,
        "use_minute_5": "5m" in timeframes,
        "use_minute_1": "1m" in timeframes,
        "use_websocket": "tick" in timeframes,
        "reason": (f"vol={vol_20d * 100:.1f}% / cond={market_condition}"
                   f"{' / 보유' if holding else ''} → tier={tier}"),
    }


def _default_tier(tier: str, reason: str) -> dict:
    timeframes = _TIMEFRAME_MAP[tier]
    return {
        "tier": tier,
        "vol_20d": None,
        "timeframes": timeframes,
        "use_minute_5": "5m" in timeframes,
        "use_minute_1": "1m" in timeframes,
        "use_websocket": "tick" in timeframes,
        "reason": reason,
    }
