from __future__ import annotations
"""래리 윌리엄스 변동성 돌파 전략.

전일 변동폭(고가-저가)의 k배를 당일 시가에 더한 가격을 돌파하면 매수,
다음 날 시가에 매도. 주식/암호화폐 모두 적용 가능한 범용 전략.

필터:
  - 이동평균 필터: 현재가 > N일 이동평균일 때만 매수
  - 노이즈 비율 필터: 노이즈가 낮을수록(추세가 강할수록) 매매
"""

import pandas as pd

from .base import Strategy


class VolatilityBreakoutStrategy(Strategy):
    """래리 윌리엄스 변동성 돌파 전략."""

    name = "volatility_breakout"

    def __init__(
        self,
        k: float = 0.5,
        ma_period: int = 5,
        use_ma_filter: bool = True,
        noise_threshold: float = 0.4,
        use_noise_filter: bool = True,
    ):
        self.k = k
        self.ma_period = ma_period
        self.use_ma_filter = use_ma_filter
        self.noise_threshold = noise_threshold
        self.use_noise_filter = use_noise_filter

    @staticmethod
    def _calc_noise(df: pd.DataFrame) -> pd.Series:
        """노이즈 비율 = 1 - |종가-시가| / (고가-저가).
        값이 낮을수록 추세가 강하고, 변동성 돌파에 유리."""
        body = (df["close"] - df["open"]).abs()
        hl_range = df["high"] - df["low"]
        return 1 - (body / hl_range.replace(0, float("nan")))

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < max(self.ma_period, 3):
            return "hold"

        df = df.copy()

        # 전일 변동폭
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        prev_range = prev["high"] - prev["low"]

        # 돌파 목표가 = 당일 시가 + (전일 변동폭 * k)
        target_price = curr["open"] + prev_range * self.k

        # ── 이동평균 필터 ──
        if self.use_ma_filter:
            df["ma"] = df["close"].rolling(window=self.ma_period).mean()
            ma_value = df["ma"].iloc[-1]
            if pd.notna(ma_value) and curr["close"] < ma_value:
                return "hold"

        # ── 노이즈 필터 ──
        if self.use_noise_filter:
            noise = self._calc_noise(df)
            avg_noise = noise.iloc[-20:].mean()  # 최근 20일 평균 노이즈
            if pd.notna(avg_noise) and avg_noise > self.noise_threshold:
                return "hold"

        # ── 돌파 판단 ──
        if curr["close"] >= target_price:
            return "buy"

        # 이미 매수 상태에서 다음 봉 시가에 매도 (일봉 기준)
        # 봇 엔진에서 새 캔들 시작 시 보유 중이면 매도 처리
        return "hold"

    def get_target_price(self, df: pd.DataFrame) -> float | None:
        """현재 캔들 데이터 기반 목표 돌파가 계산."""
        if len(df) < 2:
            return None
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        prev_range = prev["high"] - prev["low"]
        return curr["open"] + prev_range * self.k
