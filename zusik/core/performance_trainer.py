from __future__ import annotations
"""성과 기반 훈련 시스템.

매월 목표 수익률 vs 실제 수익률을 비교하여:
  초과 달성 → 메리트: 공격성 ↑, 잘한 전략/애널리스트 가중치 ↑
  미달       → 디메리트: 보수적으로, 못한 전략/애널리스트 가중치 ↓
  대폭 미달  → 전면 리셋: 전략 교체, 파라미터 초기화

훈련 주기:
  - 주간 체크 (7일마다 중간 점검)
  - 월간 평가 (30일마다 본 평가 + 메리트/디메리트)

저장: data/training.json
"""

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TRAINING_FILE = os.path.join("data", "training.json")


def _load() -> dict:
    if os.path.exists(TRAINING_FILE):
        with open(TRAINING_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "monthly_targets": [],
        "evaluations": [],
        "current_period": None,
        "cumulative_merit": 0,       # 누적 메리트 점수 (-10 ~ +10)
        "adjustments": [],
    }


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(TRAINING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class PerformanceTrainer:
    """성과 기반 자동 훈련."""

    def __init__(self, config: dict):
        train_cfg = config.get("training", {})

        self.monthly_target_rate: float = train_cfg.get("monthly_target_rate", 0.05)  # 월 5% 목표
        self.weekly_check: bool = train_cfg.get("weekly_check", True)
        self.merit_boost: float = train_cfg.get("merit_boost", 0.1)       # 메리트당 공격성 +10%
        self.demerit_penalty: float = train_cfg.get("demerit_penalty", 0.1)  # 디메리트당 보수적 +10%

        self._data = _load()

        # 현재 기간 초기화
        if not self._data.get("current_period"):
            self._start_new_period()

    # ══════════════════════════════════════
    # 기간 관리
    # ══════════════════════════════════════

    def _start_new_period(self):
        """새 평가 기간 시작."""
        now = datetime.now()
        self._data["current_period"] = {
            "start_date": now.isoformat(),
            "end_date": (now + timedelta(days=30)).isoformat(),
            "target_rate": self.monthly_target_rate,
            "start_asset": 0,  # 봇 실행 시 채워짐
            "weekly_checks": [],
        }
        _save(self._data)

    def set_start_asset(self, total_asset: int):
        """기간 시작 자산 기록."""
        period = self._data.get("current_period")
        if period and period.get("start_asset", 0) == 0:
            period["start_asset"] = total_asset
            _save(self._data)

    # ══════════════════════════════════════
    # 주간 중간 점검
    # ══════════════════════════════════════

    def weekly_checkpoint(self, current_asset: int, realized_pnl: int) -> dict | None:
        """7일마다 중간 점검.

        Returns:
            {"status": "on_track" / "behind" / "ahead", "adjustment": ...} 또는 None (아직 때 안됨)
        """
        if not self.weekly_check:
            return None

        period = self._data.get("current_period")
        if not period or period.get("start_asset", 0) <= 0:
            return None

        start = datetime.fromisoformat(period["start_date"])
        days_elapsed = (datetime.now() - start).days

        # 7일 간격 체크
        last_checks = period.get("weekly_checks", [])
        last_check_day = last_checks[-1]["day"] if last_checks else 0
        if days_elapsed - last_check_day < 7:
            return None

        start_asset = period["start_asset"]
        actual_rate = (current_asset - start_asset) / start_asset if start_asset > 0 else 0

        # 기대 수익률 (일수 비례)
        expected_rate = self.monthly_target_rate * (days_elapsed / 30)

        # 판정
        gap = actual_rate - expected_rate
        if gap >= 0.02:
            status = "ahead"
            msg = f"목표 초과 중 (+{gap:.1%})"
        elif gap >= -0.01:
            status = "on_track"
            msg = f"정상 궤도 ({gap:+.1%})"
        else:
            status = "behind"
            msg = f"목표 미달 ({gap:+.1%})"

        checkpoint = {
            "day": days_elapsed,
            "date": datetime.now().isoformat(),
            "actual_rate": round(actual_rate, 4),
            "expected_rate": round(expected_rate, 4),
            "gap": round(gap, 4),
            "status": status,
        }
        period.setdefault("weekly_checks", []).append(checkpoint)
        _save(self._data)

        logger.info("주간 점검 (%d일차): %s | 실제 %+.2f%% vs 목표 %+.2f%%",
                     days_elapsed, msg, actual_rate * 100, expected_rate * 100)

        return {"status": status, "gap": gap, "days": days_elapsed, "message": msg}

    # ══════════════════════════════════════
    # 월간 평가 + 메리트/디메리트
    # ══════════════════════════════════════

    def monthly_evaluation(self, current_asset: int, realized_pnl_total: int,
                           analyst_standings: dict | None = None) -> dict | None:
        """30일 평가. 메리트/디메리트 부여.

        Returns:
            {
                "result": "merit" / "demerit" / "neutral",
                "score": +2 / -1 / 0,
                "actual_rate": 실제 수익률,
                "target_rate": 목표 수익률,
                "adjustments": [적용할 조정 내역],
            }
        """
        period = self._data.get("current_period")
        if not period or period.get("start_asset", 0) <= 0:
            return None

        start = datetime.fromisoformat(period["start_date"])
        if (datetime.now() - start).days < 28:
            return None  # 아직 30일 안 됨

        start_asset = period["start_asset"]
        actual_rate = (current_asset - start_asset) / start_asset
        target_rate = period["target_rate"]
        gap = actual_rate - target_rate

        # ── 메리트/디메리트 점수 ──
        adjustments = []

        if gap >= 0.03:
            # 대폭 초과 → 큰 메리트
            result = "merit"
            score = 3
            adjustments.append("투자비율 +15% (공격성 강화)")
            adjustments.append("확신도 기준 -5%p (더 많은 기회 잡기)")
            adjustments.append("다음 달 목표 상향 (+1%p)")
        elif gap >= 0:
            # 목표 달성 → 소 메리트
            result = "merit"
            score = 1
            adjustments.append("투자비율 +5%")
            adjustments.append("현재 전략 유지")
        elif gap >= -0.03:
            # 소폭 미달 → 소 디메리트
            result = "demerit"
            score = -1
            adjustments.append("투자비율 -5% (보수적)")
            adjustments.append("확신도 기준 +5%p (더 신중)")
        else:
            # 대폭 미달 → 큰 디메리트
            result = "demerit"
            score = -3
            adjustments.append("투자비율 -15%")
            adjustments.append("확신도 기준 +10%p")
            adjustments.append("최악 성적 애널리스트 가중치 리셋")
            adjustments.append("다음 달 목표 하향 (-1%p)")

        # 누적 메리트
        self._data["cumulative_merit"] = max(-10, min(10, self._data.get("cumulative_merit", 0) + score))

        # 평가 기록
        evaluation = {
            "date": datetime.now().isoformat(),
            "period_start": period["start_date"],
            "start_asset": start_asset,
            "end_asset": current_asset,
            "actual_rate": round(actual_rate, 4),
            "target_rate": target_rate,
            "gap": round(gap, 4),
            "result": result,
            "score": score,
            "cumulative_merit": self._data["cumulative_merit"],
            "adjustments": adjustments,
        }
        self._data["evaluations"].append(evaluation)
        self._data["adjustments"].append({
            "date": datetime.now().isoformat(),
            "adjustments": adjustments,
            "score": score,
        })

        # 새 기간 시작
        self._data["current_period"] = None
        _save(self._data)
        self._start_new_period()

        # 다음 달 목표 조정
        if score >= 3:
            self.monthly_target_rate = min(0.15, self.monthly_target_rate + 0.01)
        elif score <= -3:
            self.monthly_target_rate = max(0.01, self.monthly_target_rate - 0.01)

        logger.info("═══ 월간 평가 ═══")
        logger.info("  시작: %s원 → 현재: %s원", f"{start_asset:,}", f"{current_asset:,}")
        logger.info("  실제: %+.2f%% | 목표: %+.2f%% | 차이: %+.2f%%",
                     actual_rate * 100, target_rate * 100, gap * 100)
        logger.info("  결과: %s (%+d점) | 누적 메리트: %d",
                     result.upper(), score, self._data["cumulative_merit"])
        for a in adjustments:
            logger.info("  조정: %s", a)

        return {
            "result": result,
            "score": score,
            "actual_rate": actual_rate,
            "target_rate": target_rate,
            "gap": gap,
            "cumulative_merit": self._data["cumulative_merit"],
            "adjustments": adjustments,
        }

    # ══════════════════════════════════════
    # 메리트/디메리트를 config에 반영
    # ══════════════════════════════════════

    def apply_adjustments(self, config: dict) -> dict:
        """누적 메리트 점수를 config 파라미터에 반영.

        메리트 +5 → 투자비율 50% 증가, 확신도 -5%p
        디메리트 -5 → 투자비율 50% 감소, 확신도 +5%p
        """
        merit = self._data.get("cumulative_merit", 0)

        if merit == 0:
            return config

        # 투자비율 조정
        ratio = config.get("invest_ratio", 0.1)
        ratio_mult = 1.0 + (merit * self.merit_boost)
        config["invest_ratio"] = max(0.03, min(0.50, ratio * ratio_mult))

        # 확신도 조정
        strategy = config.get("strategy", {})
        min_conf = strategy.get("min_confidence", 0.5)
        conf_adj = merit * -0.01  # 메리트 +1 → 확신도 -1%p (더 많은 기회)
        strategy["min_confidence"] = max(0.30, min(0.80, min_conf + conf_adj))
        config["strategy"] = strategy

        # 일일 목표수익 조정
        risk = config.get("risk", {})
        target = risk.get("daily_target_profit_rate", 0.02)
        target_adj = merit * 0.002  # 메리트 +1 → 일일 목표 +0.2%p
        risk["daily_target_profit_rate"] = max(0.005, min(0.10, target + target_adj))
        config["risk"] = risk

        if merit > 0:
            logger.info("메리트 반영 (누적 %+d): 투자비율 ×%.1f, 확신도 %+.0f%%p, 일일목표 %+.1f%%p",
                        merit, ratio_mult, merit * -1, merit * 0.2)
        else:
            logger.info("디메리트 반영 (누적 %d): 투자비율 ×%.1f, 확신도 %+.0f%%p",
                        merit, ratio_mult, merit * -1)

        return config

    # ══════════════════════════════════════
    # 현황
    # ══════════════════════════════════════

    def get_status(self) -> dict:
        period = self._data.get("current_period", {})
        evals = self._data.get("evaluations", [])
        start_date = period.get("start_date", "")
        days = (datetime.now() - datetime.fromisoformat(start_date)).days if start_date else 0

        return {
            "current_period_day": days,
            "monthly_target": self.monthly_target_rate,
            "cumulative_merit": self._data.get("cumulative_merit", 0),
            "total_evaluations": len(evals),
            "recent_result": evals[-1]["result"] if evals else "없음",
        }
