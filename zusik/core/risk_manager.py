from __future__ import annotations
"""리스크 관리 모듈.

4중 방어 시스템:
  1. 전략 자동 교체 — 실현손실률 -10% 이상이면 다른 전략으로 전환
  2. 위기 감지 + 긴급 홀딩 — 전쟁/폭락 등 급락 시 매매 중단, 전종목 홀딩
  3. 완전손실 방어 — 상장폐지/관리종목 감지, 종목별 손절, 일일 손실한도
  4. 일일 목표수익 — 정보성 알림만. 매매는 계속 진행하여
     추가 수익을 추구한다. 과거에는 당일 매매 중단 정책이었으나 변경됨.
"""

import json
import logging
import os
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join("data", "risk_state.json")


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state: dict):
    os.makedirs("data", exist_ok=True)
    # 원자적 쓰기: 쓰기 도중 크래시/동시 읽기가 파손 JSON을 만들던 클래스 차단
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


class RiskManager:
    """3중 리스크 관리."""

    # 전략 교체 순서 (손실 시 다음 전략으로 이동)
    STRATEGY_FALLBACK = [
        "claude",
        "adaptive",
        "macd_rsi",
        "volatility_breakout",
        "dual_momentum",
        "ma_cross",
    ]

    def __init__(self, config: dict):
        # ── 설정값 로드 ──
        risk_cfg = config.get("risk", {})

        # 전략 교체 기준
        self.strategy_switch_loss = risk_cfg.get("strategy_switch_loss", -0.10)  # -10%
        # 한 번 교체했으면 같은 손실 수준에서 또 교체 금지.
        # 직전 교체 시점 손실률 대비 추가 악화가 이만큼 발생해야 다음 교체 발동.
        self.strategy_switch_min_step = risk_cfg.get("strategy_switch_min_step", -0.05)  # 추가 -5%
        # 절대 cooldown — 같은 누적 손실로 매 사이클 트리거되는 무한 루프 차단.
        self.strategy_switch_cooldown_h = risk_cfg.get("strategy_switch_cooldown_h", 24)

        # 긴급 홀딩
        self.crisis_drop_threshold = risk_cfg.get("crisis_drop_threshold", -0.05)  # 일일 -5% 급락
        self.crisis_vix_threshold = risk_cfg.get("crisis_vix_threshold", 30)  # 공포 지수

        # 종목별 손절
        self.stop_loss_per_stock = risk_cfg.get("stop_loss_per_stock", -0.15)  # 종목 -15% 손절
        self.daily_loss_limit = risk_cfg.get("daily_loss_limit", -500000)  # 일일 손실 한도 50만원

        # 일일 목표수익
        self.daily_target_profit_rate = risk_cfg.get("daily_target_profit_rate", 0.02)  # 기본 2%
        self.daily_target_profit_amount = risk_cfg.get("daily_target_profit_amount", 0)  # 금액 기준 (0이면 비율만 사용)
        self._validate_daily_target()

        # 상장폐지 방어 키워드. 시장경보(투자주의/경고/위험)는 KRX '지정' 용어 전체로 매칭한다.
        # 'investment risk' 의미의 일반어 '투자위험'은 미국 증시·반도체 분석 뉴스에 흔히 나와
        # (예: "반도체는 변동성/투자위험이 크다") 정상 ETF에 오탐을 양산했다(실측 381180/133690/379780).
        # '투자위험종목'처럼 지정 용어 전체를 써야 실제 시장경보 지정만 잡는다.
        self.danger_keywords = [
            "관리종목", "투자경고종목", "투자위험종목", "투자주의종목",
            "거래정지", "상장폐지", "감사의견거절", "자본잠식",
            "회생절차", "파산", "부도", "횡령", "분식회계",
        ]

        # 상태 로드
        self._state = _load_state()
        self._emergency_hold = self._state.get("emergency_hold", False)
        self._emergency_reason = self._state.get("emergency_reason", "")
        self._current_strategy_idx = self._state.get("strategy_index", 0)
        self._strategy_switches = self._state.get("strategy_switches", [])
        self._last_switch_loss_rate = self._state.get("last_switch_loss_rate")
        self._last_switch_at = self._state.get("last_switch_at")

    # ══════════════════════════════════════
    # 0. 일일 목표수익 (0% 고정 금지)
    # ══════════════════════════════════════

    def _validate_daily_target(self):
        """일일 목표수익이 0%로 설정되면 강제 보정.

        0%는 '수익을 포기하겠다'는 뜻이므로 금지.
        최소 0.5% 이상이어야 함.
        """
        MIN_TARGET_RATE = 0.005  # 최소 0.5%

        if self.daily_target_profit_rate <= 0:
            logger.warning(
                "일일 목표수익 %.2f%%는 허용되지 않음 (0%% 이하 금지) → 최소값 %.1f%%로 강제 설정",
                self.daily_target_profit_rate * 100, MIN_TARGET_RATE * 100,
            )
            self.daily_target_profit_rate = MIN_TARGET_RATE

    def check_daily_target_reached(self, realized_pnl_today: int, total_asset: int) -> bool:
        """일일 목표수익 도달 여부. True여도 매매는 중단되지 않음.

        호출측(`TradingBot`)은 True일 때 1회 정보 알림만 발송하고 매수/매도는 계속 진행한다.
        금액 기준과 비율 기준 중 하나라도 도달하면 True.
        """
        if total_asset <= 0:
            return False

        today_rate = realized_pnl_today / total_asset

        # 비율 기준 체크
        if today_rate >= self.daily_target_profit_rate:
            logger.info(
                "일일 목표수익 도달: 오늘 실현 %+.2f%% >= 목표 %.2f%% (%s원) — 목표 수익에 도달했으나, 추가 수익을 위해 계속 동작합니다",
                today_rate * 100, self.daily_target_profit_rate * 100,
                f"{realized_pnl_today:+,}",
            )
            return True

        # 금액 기준 체크 (설정된 경우)
        if self.daily_target_profit_amount > 0 and realized_pnl_today >= self.daily_target_profit_amount:
            logger.info(
                "일일 목표수익 도달: 오늘 실현 %s원 >= 목표 %s원 — 목표 수익에 도달했으나, 추가 수익을 위해 계속 동작합니다",
                f"{realized_pnl_today:+,}", f"{self.daily_target_profit_amount:,}",
            )
            return True

        return False

    # ══════════════════════════════════════
    # 1. 전략 자동 교체
    # ══════════════════════════════════════

    def check_strategy_switch(self, initial_capital: int, realized_pnl_total: int) -> str | None:
        """실현손실률이 기준을 초과하면 다음 전략명을 반환.

        Args:
            initial_capital: 초기 투자금 (또는 총 자산)
            realized_pnl_total: 누적 실현손익

        Returns:
            전환할 전략 이름, 또는 None (교체 불필요)
        """
        if initial_capital <= 0:
            return None

        loss_rate = realized_pnl_total / initial_capital

        if loss_rate > self.strategy_switch_loss:
            return None

        # cooldown 1: 직전 교체와 동일 손실 수준이면 재발동 금지.
        # 매 사이클 같은 누적 실현손실로 fallback chain을 무한 순환하던 버그 차단.
        if self._last_switch_loss_rate is not None:
            improvement_needed = self._last_switch_loss_rate + self.strategy_switch_min_step
            if loss_rate > improvement_needed:
                # 충분히 더 악화되지 않음 → cooldown 유지
                logger.debug(
                    "전략 교체 cooldown: loss=%.1f%% vs 직전=%.1f%% (추가 악화 %.1f%% 필요)",
                    loss_rate * 100, self._last_switch_loss_rate * 100,
                    self.strategy_switch_min_step * 100,
                )
                return None

        # cooldown 2: 시간 기반 — 같은 손실에서 빠르게 재발동되는 케이스 방어.
        if self._last_switch_at:
            try:
                last_dt = datetime.fromisoformat(self._last_switch_at)
                elapsed_h = (datetime.now() - last_dt).total_seconds() / 3600
                if elapsed_h < self.strategy_switch_cooldown_h:
                    logger.debug(
                        "전략 교체 cooldown: 마지막 교체 %.1fh 전 (cooldown %dh)",
                        elapsed_h, self.strategy_switch_cooldown_h,
                    )
                    return None
            except (ValueError, TypeError):
                pass

        next_idx = self._current_strategy_idx + 1
        if next_idx >= len(self.STRATEGY_FALLBACK):
            next_idx = 0  # 처음으로 돌아감

        next_strategy = self.STRATEGY_FALLBACK[next_idx]
        self._current_strategy_idx = next_idx
        self._last_switch_loss_rate = loss_rate
        self._last_switch_at = datetime.now().isoformat()

        record = {
            "date": self._last_switch_at,
            "reason": f"누적 실현손실 {loss_rate:.1%} (기준: {self.strategy_switch_loss:.0%})",
            "from_index": self._current_strategy_idx - 1,
            "to_strategy": next_strategy,
        }
        self._strategy_switches.append(record)
        self._save()

        logger.warning(
            "전략 자동 교체: 실현손실 %.1f%% → '%s'로 전환",
            loss_rate * 100, next_strategy,
        )
        return next_strategy

    def get_current_strategy_name(self) -> str:
        """현재 선택된 전략명."""
        if self._current_strategy_idx < len(self.STRATEGY_FALLBACK):
            return self.STRATEGY_FALLBACK[self._current_strategy_idx]
        return self.STRATEGY_FALLBACK[0]

    # ══════════════════════════════════════
    # 2. 위기 감지 + 긴급 홀딩
    # ══════════════════════════════════════

    def check_crisis(self, market_data: dict | None = None, df: pd.DataFrame | None = None) -> bool:
        """위기 상황인지 판단. True면 긴급 홀딩 모드.

        감지 기준:
          - 당일 시장(코인/지수) 급락: 일일 변동 -5% 이하
          - 개별 종목 급락: 당일 -8% 이상 하락
          - 3일 연속 하락 + 누적 -10% 이상
        """
        reasons = []

        # 개별 종목 캔들 데이터 기반 판단
        if df is not None and len(df) >= 2:
            # 당일 급락
            today_change = (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2]
            if today_change <= self.crisis_drop_threshold:
                reasons.append(f"당일 급락 {today_change:.1%}")

            # 3일 연속 하락 + 누적 큰 폭
            if len(df) >= 4:
                changes = df["close"].pct_change().iloc[-3:]
                if (changes < 0).all():
                    cumulative = (df["close"].iloc[-1] / df["close"].iloc[-4]) - 1
                    if cumulative <= -0.10:
                        reasons.append(f"3일 연속 하락, 누적 {cumulative:.1%}")

            # 거래량 이상 급증 + 급락 (패닉셀)
            if len(df) >= 21:
                vol_avg = df["volume"].iloc[-21:-1].mean()
                vol_today = df["volume"].iloc[-1]
                if vol_avg > 0 and vol_today > vol_avg * 5 and today_change < -0.03:
                    reasons.append(f"거래량 {vol_today / vol_avg:.1f}배 폭증 + 하락")

        # 외부 시장 데이터 (Claude 웹검색 결과 등)
        if market_data:
            if market_data.get("kospi_change", 0) <= self.crisis_drop_threshold:
                reasons.append(f"KOSPI 급락 {market_data['kospi_change']:.1%}")
            if market_data.get("sp500_change", 0) <= self.crisis_drop_threshold:
                reasons.append(f"S&P500 급락 {market_data['sp500_change']:.1%}")

        if reasons:
            self.activate_emergency_hold(", ".join(reasons))
            return True

        return False

    def activate_emergency_hold(self, reason: str):
        """긴급 홀딩 모드 활성화 — 모든 매매 중단."""
        self._emergency_hold = True
        self._emergency_reason = reason
        self._state["emergency_hold"] = True
        self._state["emergency_reason"] = reason
        self._state["emergency_activated"] = datetime.now().isoformat()
        self._save()
        logger.critical("긴급 홀딩 모드 활성화: %s", reason)

    def deactivate_emergency_hold(self):
        """긴급 홀딩 모드 해제."""
        self._emergency_hold = False
        self._emergency_reason = ""
        self._state["emergency_hold"] = False
        self._state["emergency_reason"] = ""
        self._state["emergency_deactivated"] = datetime.now().isoformat()
        self._save()
        logger.info("긴급 홀딩 모드 해제")

    def is_emergency_hold(self) -> bool:
        return self._emergency_hold

    def get_emergency_reason(self) -> str:
        return self._emergency_reason

    # ══════════════════════════════════════
    # 3. 완전손실 방어
    # ══════════════════════════════════════

    def check_stock_danger(self, stock_code: str, stock_name: str, news_text: str) -> dict:
        """종목의 상장폐지/관리종목 위험 감지.

        Returns:
            {
                "is_dangerous": bool,
                "danger_level": "safe" | "warning" | "critical",
                "reasons": [감지된 위험 키워드들],
                "action": "hold" | "sell_immediately" | "none"
            }
        """
        found = []
        text = (news_text + " " + stock_name).lower()

        for keyword in self.danger_keywords:
            if keyword in text:
                found.append(keyword)

        if not found:
            return {"is_dangerous": False, "danger_level": "safe", "reasons": [], "action": "none"}

        # 위험도 분류
        critical_keywords = {"상장폐지", "거래정지", "파산", "부도", "회생절차", "감사의견거절", "분식회계", "횡령"}
        has_critical = any(k in critical_keywords for k in found)

        if has_critical:
            level = "critical"
            action = "sell_immediately"
        else:
            level = "warning"
            action = "hold"  # 경고 수준은 일단 홀딩

        logger.warning(
            "종목 위험 감지: %s(%s) — 수준: %s, 키워드: %s, 조치: %s",
            stock_name, stock_code, level, found, action,
        )

        return {
            "is_dangerous": True,
            "danger_level": level,
            "reasons": found,
            "action": action,
        }

    def check_stop_loss_per_stock(self, holding: dict) -> bool:
        """개별 종목 손절선 도달 여부. True면 즉시 매도 필요.

        -15% 이상 하락한 종목은 추가 손실 방지를 위해 강제 매도.
        """
        rate = holding.get("profit_rate", 0) / 100  # API는 %로 줌
        if rate <= self.stop_loss_per_stock:
            logger.warning(
                "종목 손절선 도달: %s — 손실 %.1f%% (기준: %.0f%%)",
                holding.get("name", holding.get("code")),
                rate * 100, self.stop_loss_per_stock * 100,
            )
            return True
        return False

    def check_daily_loss_limit(self, realized_pnl_today: int) -> bool:
        """일일 실현손실 한도 초과 여부. True면 오늘 매매 중단."""
        if realized_pnl_today <= self.daily_loss_limit:
            logger.warning(
                "일일 손실한도 도달: %s원 (한도: %s원) — 오늘 매매 중단",
                f"{realized_pnl_today:,}", f"{self.daily_loss_limit:,}",
            )
            return True
        return False

    # ══════════════════════════════════════
    # 유틸
    # ══════════════════════════════════════

    def get_status(self) -> dict:
        """리스크 관리 현황."""
        return {
            "emergency_hold": self._emergency_hold,
            "emergency_reason": self._emergency_reason,
            "current_strategy_index": self._current_strategy_idx,
            "current_strategy": self.get_current_strategy_name(),
            "strategy_switches": self._strategy_switches,
            "stop_loss_per_stock": self.stop_loss_per_stock,
            "daily_loss_limit": self.daily_loss_limit,
            "strategy_switch_loss": self.strategy_switch_loss,
            "daily_target_profit_rate": self.daily_target_profit_rate,
            "daily_target_profit_amount": self.daily_target_profit_amount,
        }

    def _save(self):
        self._state["strategy_index"] = self._current_strategy_idx
        self._state["strategy_switches"] = self._strategy_switches
        self._state["last_switch_loss_rate"] = self._last_switch_loss_rate
        self._state["last_switch_at"] = self._last_switch_at
        _save_state(self._state)
