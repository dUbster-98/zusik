from __future__ import annotations
import pandas as pd

from .base import Strategy


class RSIStrategy(Strategy):
    """RSI(상대강도지수) 전략.

    RSI가 과매도 구간에 진입하면 매수, 과매수 구간에 진입하면 매도.
    """

    name = "rsi"

    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    @staticmethod
    def _calc_rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.period + 1:
            return "hold"

        rsi = self._calc_rsi(df["close"], self.period)
        current_rsi = rsi.iloc[-1]

        if pd.isna(current_rsi):
            return "hold"

        if current_rsi <= self.oversold:
            return "buy"
        if current_rsi >= self.overbought:
            return "sell"

        return "hold"
