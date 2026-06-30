from __future__ import annotations
"""듀얼 모멘텀 전략 (Gary Antonacci 기반).

상대 모멘텀: 여러 코인 중 최근 수익률이 가장 높은 코인 선택.
절대 모멘텀: 선택된 코인의 수익률이 양수일 때만 매수, 음수면 현금 보유.

암호화폐 시장에 맞게 룩백 기간을 7~30일로 단축.
"""

import pandas as pd

from .base import Strategy


class DualMomentumStrategy(Strategy):
    """듀얼 모멘텀 전략.

    단일 코인 분석이 아닌 multi-ticker 분석이 필요하므로,
    analyze()는 단일 코인의 절대 모멘텀만 판단하고,
    코인 선택(상대 모멘텀)은 rank_tickers()로 별도 제공.
    """

    name = "dual_momentum"

    def __init__(self, lookback: int = 14, min_momentum: float = 0.0):
        self.lookback = lookback
        self.min_momentum = min_momentum  # 절대 모멘텀 임계값

    @staticmethod
    def _calc_momentum(df: pd.DataFrame, lookback: int) -> float | None:
        """N일 수익률(모멘텀) 계산."""
        if len(df) < lookback + 1:
            return None
        past_price = df["close"].iloc[-(lookback + 1)]
        curr_price = df["close"].iloc[-1]
        if past_price == 0:
            return None
        return (curr_price - past_price) / past_price

    def analyze(self, df: pd.DataFrame) -> str:
        """절대 모멘텀 판단: 수익률 > 0이면 buy, 아니면 sell."""
        momentum = self._calc_momentum(df, self.lookback)
        if momentum is None:
            return "hold"

        if momentum > self.min_momentum:
            return "buy"
        elif momentum < -self.min_momentum:
            return "sell"
        return "hold"

    @classmethod
    def rank_tickers(
        cls, ticker_data: dict[str, pd.DataFrame], lookback: int = 14, top_n: int = 3
    ) -> list[dict]:
        """여러 코인의 모멘텀을 계산하고 순위를 매김.

        Args:
            ticker_data: {ticker: ohlcv_dataframe} 딕셔너리
            lookback: 모멘텀 계산 기간
            top_n: 상위 N개 코인 반환

        Returns:
            [{"ticker": "KRW-BTC", "momentum": 0.15}, ...] 모멘텀 내림차순
        """
        results = []
        for ticker, df in ticker_data.items():
            momentum = cls._calc_momentum(df, lookback)
            if momentum is not None:
                results.append({"ticker": ticker, "momentum": momentum})

        results.sort(key=lambda x: x["momentum"], reverse=True)
        # 절대 모멘텀 필터: 양수인 것만
        results = [r for r in results if r["momentum"] > 0]
        return results[:top_n]
