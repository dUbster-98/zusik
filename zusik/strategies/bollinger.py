from __future__ import annotations
import pandas as pd

from .base import Strategy


class BollingerBandStrategy(Strategy):
    """볼린저 밴드 전략.

    가격이 하단 밴드 아래로 내려가면 매수(반등 기대),
    상단 밴드 위로 올라가면 매도(조정 기대).
    """

    name = "bollinger"

    def __init__(self, window: int = 20, num_std: float = 2.0):
        self.window = window
        self.num_std = num_std

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.window:
            return "hold"

        df = df.copy()
        df["ma"] = df["close"].rolling(window=self.window).mean()
        df["std"] = df["close"].rolling(window=self.window).std()
        df["upper"] = df["ma"] + self.num_std * df["std"]
        df["lower"] = df["ma"] - self.num_std * df["std"]

        curr = df.iloc[-1]

        if curr["close"] <= curr["lower"]:
            return "buy"
        if curr["close"] >= curr["upper"]:
            return "sell"

        return "hold"
