from __future__ import annotations
"""모멘텀 돌파 전략 (Momentum Breakout).

핵심 아이디어:
  - N일 고점 돌파 + 거래량 폭증 = 추세 시작 신호 → 공격 진입
  - 돌파 후 되돌림 시 보유 유지 (ATR 손절 기준)
  - 모멘텀 약화 시 매도 (상위 종목 이탈 방지)

파라미터:
  lookback: N일 고점 기준 (기본 20일)
  volume_mult: 거래량 배수 임계 (기본 2.0배)
  momentum_min: 모멘텀 점수 하한 (-1 ~ +1, 기본 0.2)

VCK5000 확장 지점:
  indicators.batch_* 함수를 쓰면 수백 종목을 FPGA에서 한 번에 스캔 가능.
  _scan_universe() 메서드에서 batch 호출로 최적 후보 선별하는 구조로 확장.
"""

import logging

import pandas as pd

from .base import Strategy
from zusik.analysis.indicators import breakout_signal, volume_surge, momentum_score, atr

logger = logging.getLogger(__name__)


class MomentumBreakoutStrategy(Strategy):

    name = "momentum_breakout"

    def __init__(
        self,
        lookback: int = 20,
        volume_mult: float = 2.0,
        momentum_min: float = 0.2,
        stop_atr_mult: float = 2.0,
        **kwargs,
    ):
        self.lookback = lookback
        self.volume_mult = volume_mult
        self.momentum_min = momentum_min
        self.stop_atr_mult = stop_atr_mult
        self._last_analysis: dict | None = None

    def analyze(self, df: pd.DataFrame) -> str:
        if df is None or len(df) < self.lookback + 5:
            return "hold"

        price = float(df["close"].iloc[-1])
        bk = breakout_signal(df, self.lookback)
        vs = volume_surge(df, window=self.lookback, threshold=self.volume_mult)
        mom = momentum_score(df)
        a = atr(df)

        # 손절선: 현재가 - ATR * 배수
        stop = price - a * self.stop_atr_mult if a > 0 else price * 0.93
        # 목표가: 돌파 시 상방 무제한이지만 ATR*3를 1차 목표로
        target = price + a * 3 if a > 0 else price * 1.10

        # ── 매수 조건: 돌파 + 거래량 + 모멘텀 ──
        buy = bk["is_breakout"] and vs["is_surge"] and mom >= self.momentum_min
        # ── 매도 조건: 모멘텀 약화 + 고점 대비 -5% ──
        recent_high = float(df["high"].iloc[-10:].max())
        from_high = (price - recent_high) / recent_high if recent_high > 0 else 0
        sell = mom <= -0.2 or from_high <= -0.05

        signal = "buy" if buy else ("sell" if sell else "hold")

        confidence = 0.5
        if buy:
            # 확신도: 모멘텀 점수 + 거래량 배수 가중
            confidence = min(0.95, 0.5 + abs(mom) * 0.3 + min(vs["ratio"] / 5, 0.2))

        self._last_analysis = {
            "signal": signal,
            "confidence": round(confidence, 2),
            "invest_ratio": 0.5 if buy else 0.3,
            "target_price": int(target),
            "stop_loss": int(stop),
            "reasoning": (
                f"[momentum_breakout] 돌파={bk['is_breakout']}({bk['distance_pct']:+.1%}) "
                f"거래량={vs['ratio']:.1f}x 모멘텀={mom:+.2f} ATR={a:.2f}"
            ),
            "long_term_reason": "",
            "analyst_details": {},
            "indicators": {
                "prior_high": bk["prior_high"],
                "volume_ratio": vs["ratio"],
                "momentum": mom,
                "atr": a,
                "from_recent_high": from_high,
            },
        }
        return signal

    def get_last_analysis(self):
        return self._last_analysis

    def get_invest_ratio(self) -> float:
        if self._last_analysis:
            return self._last_analysis.get("invest_ratio", 0.3)
        return 0.3

    def get_target_price(self) -> int:
        if self._last_analysis:
            return self._last_analysis.get("target_price", 0)
        return 0

    def get_stop_loss(self) -> int:
        if self._last_analysis:
            return self._last_analysis.get("stop_loss", 0)
        return 0
