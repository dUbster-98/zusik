from __future__ import annotations
"""자동 하이브리드 전략.

장 상태에 따라 분석 모드를 능동적으로 전환:
  평온 (calm/normal)   → adaptive (로컬 퀀트, 0.1초, 비용 $0)
  급변 (volatile)       → claude quick (2인 경쟁, ~1분)
  위기 (extreme)        → claude full (4인 경쟁, ~2분, 뉴스 필수)
"""

import logging

import pandas as pd

from .base import Strategy
from .adaptive import AdaptiveStrategy

logger = logging.getLogger(__name__)


class AutoHybridStrategy(Strategy):

    name = "auto_hybrid"

    def __init__(
        self,
        api_key: str = "",
        model: str = "claude-sonnet-5",
        use_web_search: bool = True,
        prefer_cli: bool = True,
        backtest_days: int = 120,   #: 30→120 (get_daily_long 250봉 실사용, 모델선택 6개월 평가)
        target_volatility: float = 0.02,
        # Claude 사용량/지연 절감 예산 (config.yaml:strategy 로 조절).
        claude_vol_hold: float = 0.04,          # 보유종목 Claude 트리거 변동성 (2.5%→4%)
        claude_vol_noholding: float = 0.10,     # 미보유 (8%→10%)
        claude_periodic_hold: int = 3600,       # 보유 주기 분석 (30분→1시간)
        claude_periodic_noholding: int = 14400, # 미보유 (2시간→4시간)
        claude_full_only_bleeding: bool = True, # full(4인)은 출혈 시만 (vol≥7%→full 제거)
        claude_websearch_full_only: bool = True,# 웹검색(5분 타임아웃)은 full만, quick은 빠르게
        **kwargs,
    ):
        self._claude_vol_hold = float(claude_vol_hold)
        self._claude_vol_noholding = float(claude_vol_noholding)
        self._claude_periodic_hold = int(claude_periodic_hold)
        self._claude_periodic_noholding = int(claude_periodic_noholding)
        self._claude_full_only_bleeding = bool(claude_full_only_bleeding)
        self._claude_websearch_full_only = bool(claude_websearch_full_only)
        # adaptive (로컬)
        self._adaptive = AdaptiveStrategy(
            backtest_days=backtest_days,
            target_volatility=target_volatility,
        )

        # AI (Claude/Codex/agy) — 있으면 초기화
        self._claude = None
        self._claude_ready = False
        kwargs.get("provider", "auto")
        try:
            from .claude_strategy import ClaudeStrategy
            self._claude = ClaudeStrategy(
                api_key=api_key, model=model,
                use_web_search=use_web_search,
                prefer_cli=prefer_cli,
            )
            self._claude_ready = True
            logger.info("하이브리드: AI 준비 완료")
        except Exception as e:
            logger.info("하이브리드: AI 없음 → adaptive 전용 (%s)", e)

        self._current_mode = "adaptive"
        self._last_analysis = None
        self._cheap_mode = False

    def set_cheap_mode(self, on: bool):
        """방어/급락 모드: Claude(4인 full / quick) 분석을 끄고 로컬 adaptive 로만 판단.

        이 모드에선 신규 매수가 확신 게이트(70%)·현금부족에 막혀 어차피 안 사고, 보유 관리는
        로컬 안전망(급락/트레일링/본전/출혈/하드스톱)이 담당한다. 크래시일수록 보유 종목이 다
        출혈→매 사이클 full 4인 분석 폭증(=비용 폭증)하던 것을 차단. 비싼 AI 분석이 순수 낭비.
        """
        self._cheap_mode = bool(on)

    @property
    def analyst(self):
        """Claude analyst 접근 (봇에서 사용)."""
        if self._claude:
            return self._claude.analyst
        return None

    def set_stock(self, code: str, name: str = ""):
        if self._claude:
            self._claude.set_stock(code, name)

    def set_context(self, **kwargs):
        if self._claude:
            self._claude.set_context(**kwargs)

    def analyze(self, df: pd.DataFrame) -> str:
        """시장 온도에 따라 자동으로 분석 모드 전환."""

        volatility = self._calc_volatility(df)

        # adaptive로 먼저 빠르게 판단
        adaptive_signal = self._adaptive.analyze(df)

        # 포지션 상태 힌트 (bot이 set_position_state로 주입)
        pos_state = getattr(self, "_position_state", {})
        holding_loss = pos_state.get("holding", False) and pos_state.get("profit_rate", 0) < -0.02
        is_bleeding = pos_state.get("is_bleeding", False)

        # Claude가 필요한 경우:
        # 1. 급변 3%+ (손실 보유 중엔 2%+로 민감)
        # 2. adaptive가 buy/sell
        # 3. 주기적 — 손실 3분 / 평시 30분
        # 4. 느린 출혈 감지 시 즉시
        # → 호출되면 항상 4인 풀(full) 사용 (이전 quick=2명 모드 제거).
        # 이유: 사용자가 "왜 미호출?" 질문 — 페어 로테이션은 통계 누적엔 좋지만
        # 매 분석마다 2명이 미호출로 표시되어 직관 위배. 3 CLI 라운드로빈으로
        # 비용은 분산되고, Max 20x 구독이라 부담 작음.
        # v2 Claude 사용량 강력 절감:
        # - 보유 종목만 자주 분석, 미보유는 거의 X
        # - 4인 → 출혈/큰 변동시만, 그 외 quick(2인 페어)
        position_state = getattr(self, "_position_state", {}) or {}
        holding = bool(position_state.get("holding", False))
        # 미보유 종목은 거의 분석 X — vol 임계 초과 또는 강한 buy 시그널만.
        #: 임계/주기를 config 예산으로 상향 → Claude 호출량 절감.
        vol_threshold = (
            self._claude_vol_hold if (holding or holding_loss) else self._claude_vol_noholding
        )
        periodic_interval = (
            self._claude_periodic_hold if (holding or holding_loss)
            else self._claude_periodic_noholding
        )
        # 미보유 종목은 adaptive_signal buy일 때만 Claude 호출 (시그널 명확할 때만)
        needs_claude_extra = (
            (adaptive_signal == "buy") if not holding else False
        )
        # 비용 절감: is_bleeding 을 LLM 트리거에서 제거. 출혈은 로컬 slow_bleed 감지 + hold-floor/
        # 하드스톱이 담당(LLM 판단이 hold-through 설계를 바꾸지 않음). 매 사이클 출혈종목마다
        # LLM 호출하던 토큰 낭비 차단 — 출혈종목도 vol/주기 트리거로만 LLM(여전히 web=full 가능).
        needs_claude = (
            volatility >= vol_threshold or
            needs_claude_extra or
            self._should_periodic_check(periodic_interval)
        )

        if needs_claude and self._claude_ready and not getattr(self, "_cheap_mode", False):
            # full(4인): 출혈 시만: vol≥7%→full 제거로 4인 버스트 절감).
            # claude_full_only_bleeding=false면 기존처럼 변동성 7%+도 full.
            if self._claude_full_only_bleeding:
                level = "full" if is_bleeding else "quick"
            else:
                level = "full" if (is_bleeding or volatility >= 0.07) else "quick"
            claude_signal = self._analyze_claude(df, level, volatility)
            return claude_signal
        else:
            return self._analyze_adaptive(df, volatility)

    def _should_periodic_check(self, interval_sec: int = 900) -> bool:
        """주기적 Claude 분석. 손실 보유 중이면 짧은 간격으로 재판단."""
        from datetime import datetime
        now = datetime.now()
        last = getattr(self, "_last_claude_time", None)
        if last is None or (now - last).total_seconds() > interval_sec:
            return True
        return False

    def set_position_state(self, holding: bool, profit_rate: float = 0.0,
                           is_bleeding: bool = False, peak_profit: float = 0.0):
        """봇이 매 분석 직전 주입: 보유 상태 + 수익률 + 출혈 여부 + 최고 수익."""
        self._position_state = {
            "holding": holding,
            "profit_rate": profit_rate,
            "is_bleeding": is_bleeding,
            "peak_profit": peak_profit,
        }

    def _analyze_adaptive(self, df: pd.DataFrame, volatility: float) -> str:
        if self._current_mode != "adaptive":
            logger.info("전략 전환: %s → adaptive (평온, 변동성 %.1f%%)", self._current_mode, volatility * 100)
            self._current_mode = "adaptive"

        signal = self._adaptive.analyze(df)

        # 로컬에서 목표가/손절가 계산
        price = int(df["close"].iloc[-1])
        atr = 0
        if len(df) >= 15:
            import pandas as _pd
            tr = _pd.concat([
                df["high"] - df["low"],
                (df["high"] - df["close"].shift()).abs(),
                (df["low"] - df["close"].shift()).abs(),
            ], axis=1).max(axis=1)
            atr = int(tr.rolling(14).mean().iloc[-1]) if _pd.notna(tr.rolling(14).mean().iloc[-1]) else 0

        target_price = price + atr * 2 if atr > 0 else int(price * 1.05)
        stop_loss = price - atr if atr > 0 else int(price * 0.93)

        # RSI 기반 확신도
        confidence = 0.5
        try:
            delta = df["close"].diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss
            rsi = (100 - (100 / (1 + rs))).iloc[-1]
            if _pd.notna(rsi):
                if rsi <= 30:
                    confidence = 0.65
                elif rsi >= 70:
                    confidence = 0.60
        except Exception:
            pass

        # 백테스트 검증 점수 기반 확신도 — RSI 스텁만으로는 중립 RSI(30~70)에서 0.5 고정이라
        # 장전 'cautious'(요구 0.55) 날에 로컬 전략 매수가 전면 정지되던 근본 원인
        # (라이브 실측: 후보 전원 "확신도 50% < 요구 55%" 차단 → 거래 전면 포기).
        # adaptive가 방금 선택에 쓴 전략의 검증 점수를 반영. 상향만(max) — 하향은 게이트
        # 강화 부작용이 있어 미적용, 약세 edge는 어차피 기존 0.5 스텁과 동일하게 취급.
        bt_conf = self._backtest_confidence()
        if bt_conf is not None:
            confidence = max(confidence, bt_conf)

        self._last_analysis = {
            "signal": signal,
            "confidence": confidence,
            "invest_ratio": 0.3,
            "target_price": target_price,
            "stop_loss": stop_loss,
            "reasoning": f"[adaptive] 로컬 퀀트 (변동성 {volatility:.1%}, ATR {atr:,})",
            "long_term_reason": "",
            "analyst_details": {},
            "indicators": {},
        }
        return signal

    def _backtest_confidence(self):
        """adaptive 백테스트 종합 점수(수익+샤프+승률 합성) → 확신도.

        승률 단독은 부적합 — 모멘텀 전략은 승률 40%대로 비대칭 수익을 내는 게 정상이라
        (라이브: dual_momentum 승률 44%인데 평균수익 +40%, 점수 0.47) 승률 매핑이면
        검증된 강한 edge도 cautious(0.55)를 못 넘는다. score 0.1→0.55, 0.3→0.65,
        [0.40, 0.70] 클램프. 표본 부족(거래 <5)이면 None(스텁 유지).
        글로벌 검증 전략이 있으면 그 점수, 없으면 종목별 백테스트 1등 점수.
        """
        a = self._adaptive
        try:
            if getattr(a, "_global_best", None) is not None and getattr(a, "_global_scores", None):
                s = a._global_scores[0]
                trades = int(s.get("trades_total", 0))
            elif getattr(a, "_last_scores", None):
                s = a._last_scores[0]
                trades = int(s.get("trades", 0))
            else:
                return None
            if trades < 5:
                return None
            return max(0.40, min(0.70, 0.5 + float(s.get("score", 0.0)) * 0.5))
        except Exception:
            return None

    def _analyze_claude(self, df: pd.DataFrame, level: str, volatility: float) -> str:
        if not self._claude_ready:
            return self._analyze_adaptive(df, volatility)

        from datetime import datetime
        self._last_claude_time = datetime.now()

        mode_label = "claude_full" if level == "full" else "claude_quick"
        if self._current_mode != mode_label:
            logger.info("전략 전환: %s → %s (변동성 %.1f%%)", self._current_mode, mode_label, volatility * 100)
            self._current_mode = mode_label

        #: 웹검색(5분 타임아웃)은 full(이벤트성)에서만. quick은 웹검색 끄고
        # 빠르게(~30s) — "분석이 너무 오래 걸려 Claude 사용량 폭증" 문제 직접 해결.
        if self._claude_websearch_full_only and self._claude is not None:
            self._claude.use_web_search = (level == "full")

        # 선별 호출 설정 + 확신도 기준 조정
        if level == "quick":
            # 4명에게 통계 공평하게 누적되도록 페어 로테이션.
            # 이전엔 (quant, generalist) 고정 → fundamental/sentiment가 거의 안 도는
            # 부작용. analyst_performance.json이 비어 있던 진짜 원인 중 하나.
            # vol 1~3% 구간이 디폴트라 quick이 자주 발동하므로 로테이션으로
            # 모든 애널리스트가 비슷한 호출 빈도를 갖도록 함.
            pairs = [["quant", "generalist"], ["fundamental", "sentiment"]]
            idx = getattr(self, "_quick_rotation_idx", 0) % len(pairs)
            self._claude.analyst._selected_roles = pairs[idx]
            self._quick_rotation_idx = idx + 1
            logger.info("claude_quick 페어 로테이션: %s", pairs[idx])
            self._claude.min_confidence = 0.20  # 2명만 호출 → 합산 확신도 낮으니 기준도 낮춤
        else:
            self._claude.analyst._selected_roles = None  # 4인 전체
            self._claude.min_confidence = 0.30  # 4인이면 합산이 더 낮을 수 있음

        # set_stock이 호출됐는지 확인
        if not self._claude._stock_code:
            logger.warning("auto_hybrid: Claude에 종목코드 미전달, adaptive 폴백")
            return self._analyze_adaptive(df, volatility)

        signal = self._claude.analyze(df)
        self._last_analysis = self._claude.get_last_analysis()
        return signal

    def get_last_analysis(self):
        return self._last_analysis

    def get_invest_ratio(self) -> float:
        if self._last_analysis:
            return self._last_analysis.get("invest_ratio", 0.3)
        return 0.3

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

    @staticmethod
    def _calc_volatility(df: pd.DataFrame) -> float:
        """최근 변동성 계산 (rolling std of returns)."""
        if df is None or len(df) < 3:
            return 0
        returns = df["close"].pct_change().dropna()
        if len(returns) < 2:
            return 0
        window = min(20, len(returns))
        return float(returns.iloc[-window:].std())
