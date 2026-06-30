from __future__ import annotations
"""인덱스 추종 모멘텀 전략.

목적: 시장 지수(KOSPI/S&P) 자체의 추세를 신호화. 개별 종목 picker가 아닌
"시장 베타에 올라타는" 전략. 작은 계좌일수록 인덱스 노출이 알파보다 중요.

신호:
  - 20일선 위 + 5일선이 20일선 위 + 최근 N일 모멘텀 양수 → buy
  - 20일선 아래 + 모멘텀 음수 → sell
  - 그 외 → hold

bot.py의 _bullish_regime_score()와 함께 동작:
  - regime_score는 시장 전체 판정용
  - 이 전략은 *해당 종목*(주로 069500/SPY/QQQ)의 추세 진입 타이밍 결정용

소액 계좌(<20만)에서 핵심 가치: 잡 종목 매매보다 인덱스 추세에 베팅 → 거래 회수 ↓ → 수수료/세금 부담 ↓.
"""

import pandas as pd

from .base import Strategy


class IndexMomentumStrategy(Strategy):
    """인덱스 ETF 추종 모멘텀 전략."""

    name = "index_momentum"

    def __init__(self,
                 ma_short: int = 5,
                 ma_long: int = 20,
                 momentum_lookback: int = 14,
                 min_momentum: float = 0.005,
                 invest_ratio: float = 0.95):
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.momentum_lookback = momentum_lookback
        self.min_momentum = min_momentum
        self.invest_ratio = invest_ratio
        self._last_analysis: dict = {}

    def analyze(self, df: pd.DataFrame) -> str:
        """이동평균 정배열 + 양의 모멘텀 → buy.

        역배열 + 음의 모멘텀 → sell.
        """
        if df is None or len(df) < self.ma_long + 1:
            self._last_analysis = {"signal": "hold", "reason": "데이터 부족"}
            return "hold"

        closes = df["close"]
        ma_s = closes.rolling(self.ma_short).mean().iloc[-1]
        ma_l = closes.rolling(self.ma_long).mean().iloc[-1]
        cur = closes.iloc[-1]
        past = closes.iloc[-(self.momentum_lookback + 1)] if len(closes) > self.momentum_lookback else closes.iloc[0]
        mom = (cur - past) / past if past else 0.0

        ma_bull = cur > ma_l and ma_s > ma_l
        ma_bear = cur < ma_l and ma_s < ma_l

        if ma_bull and mom > self.min_momentum:
            signal = "buy"
            reason = f"MA 정배열 + 모멘텀 {mom * 100:+.2f}% > {self.min_momentum * 100:.2f}%"
        elif ma_bear and mom < -self.min_momentum:
            signal = "sell"
            reason = f"MA 역배열 + 모멘텀 {mom * 100:+.2f}% < -{self.min_momentum * 100:.2f}%"
        else:
            signal = "hold"
            reason = (f"중립 (MA bull={ma_bull}, MA bear={ma_bear}, "
                      f"momentum {mom * 100:+.2f}%)")

        self._last_analysis = {
            "signal": signal,
            "reason": reason,
            "ma_short": float(ma_s),
            "ma_long": float(ma_l),
            "momentum": float(mom),
            "current": float(cur),
        }
        return signal

    def calc_position_ratio(self, df: pd.DataFrame) -> float:
        """진입 시 권장 투자 비율. 모멘텀 강도에 따라 0.6~1.0 스케일.

        강한 추세일수록 풀 진입 (작은 계좌에서 분할 무의미).
        """
        if not self._last_analysis or self._last_analysis.get("signal") != "buy":
            return self.invest_ratio
        mom = abs(self._last_analysis.get("momentum", 0.0))
        # 0.5%~5% 모멘텀을 0.6~1.0 비율에 매핑
        scaled = max(0.6, min(1.0, 0.6 + (mom - 0.005) * 8.0))
        return min(self.invest_ratio, scaled)

    def get_last_analysis(self) -> dict:
        return dict(self._last_analysis)
