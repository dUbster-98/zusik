from __future__ import annotations
import pandas as pd

from .base import Strategy


class MACrossStrategy(Strategy):
    """이동평균 교차(골든크로스/데드크로스) 전략.

    단기 이동평균이 장기 이동평균을 상향 돌파하면 매수,
    하향 돌파하면 매도.
    """

    name = "ma_cross"

    def __init__(self, short_window: int = 5, long_window: int = 20):
        self.short_window = short_window
        self.long_window = long_window

    def analyze(self, df: pd.DataFrame) -> str:
        df = df.copy()
        df["ma_short"] = df["close"].rolling(window=self.short_window).mean()
        df["ma_long"] = df["close"].rolling(window=self.long_window).mean()

        if len(df) < self.long_window + 1:
            return "hold"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # 골든크로스: 단기선이 장기선을 아래→위로 돌파
        if prev["ma_short"] <= prev["ma_long"] and curr["ma_short"] > curr["ma_long"]:
            return "buy"
        # 데드크로스: 단기선이 장기선을 위→아래로 돌파
        if prev["ma_short"] >= prev["ma_long"] and curr["ma_short"] < curr["ma_long"]:
            return "sell"

        return "hold"
