from __future__ import annotations
"""Upbit 암호화폐 거래 클라이언트.

KR/US 주식 마감 시간 + 주말에 24/7 자동매매.
빈 시간대를 수익 기회로 전환.
"""

import logging
import math

import pyupbit
import pandas as pd

logger = logging.getLogger(__name__)


def _positive_finite(x) -> bool:
    """주문 수량/금액이 정상 양의 유한 실수인지. NaN/inf/음수/0/비수치는 거부."""
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) and x > 0)


class CryptoClient:
    """Upbit 거래소 클라이언트."""

    def __init__(self, access_key: str, secret_key: str):
        self.upbit = pyupbit.Upbit(access_key, secret_key) if access_key else None
        self._enabled = bool(access_key and secret_key)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 시세 ──

    @staticmethod
    def get_current_price(ticker: str) -> dict:
        price = pyupbit.get_current_price(ticker)
        return {"price": float(price or 0), "currency": "KRW"}

    @staticmethod
    def get_ohlcv(ticker: str, interval: str = "day", count: int = 100) -> pd.DataFrame | None:
        """interval: day, minute1, minute3, minute5, minute10, minute15, minute30, minute60, minute240"""
        df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
        if df is None or df.empty:
            return None
        return df

    @staticmethod
    def get_tickers(fiat: str = "KRW") -> list:
        return pyupbit.get_tickers(fiat=fiat)

    # ── 잔고 ──

    def get_balance(self, currency: str = "KRW") -> float:
        if not self._enabled:
            return 0
        return self.upbit.get_balance(currency)

    def get_balances(self) -> list:
        if not self._enabled:
            return []
        return self.upbit.get_balances()

    # ── 주문 ──

    def buy_market(self, ticker: str, amount: float) -> dict:
        """시장가 매수. amount = 원화 금액."""
        if not self._enabled:
            return {"success": False, "message": "API 키 없음"}
        # 하위 관문 fail-closed — 오염된 설정/전략/신호가 만든 비정상 금액 차단.
        if not _positive_finite(amount):
            logger.critical("암호화폐 매수 차단 — 비정상 금액: %r (%s)", amount, ticker)
            return {"success": False, "message": f"비정상 주문 금액 차단: {amount!r}", "blocked": True}
        logger.info("암호화폐 매수: %s %s원", ticker, f"{amount:,.0f}")
        result = self.upbit.buy_market_order(ticker, amount)
        success = isinstance(result, dict) and "uuid" in result
        return {"success": success, "result": result}

    def sell_market(self, ticker: str, volume: float) -> dict:
        """시장가 매도. volume = 코인 수량."""
        if not self._enabled:
            return {"success": False, "message": "API 키 없음"}
        if not _positive_finite(volume):
            logger.critical("암호화폐 매도 차단 — 비정상 수량: %r (%s)", volume, ticker)
            return {"success": False, "message": f"비정상 주문 수량 차단: {volume!r}", "blocked": True}
        logger.info("암호화폐 매도: %s %s개", ticker, volume)
        result = self.upbit.sell_market_order(ticker, volume)
        success = isinstance(result, dict) and "uuid" in result
        return {"success": success, "result": result}

    # ── 24/7 가용 ──

    @staticmethod
    def is_market_open() -> bool:
        """암호화폐는 항상 열려있음."""
        return True

    @staticmethod
    def market_phase() -> str:
        return "open"
