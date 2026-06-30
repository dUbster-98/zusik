from __future__ import annotations
from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    """트레이딩 전략 베이스 클래스."""

    name: str = "base"

    @abstractmethod
    def analyze(self, df: pd.DataFrame) -> str:
        """캔들 데이터를 분석하여 매매 신호를 반환.

        Returns:
            "buy"  - 매수 신호
            "sell" - 매도 신호
            "hold" - 관망
        """
        ...
