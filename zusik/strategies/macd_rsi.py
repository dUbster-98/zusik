from __future__ import annotations
"""MACD + RSI 복합 전략.

MACD 크로스오버로 추세 방향을 잡고, RSI로 과매수/과매도를 필터링.
단일 지표보다 오신호(false signal)를 줄이는 것이 목표.
"""

import pandas as pd

from .base import Strategy


class MACDRSIStrategy(Strategy):
    """MACD + RSI 복합 전략."""

    name = "macd_rsi"

    def __init__(
        self,
        # MACD 파라미터
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        # RSI 파라미터
        rsi_period: int = 14,
        rsi_oversold: float = 35.0,
        rsi_overbought: float = 65.0,
        # 스톱로스
        stop_loss_pct: float = 0.03,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.stop_loss_pct = stop_loss_pct

    @staticmethod
    def _calc_rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _calc_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = df["close"].ewm(span=self.fast_period, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.slow_period, adjust=False).mean()
        df["macd"] = df["ema_fast"] - df["ema_slow"]
        df["macd_signal"] = df["macd"].ewm(span=self.signal_period, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]
        return df

    def analyze(self, df: pd.DataFrame) -> str:
        min_rows = self.slow_period + self.signal_period + 1
        if len(df) < min_rows:
            return "hold"

        df = self._calc_macd(df)
        df["rsi"] = self._calc_rsi(df["close"], self.rsi_period)

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        if pd.isna(curr["rsi"]) or pd.isna(curr["macd_signal"]):
            return "hold"

        # ── 매수 신호 ──
        # MACD 골든크로스 + RSI 과매도 아닌 영역에서 상승 중
        macd_cross_up = prev["macd"] <= prev["macd_signal"] and curr["macd"] > curr["macd_signal"]
        rsi_ok_buy = curr["rsi"] < self.rsi_overbought

        if macd_cross_up and rsi_ok_buy:
            return "buy"

        # ── 매도 신호 ──
        # MACD 데드크로스 또는 RSI 과매수
        macd_cross_down = prev["macd"] >= prev["macd_signal"] and curr["macd"] < curr["macd_signal"]
        rsi_overbought = curr["rsi"] >= self.rsi_overbought

        if macd_cross_down or rsi_overbought:
            return "sell"

        return "hold"
