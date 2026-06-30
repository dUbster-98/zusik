from __future__ import annotations
"""Claude AI 기반 주식 트레이딩 전략.

Claude가 기술적 지표 + 뉴스/공시/재무 + 변동성 + 시장 동향을
종합 분석하여 매매 신호를 결정.

신호 종류:
  - buy: 단기 매수 (수일~수주 내 매도하여 수익 실현 목표)
  - long_term_buy: 장기 매수 (수개월~수년 보유, 사유 필수 기재)
  - sell: 매도
  - hold: 관망
"""

import logging

import pandas as pd

from .base import Strategy
from zusik.analysis.claude_analyst import ClaudeAnalyst

logger = logging.getLogger(__name__)


class ClaudeStrategy(Strategy):
    """Claude AI 기반 주식 전략."""

    name = "claude"

    def __init__(
        self,
        api_key: str = "",
        model: str = "claude-sonnet-4-20250514",
        use_web_search: bool = True,
        min_confidence: float = 0.6,
        prefer_cli: bool = True,
    ):
        self.analyst = ClaudeAnalyst(api_key=api_key, model=model, prefer_cli=prefer_cli)
        self.use_web_search = use_web_search
        self.min_confidence = min_confidence

        self._last_analysis: dict | None = None
        self._stock_code: str = ""
        self._stock_name: str = ""
        self._portfolio_info: str = ""
        self._long_term_info: str = ""
        self._mc_info: str = ""

    def set_stock(self, stock_code: str, stock_name: str = ""):
        """분석 대상 종목 설정."""
        self._stock_code = stock_code
        self._stock_name = stock_name or stock_code

    def set_context(self, portfolio_info: str = "", long_term_info: str = "",
                     mc_info: str = ""):
        """포트폴리오/장기투자/Monte Carlo 통계 컨텍스트."""
        self._portfolio_info = portfolio_info
        self._long_term_info = long_term_info
        self._mc_info = mc_info

    def analyze(self, df: pd.DataFrame) -> str:
        """Claude AI로 종합 분석 후 매매 신호 반환.

        Returns:
            "buy", "long_term_buy", "sell", "hold" 중 하나
        """
        if not self._stock_code:
            logger.warning("종목코드가 설정되지 않음, hold 반환")
            return "hold"

        # Monte Carlo 통계를 portfolio_info에 합쳐 LLM에 전달
        portfolio_info = self._portfolio_info
        if self._mc_info:
            portfolio_info = f"{portfolio_info} | {self._mc_info}" if portfolio_info else self._mc_info

        analysis = self.analyst.analyze(
            stock_code=self._stock_code,
            stock_name=self._stock_name,
            df=df,
            use_web_search=self.use_web_search,
            portfolio_info=portfolio_info,
            long_term_info=self._long_term_info,
        )
        self._last_analysis = analysis

        if analysis["confidence"] < self.min_confidence:
            logger.info(
                "확신도 %.0f%% < 기준 %.0f%% → hold",
                analysis["confidence"] * 100,
                self.min_confidence * 100,
            )
            return "hold"

        return analysis["signal"]

    def get_invest_ratio(self) -> float:
        if self._last_analysis:
            return self._last_analysis.get("invest_ratio", 0.1)
        return 0.1

    def get_target_price(self) -> int:
        if self._last_analysis:
            return self._last_analysis.get("target_price", 0)
        return 0

    def get_stop_loss(self) -> int:
        if self._last_analysis:
            return self._last_analysis.get("stop_loss", 0)
        return 0

    def get_long_term_reason(self) -> str:
        if self._last_analysis:
            return self._last_analysis.get("long_term_reason", "")
        return ""

    def get_last_analysis(self) -> dict | None:
        return self._last_analysis
