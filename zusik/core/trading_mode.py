from __future__ import annotations
"""트레이딩 모드 — 자산 + 시장 상황 기반 능동적 자동 전환.

10단계 자산 티어:
  ~5만     → seed       (씨앗, 1종목 올인)
  5~10만   → yolo       (소액 올인, 빠른 회전)
  10~30만  → micro      (소규모 집중)
  30~50만  → aggressive (공격 집중)
  50~100만 → active     (적극 매매)
  100~200만→ balanced   (균형 분산)
  200~500만→ growth     (안정 성장)
  500~1000만→ wealth    (자산 확대)
  1000만~  → premium    (프리미엄 운영)

+ 시장 상황 레이어:
  peace   → 해당 티어 그대로
  tension → 한 단계 보수적으로 다운그레이드
  crisis  → 두 단계 다운 + 현금 비중 확대
  war     → 최소 매매, 현금 90% 확보

자산 변동(수익/손실/적립) 감지 → 매 실행마다 모드 재평가.
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

MODE_STATE_FILE = os.path.join("data", "mode_state.json")

# ══════════════════════════════════════
# 10단계 자산 티어
# ══════════════════════════════════════

AUTO_MODE_TIERS = [
    {"max_asset": 50_000,       "mode": "seed",       "kr": 1, "us": 1},
    {"max_asset": 100_000,      "mode": "yolo",       "kr": 2, "us": 2},
    {"max_asset": 300_000,      "mode": "micro",      "kr": 2, "us": 2},
    {"max_asset": 500_000,      "mode": "aggressive",  "kr": 3, "us": 3},
    {"max_asset": 1_000_000,    "mode": "active",     "kr": 3, "us": 3},
    {"max_asset": 2_000_000,    "mode": "balanced",    "kr": 4, "us": 4},
    {"max_asset": 5_000_000,    "mode": "growth",      "kr": 5, "us": 5},
    {"max_asset": 10_000_000,   "mode": "wealth",      "kr": 10, "us": 7},
    {"max_asset": float("inf"), "mode": "premium",     "kr": 12, "us": 8},
]

MODE_ORDER = ["shelter", "seed", "yolo", "micro", "aggressive", "active", "balanced", "growth", "wealth", "premium"]

# ══════════════════════════════════════
# 시장 상황 레이어
# ══════════════════════════════════════

MARKET_CONDITION_DOWNGRADE = {
    "peace": 0,     # 평시: 다운그레이드 없음
    "tension": 1,   # 긴장: 1단계 다운
    "crisis": 2,    # 위기: 2단계 다운
    "war": 4,       # 전쟁: 4단계 다운 (거의 최하위)
}

# ══════════════════════════════════════
# 모드 프로파일
# ══════════════════════════════════════

MODE_PROFILES = {
    "shelter": {
        # 대피: 전쟁/폭락 시 현금 90% 확보
        "invest_ratio": 0.05,
        "min_confidence": 0.80,
        "buy_tranches": [1.0],
        "buy_dip_pcts": [],
        "sell_tranches": [1.0],
        "sell_target_pcts": [],
        "trailing_stop_pct": 0.02,
        "trailing_activate_pct": 0.01,
        "stop_loss_per_stock": -0.05,
        "daily_loss_limit_pct": 0.03,
        "daily_target_profit_rate": 0.01,
        "screening_style": "conservative",
        "max_same_sector": 1,
        "min_amount": 5000,
        "min_amount_usd": 5,
        "cash_reserve": 0.90,  # 현금 90% 유지
    },
    "seed": {
        "invest_ratio": 0.50,
        "min_confidence": 0.35,
        "buy_tranches": [1.0],      # 소액이라 분할 불필요
        "buy_dip_pcts": [],
        "sell_tranches": [1.0],
        "sell_target_pcts": [],
        "trailing_stop_pct": 0.025,
        "trailing_activate_pct": 0.01,
        "stop_loss_per_stock": -0.08,
        "daily_loss_limit_pct": 0.20,
        "daily_target_profit_rate": 0.06,
        "screening_style": "aggressive",
        "max_same_sector": 1,
        "min_amount": 3000,
        "min_amount_usd": 3,
        "cash_reserve": 0.0,
    },
    "yolo": {
        "invest_ratio": 0.40,
        "min_confidence": 0.40,
        "buy_tranches": [0.7, 0.3],
        "buy_dip_pcts": [-0.03],
        "sell_tranches": [0.6, 0.4],
        "sell_target_pcts": [0.05],
        "trailing_stop_pct": 0.03,
        "trailing_activate_pct": 0.015,
        "stop_loss_per_stock": -0.10,
        "daily_loss_limit_pct": 0.15,
        "daily_target_profit_rate": 0.05,
        "screening_style": "aggressive",
        "max_same_sector": 2,
        "min_amount": 5000,
        "min_amount_usd": 5,
        "cash_reserve": 0.0,
    },
    "micro": {
        "invest_ratio": 0.30,
        "min_confidence": 0.42,
        "buy_tranches": [0.6, 0.4],
        "buy_dip_pcts": [-0.03],
        "sell_tranches": [0.5, 0.5],
        "sell_target_pcts": [0.06],
        "trailing_stop_pct": 0.035,
        "trailing_activate_pct": 0.02,
        "stop_loss_per_stock": -0.12,
        "daily_loss_limit_pct": 0.12,
        "daily_target_profit_rate": 0.04,
        "screening_style": "aggressive",
        "max_same_sector": 2,
        "min_amount": 8000,
        "min_amount_usd": 8,
        "cash_reserve": 0.05,
    },
    "aggressive": {
        "invest_ratio": 0.20,
        "min_confidence": 0.45,
        "buy_tranches": [0.5, 0.3, 0.2],
        "buy_dip_pcts": [-0.02, -0.03],
        "sell_tranches": [0.3, 0.3, 0.4],
        "sell_target_pcts": [0.07, 0.15],
        "trailing_stop_pct": 0.035,
        "trailing_activate_pct": 0.02,
        "stop_loss_per_stock": -0.18,
        "daily_loss_limit_pct": 0.10,
        "daily_target_profit_rate": 0.035,
        "screening_style": "aggressive",
        "max_same_sector": 3,
        "min_amount": 10000,
        "min_amount_usd": 10,
        "cash_reserve": 0.10,
    },
    "active": {
        "invest_ratio": 0.30,         # 종목당 30% (3종목이면 90%)
        "min_confidence": 0.30,
        "buy_tranches": [1.0],
        "buy_dip_pcts": [],
        "sell_tranches": [0.5, 0.5],
        "sell_target_pcts": [0.05],
        "trailing_stop_pct": 0.035,
        "trailing_activate_pct": 0.02,
        "stop_loss_per_stock": -0.12,
        "daily_loss_limit_pct": 0.15,
        "daily_target_profit_rate": 0.04,
        "screening_style": "aggressive",
        "max_same_sector": 2,
        "min_amount": 5000,
        "min_amount_usd": 5,
        "cash_reserve": 0.03,          # 소액 버퍼 3% — 자투리 현금 강제 소진(죽은 자본) 압력 완화
    },
    "balanced": {
        "invest_ratio": 0.12,
        "min_confidence": 0.55,
        "buy_tranches": [0.3, 0.4, 0.3],
        "buy_dip_pcts": [-0.03, -0.05],
        "sell_tranches": [0.5, 0.3, 0.2],
        "sell_target_pcts": [0.05, 0.10],
        "trailing_stop_pct": 0.05,
        "trailing_activate_pct": 0.03,
        "stop_loss_per_stock": -0.15,
        "daily_loss_limit_pct": 0.05,
        "daily_target_profit_rate": 0.02,
        "screening_style": "balanced",
        "max_same_sector": 2,
        "min_amount": 30000,
        "min_amount_usd": 30,
        "cash_reserve": 0.20,
    },
    "growth": {
        "invest_ratio": 0.10,
        "min_confidence": 0.60,
        "buy_tranches": [0.25, 0.35, 0.40],
        "buy_dip_pcts": [-0.04, -0.07],
        "sell_tranches": [0.4, 0.3, 0.3],
        "sell_target_pcts": [0.05, 0.12],
        "trailing_stop_pct": 0.06,
        "trailing_activate_pct": 0.03,
        "stop_loss_per_stock": -0.12,
        "daily_loss_limit_pct": 0.03,
        "daily_target_profit_rate": 0.015,
        "screening_style": "balanced",
        "max_same_sector": 2,
        "min_amount": 50000,
        "min_amount_usd": 50,
        "cash_reserve": 0.25,
    },
    "wealth": {
        "invest_ratio": 0.08,
        "min_confidence": 0.60,
        "buy_tranches": [0.2, 0.3, 0.3, 0.2],
        "buy_dip_pcts": [-0.03, -0.05, -0.08],
        "sell_tranches": [0.3, 0.3, 0.2, 0.2],
        "sell_target_pcts": [0.04, 0.08, 0.15],
        "trailing_stop_pct": 0.05,
        "trailing_activate_pct": 0.03,
        "stop_loss_per_stock": -0.12,
        "daily_loss_limit_pct": 0.025,
        "daily_target_profit_rate": 0.012,
        "screening_style": "balanced",
        "max_same_sector": 2,
        "min_amount": 50000,
        "min_amount_usd": 50,
        "cash_reserve": 0.30,
    },
    "premium": {
        "invest_ratio": 0.06,
        "min_confidence": 0.65,
        "buy_tranches": [0.2, 0.25, 0.3, 0.25],
        "buy_dip_pcts": [-0.03, -0.05, -0.08],
        "sell_tranches": [0.25, 0.25, 0.25, 0.25],
        "sell_target_pcts": [0.04, 0.08, 0.15],
        "trailing_stop_pct": 0.05,
        "trailing_activate_pct": 0.03,
        "stop_loss_per_stock": -0.10,
        "daily_loss_limit_pct": 0.02,
        "daily_target_profit_rate": 0.01,
        "screening_style": "balanced",
        "max_same_sector": 2,
        "min_amount": 100000,
        "min_amount_usd": 100,
        "cash_reserve": 0.35,
    },
    "conservative": {
        "invest_ratio": 0.08,
        "min_confidence": 0.65,
        "buy_tranches": [0.25, 0.35, 0.40],
        "buy_dip_pcts": [-0.04, -0.07],
        "sell_tranches": [0.6, 0.25, 0.15],
        "sell_target_pcts": [0.03, 0.06],
        "trailing_stop_pct": 0.07,
        "trailing_activate_pct": 0.04,
        "stop_loss_per_stock": -0.12,
        "daily_loss_limit_pct": 0.03,
        "daily_target_profit_rate": 0.015,
        "screening_style": "conservative",
        "max_same_sector": 2,
        "min_amount": 50000,
        "min_amount_usd": 50,
        "cash_reserve": 0.30,
    },
}


# ══════════════════════════════════════
# 상태 관리
# ══════════════════════════════════════

def _load_state() -> dict:
    if os.path.exists(MODE_STATE_FILE):
        with open(MODE_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state: dict):
    os.makedirs("data", exist_ok=True)
    with open(MODE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════
# 자산 + 시장 상황 기반 모드 결정
# ══════════════════════════════════════

def determine_auto_mode(total_asset: int, market_condition: str = "peace",
                        external_reserve: int = 0) -> dict:
    """자산 + 시장 상황 + 외부 자산으로 최적 모드 결정.

    Args:
        total_asset: 주식 계좌 자산 (원)
        market_condition: "peace" / "tension" / "crisis" / "war"
        external_reserve: 주식 계좌 밖 자산 (통장 보유금 등)
    """
    # 1) 자산 기반 기본 모드
    base_mode = "seed"
    kr, us = 1, 1
    for tier in AUTO_MODE_TIERS:
        if total_asset <= tier["max_asset"]:
            base_mode = tier["mode"]
            kr, us = tier["kr"], tier["us"]
            break

    # 2) 외부 자산 기반 공격성 부스트
    # 주식 계좌가 전체 자산의 극히 일부 → 잃어도 타격 적음 → 더 공격적으로
    aggression_boost = 0
    if external_reserve > 0 and total_asset > 0:
        stock_ratio = total_asset / (total_asset + external_reserve)
        # 주식 비중이 전체의 5% 이하 → 2단계 공격적으로
        # 주식 비중이 10% 이하 → 1단계
        # 주식 비중이 20% 이상 → 부스트 없음
        if stock_ratio <= 0.02:
            aggression_boost = 3
        elif stock_ratio <= 0.05:
            aggression_boost = 2
        elif stock_ratio <= 0.10:
            aggression_boost = 1

        if aggression_boost > 0:
            idx = MODE_ORDER.index(base_mode) if base_mode in MODE_ORDER else 0
            boosted_idx = min(len(MODE_ORDER) - 1, idx + aggression_boost)
            base_mode = MODE_ORDER[boosted_idx]
            #: 부스트 시 종목 수도 boosted mode의 tier로 따라가도록.
            # 이전엔 모드만 AGGRESSIVE 올라가고 kr/us는 base(seed) 값 1/1 유지되어
            # 단일 KR 후보가 가드에 막히면 KR 매매 전면 정지. 사용자 요청.
            for _tier in AUTO_MODE_TIERS:
                if _tier["mode"] == base_mode:
                    kr, us = _tier["kr"], _tier["us"]
                    break
            logger.info(
                "외부자산 부스트: 주식비중 %.1f%% (계좌 %s / 전체 %s) → +%d단계 → %s (KR %d / US %d)",
                stock_ratio * 100, f"{total_asset:,}",
                f"{total_asset + external_reserve:,}",
                aggression_boost, base_mode.upper(), kr, us,
            )

    # 3) 시장 상황에 따른 다운그레이드
    downgrade = MARKET_CONDITION_DOWNGRADE.get(market_condition, 0)
    final_mode = base_mode

    if downgrade > 0 and base_mode in MODE_ORDER:
        idx = MODE_ORDER.index(base_mode)
        new_idx = max(0, idx - downgrade)
        final_mode = MODE_ORDER[new_idx]

    # 위기 시 종목 수 축소
    if market_condition in ("crisis", "war"):
        kr = max(1, kr - 1)
        us = max(1, us - 1)

    return {
        "mode": final_mode,
        "kr_count": kr,
        "us_count": us,
        "original_mode": base_mode,
        "market_condition": market_condition,
        "downgraded": final_mode != base_mode,
        "aggression_boost": aggression_boost,
    }


def detect_market_condition(risk_manager=None, df_samples: list | None = None) -> str:
    """현재 시장 상황 자동 감지.

    Returns:
        "peace" / "tension" / "crisis" / "war"
    """
    if risk_manager and risk_manager.is_emergency_hold():
        reason = risk_manager.get_emergency_reason().lower()
        if any(w in reason for w in ("전쟁", "war", "폭격", "미사일")):
            return "war"
        return "crisis"

    if df_samples:
        drops = []
        for df in df_samples:
            if df is not None and len(df) >= 2:
                change = (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2]
                drops.append(change)

        if drops:
            avg_drop = sum(drops) / len(drops)
            severe_count = sum(1 for d in drops if d <= -0.05)

            if avg_drop <= -0.05 or severe_count >= 2:
                return "crisis"
            if avg_drop <= -0.02 or severe_count >= 1:
                return "tension"

    return "peace"


# ══════════════════════════════════════
# 모드 전환 감지
# ══════════════════════════════════════

def check_mode_change(current_mode: str, total_asset: int,
                      market_condition: str = "peace", discord=None,
                      external_reserve: int = 0) -> str | None:
    """모드 승격/다운그레이드 필요 여부 확인.

    자산 증가 → 승격, 시장 악화 → 다운그레이드, 외부자산 大 → 공격 부스트.
    """
    recommended = determine_auto_mode(total_asset, market_condition, external_reserve)
    new_mode = recommended["mode"]

    if new_mode == current_mode:
        return None

    cur_idx = MODE_ORDER.index(current_mode) if current_mode in MODE_ORDER else 0
    new_idx = MODE_ORDER.index(new_mode) if new_mode in MODE_ORDER else 0

    state = _load_state()

    if new_idx > cur_idx:
        direction = "승격"
    elif new_idx < cur_idx:
        direction = "다운그레이드"
    else:
        return None

    logger.info(
        "모드 %s: %s → %s (자산 %s, 시장 %s%s)",
        direction, current_mode.upper(), new_mode.upper(),
        f"{total_asset:,}", market_condition,
        f", 원래 {recommended['original_mode']}" if recommended["downgraded"] else "",
    )

    state.setdefault("history", []).append({
        "date": datetime.now().isoformat(),
        "direction": direction,
        "from": current_mode,
        "to": new_mode,
        "asset": total_asset,
        "market": market_condition,
    })
    state["current_mode"] = new_mode
    _save_state(state)

    if discord:
        discord.notify_mode_upgrade(current_mode, new_mode, total_asset, recommended)

    return new_mode


# ── 적립금 감지 ──

def check_deposit(prev_cash: int, current_cash: int, total_asset: int) -> dict | None:
    if current_cash <= prev_cash:
        return None
    increase = current_cash - prev_cash
    if increase >= 5000 and increase % 1000 == 0:
        state = _load_state()
        deposits = state.get("deposits", [])
        deposits.append({
            "date": datetime.now().isoformat(),
            "amount": increase,
            "total_after": total_asset,
        })
        state["deposits"] = deposits
        state["total_deposited"] = state.get("total_deposited", 0) + increase
        _save_state(state)
        logger.info("적립금 입금: +%s원 (누적 %s원)", f"{increase:,}", f"{state['total_deposited']:,}")
        return {"amount": increase, "new_total": total_asset}
    return None



# ══════════════════════════════════════
# config 적용
# ══════════════════════════════════════

def apply_mode(config: dict) -> dict:
    mode = config.get("trading_mode", "auto")

    if mode == "auto":
        state = _load_state()
        mode = state.get("current_mode", "seed")
        logger.info("자동 모드: %s (매 실행마다 자산/시장 재평가)", mode.upper())
    else:
        logger.info("수동 모드: %s", mode.upper())

    profile = MODE_PROFILES.get(mode)
    if not profile:
        profile = MODE_PROFILES["seed"]
        mode = "seed"

    state = _load_state()
    state["current_mode"] = mode
    _save_state(state)

    config["invest_ratio"] = profile["invest_ratio"]
    config["min_amount"] = profile["min_amount"]
    config["min_amount_usd"] = profile["min_amount_usd"]
    config["_cash_reserve"] = profile.get("cash_reserve", 0)

    strategy = config.get("strategy", {})
    strategy.setdefault("min_confidence", profile["min_confidence"])
    config["strategy"] = strategy

    # ═══ 통일: config.yaml 명시값은 모드 프로파일보다 항상 우선 ═══
    # 같은 버그 클래스 3연속 발생의 구조적 해결:
    # - buy_tranches 클로버('1주씩 찔끔' 매수)
    # - stop_loss_per_stock 클로버(삼바 -10.6% 바닥컷 -148k)
    # - trailing 클로버(손실 트레일링 2건 전패 -264k)
    # hard-override(pos[key]=...)는 "운영자가 config.yaml을 튜닝해도 조용히 무시"를 의미
    # — 모든 모드 파생 키를 setdefault로 통일. 프로파일은 config가 비워둔 키만 채운다.
    # 가드: test_mode_never_clobbers_explicit_config (제네릭 드리프트 테스트).
    pos = config.get("position", {})
    for key in ("buy_dip_pcts", "sell_tranches", "sell_target_pcts", "max_same_sector",
                "buy_tranches", "trailing_stop_pct", "trailing_activate_pct"):
        pos.setdefault(key, profile[key])
    config["position"] = pos

    risk = config.get("risk", {})
    for key in ("stop_loss_per_stock", "daily_target_profit_rate"):
        risk.setdefault(key, profile[key])
    config["risk"] = risk
    #: 일일손실한도도 config.yaml risk 섹션 오버라이드 허용 (동일 setdefault 철학).
    # 헷지 청산(전일 누적 평가손의 일괄 실현) 같은 일회성 이벤트가 1.5% 한도를 치고
    # 반등장 출구관리까지 전면 정지시킨 실측이 배경. 운영자가 수위 결정.
    config["_daily_loss_limit_pct"] = risk.get("daily_loss_limit_pct",
                                               profile["daily_loss_limit_pct"])

    screening = config.get("screening", {})
    screening["style"] = profile["screening_style"]
    config["screening"] = screening

    config["_active_mode"] = mode

    return config


def get_mode_summary(mode: str) -> str:
    summaries = {
        "shelter": "대피: 현금 90%, 최소 매매, 전쟁/폭락 방어",
        "seed": "씨앗 (~5만): 1종목 올인, 일일 6% 목표",
        "yolo": "소액 (5~10만): 2종목 집중, 빠른 회전",
        "micro": "소규모 (10~30만): 2종목 집중, 수익률 극대화",
        "aggressive": "공격 (30~50만): 3종목, 모멘텀 추격",
        "active": "적극 (50~100만): 3종목, 적극 매매",
        "balanced": "균형 (100~200만): 4종목, 분산 안정",
        "growth": "성장 (200~500만): 5종목, 복리 중심",
        "wealth": "자산 (500~1000만): 6종목, 자산 확대",
        "premium": "프리미엄 (1000만+): 7종목, 안정 운영",
        "conservative": "보수적: 방어주, 높은 확신도만",
    }
    return summaries.get(mode, "알 수 없는 모드")
