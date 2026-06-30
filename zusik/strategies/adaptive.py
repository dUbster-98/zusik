from __future__ import annotations
"""적응형 전략 선택기.

모든 전략을 최근 N일 데이터로 간이 백테스트하고,
수익률이 가장 높은 전략을 자동 선택하여 실행.
변동성 기반 포지션 사이징도 함께 적용.

작동 원리:
  1. 매 실행 시 모든 후보 전략을 최근 데이터로 시뮬레이션
  2. 전략별 수익률·샤프비율 계산
  3. 종합 점수가 가장 높은 전략 선택
  4. ATR 기반 변동성 타겟팅으로 투자 비율 조절
"""

import logging

import numpy as np
import pandas as pd

from .base import Strategy
from .volatility_breakout import VolatilityBreakoutStrategy
from .dual_momentum import DualMomentumStrategy
from .macd_rsi import MACDRSIStrategy
from .ma_cross import MACrossStrategy
from .rsi import RSIStrategy
from .bollinger import BollingerBandStrategy
from .momentum_breakout import MomentumBreakoutStrategy

logger = logging.getLogger(__name__)


class AdaptiveStrategy(Strategy):
    """적응형 전략: 백테스트 기반 자동 전략 선택 + 변동성 포지션 사이징."""

    name = "adaptive"

    def __init__(
        self,
        backtest_days: int = 30,
        target_volatility: float = 0.02,
        vol_lookback: int = 20,
        reselect_interval: int = 1,
    ):
        """
        Args:
            backtest_days: 백테스트에 사용할 최근 일수
            target_volatility: 목표 일일 변동성 (기본 2%)
            vol_lookback: 변동성 계산 기간
            reselect_interval: 전략 재선택 간격 (실행 횟수 기준)
        """
        self.backtest_days = backtest_days
        self.target_volatility = target_volatility
        self.vol_lookback = vol_lookback
        self.reselect_interval = reselect_interval

        # 후보 전략들
        self.candidates: list[Strategy] = [
            VolatilityBreakoutStrategy(k=0.5),
            VolatilityBreakoutStrategy(k=0.4),
            DualMomentumStrategy(lookback=7),
            DualMomentumStrategy(lookback=14),
            MACDRSIStrategy(),
            MACDRSIStrategy(fast_period=5, slow_period=13, signal_period=4, rsi_period=7),
            MACrossStrategy(short_window=5, long_window=20),
            MACrossStrategy(short_window=10, long_window=40),
            RSIStrategy(period=14, oversold=30, overbought=70),
            BollingerBandStrategy(window=20, num_std=2.0),
            MomentumBreakoutStrategy(lookback=20, volume_mult=2.0),
            MomentumBreakoutStrategy(lookback=10, volume_mult=1.5, momentum_min=0.1),
        ]

        self._selected: Strategy | None = None
        self._run_count = 0
        self._last_scores: list[dict] = []
        # 글로벌 백테스트 결과: 다종목 풀에서 통계적으로 검증된 1등 전략
        self._global_best: Strategy | None = None
        self._global_scores: list[dict] = []

    def _backtest_window(self, df: pd.DataFrame) -> pd.DataFrame:
        """백테스트/전략선택에 사용할 최근 구간만 잘라낸다.

        최근 `backtest_days` 구간만 성과 비교에 반영하되, 지표 초기화를 위한
        최소 워밍업 봉은 남겨 둔다.
        """
        if df is None or df.empty:
            return df

        warmup_bars = 30
        max_lookback = max(
            40,  # MA(10,40)
            self.vol_lookback,
            *(getattr(s, "lookback", 0) for s in self.candidates),
            *(getattr(s, "long_window", 0) for s in self.candidates),
            *(getattr(s, "window", 0) for s in self.candidates),
            *(getattr(s, "slow_period", 0) for s in self.candidates),
            *(getattr(s, "rsi_period", 0) for s in self.candidates),
            *(getattr(s, "period", 0) for s in self.candidates),
        )
        keep = max(self.backtest_days + warmup_bars, max_lookback + warmup_bars)
        if len(df) <= keep:
            return df
        return df.iloc[-keep:].copy()

    # ── 간이 백테스트 (5/1 정정: look-ahead bias 제거 + 수수료 현실화) ──

    # 왕복 수수료 (KR ≈ 0.23%, US ≈ 0.18%). 보수적으로 0.15% 단방향 통일
    _SIMULATE_FEE_RATE = 0.0015
    # 표본 5건 미만이면 통계적 의미 없음 → 점수에서 사실상 제외
    _MIN_TRADES_FOR_SCORING = 5

    @staticmethod
    def _simulate(strategy: Strategy, df: pd.DataFrame) -> dict:
        """전략 백테스트.

        주의 (5/1 수정):
        - 신호는 i-1까지의 데이터만 사용 (look-ahead 차단)
        - 체결가는 i의 시가 (다음 봉 진입). 시가 없으면 close 폴백
        - 수수료 0.05% → 0.15% (실전 왕복 0.23~0.30% 기준)
        """
        if len(df) < 31:
            return {"return": 0, "sharpe": 0, "win_rate": 0, "trades": 0}

        fee_rate = AdaptiveStrategy._SIMULATE_FEE_RATE
        capital = 1_000_000
        position = 0.0
        entry_price = 0.0
        trade_returns = []
        equity_curve = []  # 일별 자산 (sharpe 산출용)

        has_open = "open" in df.columns

        # i 시점에서 분석은 i-1까지의 데이터로, 체결은 i의 시가
        for i in range(30, len(df)):
            window = df.iloc[:i]  # i 미포함 → i-1까지 (look-ahead 차단)
            signal = strategy.analyze(window)
            exec_price = float(df["open"].iloc[i]) if has_open else float(df["close"].iloc[i])

            if signal == "buy" and position == 0 and capital > 0 and exec_price > 0:
                fee = capital * fee_rate
                position = (capital - fee) / exec_price
                entry_price = exec_price
                capital = 0

            elif signal == "sell" and position > 0:
                proceeds = position * exec_price
                fee = proceeds * fee_rate
                capital = proceeds - fee
                ret = (exec_price - entry_price) / entry_price
                # 왕복 수수료 차감해 실현수익률 보정
                ret -= 2 * fee_rate
                trade_returns.append(ret)
                position = 0
                entry_price = 0

            # 일별 mark-to-market 자산
            mark_price = float(df["close"].iloc[i])
            equity = capital + position * mark_price
            equity_curve.append(equity)

        # 미청산 포지션 정리 (마지막 close에 청산)
        if position > 0:
            final_price = float(df["close"].iloc[-1])
            capital = position * final_price * (1 - fee_rate)
            ret = (final_price - entry_price) / entry_price - 2 * fee_rate
            trade_returns.append(ret)

        total_return = (capital / 1_000_000) - 1 if capital > 0 else -1
        win_rate = (sum(1 for r in trade_returns if r > 0) / len(trade_returns)
                    if trade_returns else 0)

        # 샤프 비율: 일별 자산 변화율 기준 (거래 수익률 아닌 일별 수익률)
        sharpe = 0.0
        if len(equity_curve) > 5:
            eq = pd.Series(equity_curve)
            daily_ret = eq.pct_change().dropna()
            if len(daily_ret) > 0 and daily_ret.std() > 0:
                # 연환산 (252일) 샤프
                sharpe = (daily_ret.mean() / daily_ret.std()) * (252 ** 0.5)

        return {
            "return": total_return,
            "sharpe": sharpe,
            "win_rate": win_rate,
            "trades": len(trade_returns),
        }

    def select_best_strategy(self, df: pd.DataFrame) -> Strategy:
        """모든 후보 전략을 백테스트하고 최적 전략 반환."""
        test_df = self._backtest_window(df)
        scores = []
        for strategy in self.candidates:
            result = self._simulate(strategy, test_df)
            # 종합 점수 (5/1 재설계):
            # - 수익률 50% (실현수익률, 수수료 차감 후)
            # - 샤프 30% (연환산. tanh로 [-1, 1] 클램프해 단위 통일)
            # - 승률 20%
            # 표본 5건 미만이면 점수 -1.0 (사실상 제외) — 1회 거래 운으로 1등 뽑히는 결함 차단
            sharpe_normalized = float(np.tanh(result["sharpe"] / 2.0))  # ~[-1, 1]
            score = (
                result["return"] * 0.5
                + sharpe_normalized * 0.3
                + result["win_rate"] * 0.2
            )
            if result["trades"] < self._MIN_TRADES_FOR_SCORING:
                score -= 1.0  # 표본 부족 페널티
            scores.append({
                "strategy": strategy,
                "name": f"{strategy.name}",
                "score": score,
                **result,
            })

        scores.sort(key=lambda x: x["score"], reverse=True)
        self._last_scores = scores

        # 결과 로깅
        logger.info("── 전략 백테스트 결과 (최근 %d일, 사용봉 %d) ──", self.backtest_days, len(test_df))
        for i, s in enumerate(scores[:5]):
            logger.info(
                "  %d. %-25s | 수익률: %+6.1f%% | 샤프: %+.2f | 승률: %.0f%% | 거래: %d회 | 점수: %.3f",
                i + 1, s["name"], s["return"] * 100, s["sharpe"],
                s["win_rate"] * 100, s["trades"], s["score"],
            )

        best = scores[0]["strategy"]
        logger.info("▶ 선택된 전략: %s (점수: %.3f)", scores[0]["name"], scores[0]["score"])
        return best

    # ── 글로벌 다종목 백테스트 (5/1 추가) ──

    def select_best_strategy_from_pool(self, stocks_dfs: dict) -> Strategy | None:
        """다종목 OHLCV로 12전략을 백테스트하고 종목 평균 점수 1등 반환.

        Args:
            stocks_dfs: {code: pd.DataFrame} — 종목별 OHLCV
        Returns:
            전략 평균 점수 1등 (표본 종목 5개 미만이면 None)
        """
        if not stocks_dfs or len(stocks_dfs) < 5:
            logger.warning("글로벌 백테스트 스킵: 표본 종목 %d (필요 ≥5)",
                           len(stocks_dfs) if stocks_dfs else 0)
            return None

        # 전략별 누적 통계
        strategy_stats = {i: {"strategy": s, "name": s.name, "returns": [],
                              "sharpes": [], "win_rates": [], "trades_total": 0,
                              "stocks": 0}
                          for i, s in enumerate(self.candidates)}

        for code, df in stocks_dfs.items():
            if df is None or len(df) < 31:
                continue
            test_df = self._backtest_window(df)
            for i, s in enumerate(self.candidates):
                result = self._simulate(s, test_df)
                # 표본 5건 이상인 종목-전략 조합만 누적
                if result["trades"] >= self._MIN_TRADES_FOR_SCORING:
                    strategy_stats[i]["returns"].append(result["return"])
                    strategy_stats[i]["sharpes"].append(result["sharpe"])
                    strategy_stats[i]["win_rates"].append(result["win_rate"])
                    strategy_stats[i]["trades_total"] += result["trades"]
                    strategy_stats[i]["stocks"] += 1

        # 종목 평균 점수 산출 (표본 종목 ≥3 이어야 인정)
        scores = []
        for i, st in strategy_stats.items():
            if st["stocks"] < 3:
                continue
            import numpy as np_ref  # 함수 스코프 별칭 (모듈 충돌 방지)
            avg_return = float(np_ref.mean(st["returns"]))
            avg_sharpe = float(np_ref.mean(st["sharpes"]))
            avg_win = float(np_ref.mean(st["win_rates"]))
            sharpe_norm = float(np_ref.tanh(avg_sharpe / 2.0))
            score = avg_return * 0.5 + sharpe_norm * 0.3 + avg_win * 0.2
            scores.append({
                "name": st["name"],
                "strategy": st["strategy"],
                "score": score,
                "avg_return": avg_return,
                "avg_sharpe": avg_sharpe,
                "avg_win": avg_win,
                "stocks": st["stocks"],
                "trades_total": st["trades_total"],
            })

        if not scores:
            logger.warning("글로벌 백테스트: 통계적으로 의미 있는 전략 없음 (모든 후보가 종목 3개 미만 표본)")
            return None

        scores.sort(key=lambda x: x["score"], reverse=True)
        self._global_scores = scores
        self._global_best = scores[0]["strategy"]

        logger.info("══ 글로벌 백테스트 (종목 %d개) ══", len(stocks_dfs))
        for i, s in enumerate(scores[:5]):
            logger.info(
                "  %d. %-25s | 평균수익 %+6.1f%% | 평균샤프 %+5.2f | 평균승률 %.0f%% "
                "| 종목 %d | 거래 %d건 | 점수 %.3f",
                i + 1, s["name"], s["avg_return"] * 100, s["avg_sharpe"],
                s["avg_win"] * 100, s["stocks"], s["trades_total"], s["score"],
            )
        logger.info("▶ 글로벌 1등 전략: %s (점수 %.3f, 종목 %d개 검증)",
                    scores[0]["name"], scores[0]["score"], scores[0]["stocks"])
        return self._global_best

    # ── 변동성 기반 포지션 사이징 ──

    def calc_position_ratio(self, df: pd.DataFrame) -> float:
        """ATR 기반 변동성 타겟팅으로 투자 비율 계산.

        Returns:
            0.0 ~ 1.0 사이의 투자 비율.
            변동성이 높으면 비율↓, 낮으면 비율↑.
        """
        if len(df) < self.vol_lookback:
            return 0.5  # 데이터 부족 시 50%

        # 실현 변동성 = 최근 N일 일별 수익률의 표준편차
        returns = df["close"].pct_change().dropna()
        recent_returns = returns.iloc[-self.vol_lookback:]
        realized_vol = recent_returns.std()

        if realized_vol <= 0 or np.isnan(realized_vol):
            return 0.5

        ratio = self.target_volatility / realized_vol
        ratio = min(max(ratio, 0.05), 1.0)  # 5% ~ 100% 클램프

        logger.info(
            "변동성 포지션 사이징: 실현변동성=%.3f, 목표=%.3f → 투자비율=%.1f%%",
            realized_vol, self.target_volatility, ratio * 100,
        )
        return ratio

    # ── Strategy 인터페이스 ──

    def analyze(self, df: pd.DataFrame) -> str:
        """적응형 분석: 전략 자동 선택 후 해당 전략의 신호 반환.

        우선순위:
          1. 글로벌 백테스트로 검증된 전략 (다종목 검증, _global_best)
          2. 종목별 단독 백테스트 1등 (fallback)
        """
        self._run_count += 1

        # 글로벌 검증 전략이 있으면 우선 사용 (5/1 추가)
        if self._global_best is not None:
            return self._global_best.analyze(df)

        # Fallback: 종목별 단독 백테스트
        if self._selected is None or self._run_count % self.reselect_interval == 0:
            self._selected = self.select_best_strategy(df)

        return self._selected.analyze(df)
