from __future__ import annotations
"""API 비용/호출 관리 — CLI 구독 한도 보호.

월 예산:
  Claude Max $100 → 하루 15건 (haiku만, hard 전용)
  Codex 구독 → 하루 20건 (medium)
  agy 구독 → 하루 30건 (easy, 제일 널널)
  합계: 하루 65건

호출 최적화:
  1. 로컬 퀀트 먼저 (호출 0)
  2. 캐시 히트 시 스킵
  3. 시장 변동 없으면 스킵
  4. 하드 리밋 도달 시 완전 중단
"""

import json
import logging
import os
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

COST_FILE = os.path.join("data", "api_costs.json")

# 일일 호출 하드 리밋.
# 이전: Anthropic API 가격 보수치 (Sonnet 50/일).
# 사용자는 Claude Max 20x ($200/월) CLI 구독 + 다른 작업에도 사용.
# Max 20x: 5시간당 ~900 sonnet 메시지, 일 이론치 ~3,600.
# 봇 외 사용 고려해 일 150 sonnet, total 2500로 보수적 설정.
DAILY_LIMITS = {
    # Claude 재활성화 (사용자 요청: "완전차단 풀고 opus 등 덜 쓰게").
    # 완전차단(전부 0)을 해제하되 보수적 한도로:
    # - opus 0 유지: 최고가 모델. premium tier는 sonnet으로 폴백 (품질 손실 미미)
    # - sonnet 150: 보수치 복원(600 상향이 주간 폭증 원인이었음)
    # - haiku 600: 저가 보조. balanced/easy rotation에서 codex/agy와 분산
    # 한도 도달 시 _check_limit이 자동으로 codex/agy 폴백 — 매매 끊김 없음.
    "claude_opus": 0,
    "claude_sonnet": 150,
    "claude_haiku": 600,
    "codex": 1500,
    "agy": 800,         # Antigravity — 구글 Gemini 계열. gemini 폐지 후 주력 저가 provider라 상향.
                        # 에이전트형(호출당 내부 다중호출)이라 실제 quota는 더 빨리 닳을 수 있음.
    "total": 6000,
}


def _load_costs() -> dict:
    if os.path.exists(COST_FILE):
        try:
            with open(COST_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"daily": {}}


def _save_costs(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(COST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class CostOptimizer:
    """CLI 구독 한도 보호 + 호출 최적화."""

    def __init__(self, config: dict):
        cost_cfg = config.get("api_cost", {})

        self.cache_ttl_calm: int = cost_cfg.get("cache_ttl_calm", 60)
        self.cache_ttl_normal: int = cost_cfg.get("cache_ttl_normal", 30)
        self.cache_ttl_volatile: int = cost_cfg.get("cache_ttl_volatile", 10)
        self.cache_ttl: int = self.cache_ttl_normal
        self.price_change_threshold: float = cost_cfg.get("price_change_threshold", 0.005)
        self.full_analysis_threshold: float = cost_cfg.get("full_analysis_threshold", 0.05)

        # 일일 호출 한도 — config.api_cost.daily_limits 로 플랜별 오버라이드 가능.
        # 미지정 키는 모듈 기본값(DAILY_LIMITS) 유지. "사용 요금(plan)에 따라" 조절용.
        # 한도 도달 시 _check_limit이 codex/agy로 자동 폴백 → 매매 끊김 없음.
        _override = cost_cfg.get("daily_limits") or {}
        self.daily_limits: dict = {**DAILY_LIMITS, **{k: int(v) for k, v in _override.items()}}

        self._market_temp: str = "normal"
        self._recent_changes: list = []
        self._cache: dict = {}
        self._costs = _load_costs()

    # ══════════════════════════════════════
    # 호출 한도 체크 (하드 리밋)
    # ══════════════════════════════════════

    def can_call(self, provider: str = "total") -> bool:
        """이 provider로 호출 가능한지. False면 절대 호출 안 함."""
        today = datetime.now().strftime("%Y-%m-%d")
        daily = self._costs.get("daily", {}).get(today, {})

        provider_count = daily.get(provider, 0)
        total_count = daily.get("total", 0)

        limit = self.daily_limits.get(provider, 999)
        total_limit = self.daily_limits.get("total", DAILY_LIMITS["total"])

        if total_count >= total_limit:
            return False
        if provider_count >= limit:
            return False
        return True

    def record_call(self, provider: str = "claude"):
        """호출 기록."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in self._costs.get("daily", {}):
            self._costs.setdefault("daily", {})[today] = {}

        daily = self._costs["daily"][today]
        daily[provider] = daily.get(provider, 0) + 1
        daily["total"] = daily.get("total", 0) + 1
        _save_costs(self._costs)

    def get_today_usage(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        daily = self._costs.get("daily", {}).get(today, {})
        opus = daily.get("claude_opus", 0)
        sonnet = daily.get("claude_sonnet", 0)
        haiku = daily.get("claude_haiku", 0)
        claude_total = opus + sonnet + haiku
        return {
            "claude": f"{claude_total} (opus {opus}/{self.daily_limits['claude_opus']} sonnet {sonnet}/{self.daily_limits['claude_sonnet']} haiku {haiku}/{self.daily_limits['claude_haiku']})",
            "codex": f"{daily.get('codex', 0)}/{self.daily_limits['codex']}",
            "agy": f"{daily.get('agy', 0)}/{self.daily_limits.get('agy', 0)}",
            "total": f"{daily.get('total', 0)}/{self.daily_limits['total']}",
        }

    # ══════════════════════════════════════
    # 시장 온도
    # ══════════════════════════════════════

    def update_market_temperature(self, price_change: float):
        self._recent_changes.append(abs(price_change))
        self._recent_changes = self._recent_changes[-20:]
        if len(self._recent_changes) < 3:
            return

        avg = sum(self._recent_changes) / len(self._recent_changes)
        mx = max(self._recent_changes[-5:]) if len(self._recent_changes) >= 5 else max(self._recent_changes)
        old = self._market_temp

        if mx >= 0.03 or avg >= 0.015:
            self._market_temp = "extreme"
            self.cache_ttl = 5
        elif mx >= 0.01 or avg >= 0.007:
            self._market_temp = "volatile"
            self.cache_ttl = self.cache_ttl_volatile
        elif avg >= 0.003:
            self._market_temp = "normal"
            self.cache_ttl = self.cache_ttl_normal
        else:
            self._market_temp = "calm"
            self.cache_ttl = self.cache_ttl_calm

        if old != self._market_temp:
            logger.info("시장 온도: %s → %s (캐시 %d분)", old, self._market_temp, self.cache_ttl)

    def get_market_temperature(self) -> dict:
        return {"temperature": self._market_temp, "cache_ttl": self.cache_ttl}

    # ══════════════════════════════════════
    # 로컬 퀀트 사전 체크 (호출 0)
    # ══════════════════════════════════════

    @staticmethod
    def local_quick_check(df) -> dict:
        if df is None or len(df) < 20:
            return {"action_needed": True, "reason": "데이터 부족", "signal_hint": "neutral"}

        close = df["close"]
        curr = close.iloc[-1]
        prev = close.iloc[-2]
        change = (curr - prev) / prev

        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = (100 - (100 / (1 + rs))).iloc[-1]

        reasons = []
        hint = "neutral"

        if abs(change) >= 0.03:
            reasons.append(f"급변 {change:+.1%}")
        if pd.notna(rsi) and rsi <= 30:
            reasons.append(f"RSI {rsi:.0f} 과매도")
            hint = "bullish"
        elif pd.notna(rsi) and rsi >= 70:
            reasons.append(f"RSI {rsi:.0f} 과매수")
            hint = "bearish"
        if pd.notna(ma5) and pd.notna(ma20):
            gap = abs(ma5 - ma20) / ma20
            if gap < 0.005:
                reasons.append("MA 교차 임박")
        if len(df) >= 21:
            vol_avg = df["volume"].iloc[-21:-1].mean()
            if vol_avg > 0 and df["volume"].iloc[-1] > vol_avg * 3:
                reasons.append(f"거래량 {df['volume'].iloc[-1] / vol_avg:.1f}배")

        return {"action_needed": len(reasons) > 0, "reason": ", ".join(reasons) or "특이사항 없음", "signal_hint": hint}

    # ══════════════════════════════════════
    # 캐시
    # ══════════════════════════════════════

    def should_analyze(self, code: str, current_price: float) -> dict:
        now = datetime.now()

        # 하드 리밋 체크
        if not self.can_call("total"):
            return {"should_call": False, "call_level": "skip",
                    "reason": f"일일 한도 도달 ({self.get_today_usage()['total']})",
                    "cached_result": self._cache.get(code, {}).get("result")}

        cached = self._cache.get(code)
        if cached:
            age_min = (now - datetime.fromisoformat(cached["timestamp"])).total_seconds() / 60
            if age_min < self.cache_ttl:
                price_change = abs(current_price - cached["price"]) / cached["price"] if cached["price"] > 0 else 0
                if price_change < self.price_change_threshold:
                    return {"should_call": False, "call_level": "skip",
                            "reason": f"캐시 유효 ({age_min:.0f}분), 변동 {price_change:.2%}",
                            "cached_result": cached["result"]}
                if price_change >= self.full_analysis_threshold:
                    return {"should_call": True, "call_level": "full", "reason": f"급변 {price_change:+.2%}"}
                return {"should_call": True, "call_level": "quick", "reason": f"변동 {price_change:+.2%}"}

        return {"should_call": True, "call_level": "quick", "reason": "캐시 없음"}

    def cache_result(self, code: str, result: dict, price: float):
        self._cache[code] = {
            "result": result, "price": price,
            "timestamp": datetime.now().isoformat(),
        }

    @staticmethod
    def select_analysts(call_level: str, signal_hint: str = "neutral") -> list:
        # 구조적 비용 절감: LLM 애널리스트는 generalist 1명만 (full·quick 공통 1콜).
        # 퀀트(기술적/모멘텀/추세)는 로컬 adaptive 전략이 이미 $0·LLM 없이 수행하므로 LLM
        # '퀀트 애널리스트'는 중복. LLM의 고유 가치(뉴스/거시/수급/종합 판단=generalist)만 1콜로
        # 받는다. 이전 full=2콜(quant+generalist) → 1콜로 절반.
        return ["generalist"]
