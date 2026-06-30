from __future__ import annotations
"""포지션 관리 모듈.

5가지 수익 극대화 시스템:
  1. 분할 매수/매도 — 3단계 진입/청산으로 평균단가 최적화
  2. 트레일링 스톱 — 수익 구간에서 손절선 자동 상향
  3. 멀티 타임프레임 — 일봉(방향) + 시간봉(타이밍) 조합
  4. 상관관계 필터 — 동일 섹터 과집중 방지
  5. 실적 캘린더 — 실적 발표 전후 매매 규칙

포지션 상태: data/positions.json
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

POSITIONS_FILE = os.path.join("data", "positions.json")


def _load_positions() -> dict:
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


# positions.json 저장 직렬화 락 — 메인 루프와 실시간 WS 틱 스레드(빠른 익절/트레일링)가
# 동시에 저장하는 걸 막는다. 과거 고정 tmp(`positions.json.tmp`) 공유 시, 한 스레드가 먼저
# os.replace 로 tmp 를 가져가 다른 스레드가 FileNotFoundError 를 내던 레이스(US 매도 중 빈발).
_SAVE_LOCK = threading.Lock()


def _save_positions(data: dict):
    """positions.json 원자적 저장 (동시 저장 안전).

    - 저장은 `_SAVE_LOCK` 으로 직렬화 (스레드 간 인터리브 차단).
    - 임시파일은 매번 고유 이름(`mkstemp`)으로 생성 → 동시/다중 프로세스에서도 tmp 충돌 없음.
    - 같은 디렉터리에서 os.replace → 원자적 교체. 실패 시 임시파일 정리.
    과거 .bak_* 수동 복구 파일들이 파손 클래스의 증거였다.
    """
    d = os.path.dirname(POSITIONS_FILE) or "."
    os.makedirs(d, exist_ok=True)
    with _SAVE_LOCK:
        try:
            snapshot = dict(data)   # 덤프 중 외부 변형으로부터 top-level 키 보호
        except Exception:
            snapshot = data
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".positions-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, POSITIONS_FILE)   # 동일 파일시스템 → 원자적
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            raise


class PositionManager:
    """포지션 관리 + 5가지 수익 극대화."""

    def __init__(self, config: dict):
        pos_cfg = config.get("position", {})

        # 분할 매수 비율 (합계 = 1.0)
        self.buy_tranches: list[float] = pos_cfg.get("buy_tranches", [0.3, 0.4, 0.3])
        # 분할 매도 비율
        self.sell_tranches: list[float] = pos_cfg.get("sell_tranches", [0.5, 0.3, 0.2])
        # 분할 매수 간격 (2차 매수: -N%, 3차: -M%)
        self.buy_dip_pcts: list[float] = pos_cfg.get("buy_dip_pcts", [-0.03, -0.05])
        # 분할 매도 목표 (1차: +N%, 2차: +M%)
        self.sell_target_pcts: list[float] = pos_cfg.get("sell_target_pcts", [0.05, 0.10])

        # 트레일링 스톱: 고점 대비 -5%→-8%, 활성화 +3%→+5%)
        self.trailing_stop_pct: float = pos_cfg.get("trailing_stop_pct", 0.08)  # 고점 대비 -8%
        self.trailing_activate_pct: float = pos_cfg.get("trailing_activate_pct", 0.05)  # +5% 수익 시 활성화

        # 본전 보호 = 피크 비례 수익 보존: 고정 +1.5% 바닥이 피크와 무관해
        # +5~9% 고점이 +1.5%까지 흘러내리며 평균 3.9%p 반납(breakeven 24건 50%승률 건당
        # +772원, rsi_overbought 고점익절 100% +55k 대비 70배 열위). 되돌림 폭을 캡해
        # 큰 피크일수록 더 높은 곳에서 잠근다. floor = max(min_floor, peak - giveback_cap).
        # 예) cap 2.5%p, min 1.5%: peak +3%→+1.5%(기존 동일), +5%→+2.5%, +8%→+5.5%
        # arm_pct를 +3%로 낮춰 KR도 +3~7% 피크 보호(기존 +7%↑/rsi_trim만 보호하던 사각 제거).
        self.breakeven_arm_pct: float = pos_cfg.get("breakeven_arm_pct", 0.03)
        self.breakeven_giveback_cap: float = pos_cfg.get("breakeven_giveback_cap", 0.025)
        self.breakeven_min_floor: float = pos_cfg.get("breakeven_min_floor", 0.015)
        # 수익 사다리: 피크 +10%↑ 큰 추세에 구간 고정 락. **기본 OFF(opt-in)**.
        # 근거: 2개월 26건 큰추세 백테스트는 사다리 우세(+5.75%p)였으나, 49종목×3.6년 walk-forward
        # 검증에선 피크비례(+75.1%)가 사다리(+68.3%)를 앞섬 — 대부분 돌파는 메가트렌드가 안 되어
        # 느슨한 락이 평균 손해(생존편향). calibrate_from_history.py가 데이터로 ladder 채택 여부를
        # 결정해 learned_params.json에 기록 → load_config가 오버레이. 빈 리스트면 피크비례만 작동.
        self.profit_ladder: list = pos_cfg.get("profit_ladder", [])

        # 상관관계
        self.max_correlation: float = pos_cfg.get("max_correlation", 0.7)  # 상관계수 0.7 이상이면 동시보유 제한
        self.max_same_sector: int = pos_cfg.get("max_same_sector", 2)  # 같은 섹터 최대 2종목

        # 실적 캘린더
        self.earnings_blackout_days: int = pos_cfg.get("earnings_blackout_days", 2)

        # 급등 대응
        self.surge_quick_profit: float = pos_cfg.get("surge_quick_profit", 0.10)
        self.surge_limit_sell: float = pos_cfg.get("surge_limit_sell", 0.25)
        self.surge_dynamic_vol_mult: float = pos_cfg.get("surge_dynamic_vol_mult", 1.5)
        self.surge_dynamic_atr_mult: float = pos_cfg.get("surge_dynamic_atr_mult", 1.2)
        self.surge_dynamic_quick_cap: float = pos_cfg.get("surge_dynamic_quick_cap", 0.05)
        self.surge_dynamic_limit_cap: float = pos_cfg.get("surge_dynamic_limit_cap", 0.10)
        self.surge_vol_fade_ratio: float = pos_cfg.get("surge_vol_fade_ratio", 0.5)
        # 라이딩: 모멘텀 강할 땐 1차 익절을 적게 덜고(0.25) 나머지를
        # 트레일링으로 추세 라이딩 → 폭등 수익 극대화. 모멘텀 약하면 기존 0.5 절반익절.
        self.surge_ride_enabled: bool = pos_cfg.get("surge_ride_enabled", True)
        self.surge_ride_trim_ratio: float = pos_cfg.get("surge_ride_trim_ratio", 0.25)
        self.surge_ride_rsi_max: float = pos_cfg.get("surge_ride_rsi_max", 82.0)

        # 급락 대응 (Claude 호출 없이 즉시 반응)
        self.crash_instant_sell: float = pos_cfg.get("crash_instant_sell", -0.07)       # 당일 -7% → 전량 매도
        self.crash_from_high_sell: float = pos_cfg.get("crash_from_high_sell", -0.10)   # 고점 대비 -10% → 전량 매도
        self.crash_gap_down: float = pos_cfg.get("crash_gap_down", -0.05)              # 갭 하락 -5% → 절반 매도
        self.crash_vol_spike_ratio: float = pos_cfg.get("crash_vol_spike_ratio", 5.0)  # 거래량 5배 + 하락 → 전량 매도
        #: 매수 직후 grace 동안 crash_instant 임계를 이 값으로 강화.
        # 반사실 검증상 fresh-buy의 -4~-7% 당일 급락은 대부분 반등(예: LG이노텍 -5.9%→+39%).
        # grace 중엔 카타스트로픽(-10%+)만 즉시 컷, 그 외엔 보류(-15% 하드스톱이 최후 방어).
        self.crash_grace_catastrophic: float = pos_cfg.get("crash_grace_catastrophic", -0.10)
        #: crash_instant(당일 급락) 임계를 종목 변동성(ATR%)에 비례해 깊게 보정.
        # 변동성 큰 종목은 일상적 -4% 출렁임이 노이즈인데 고정 임계로 패닉컷되던 문제 완화.
        # crash_instant 0%승률(0/13, -649k 바닥투매) — 컷 완화 방향이라 손실철학과 정합.
        # 깊은 붕괴(crash_from_high/grace_catastrophic/하드스톱)는 ATR 무관 — 자본보호 불변.
        self.crash_atr_scaling_enabled: bool = pos_cfg.get("crash_atr_scaling_enabled", True)
        self.crash_atr_baseline: float = pos_cfg.get("crash_atr_baseline", 0.02)   # 이 ATR% 이하면 scale=1.0
        self.crash_atr_mult: float = pos_cfg.get("crash_atr_mult", 20.0)           # 초과 ATR% × 이 배수만큼 깊게
        self.crash_atr_scale_cap: float = pos_cfg.get("crash_atr_scale_cap", 2.0)  # 최대 배수(임계 2배까지만)

        # 피라미딩 (이기는 포지션에 추가 배팅)
        # [+3%, +7%] 도달 시마다 남은 현금의 N%를 추가 매수
        self.pyramid_trigger_pcts: list[float] = pos_cfg.get("pyramid_trigger_pcts", [0.03, 0.07])
        self.pyramid_add_ratios: list[float] = pos_cfg.get("pyramid_add_ratios", [0.4, 0.3])

        # 포지션 상태 로드
        self._positions: dict = _load_positions()

    def plan_add_on(self, code: str, current_price: float, available_cash: float) -> dict:
        """피라미딩 계획: 이기는 포지션에 추가 매수.

        Returns:
            {"qty": 추가 매수할 수량, "level": 1/2, "reason": 설명}
            qty=0이면 추가 매수 조건 미충족
        """
        pos = self._get_position(code)
        avg_price = pos.get("avg_price", 0)
        if avg_price <= 0 or current_price <= 0 or available_cash < current_price:
            return {"qty": 0, "level": 0, "reason": "조건 미충족"}

        profit_rate = (current_price - avg_price) / avg_price
        levels_triggered = pos.get("pyramid_level", 0)

        # 다음 레벨 체크
        next_level = levels_triggered + 1
        if next_level > len(self.pyramid_trigger_pcts):
            return {"qty": 0, "level": 0, "reason": "피라미딩 최대 도달"}

        required = self.pyramid_trigger_pcts[next_level - 1]
        if profit_rate < required:
            return {"qty": 0, "level": 0,
                    "reason": f"수익률 {profit_rate:+.1%} < 기준 {required:+.1%}"}

        add_ratio = self.pyramid_add_ratios[next_level - 1]
        add_amount = available_cash * add_ratio
        qty = int(add_amount / current_price)
        if qty < 1:
            return {"qty": 0, "level": 0, "reason": "금액 부족"}

        return {
            "qty": qty,
            "level": next_level,
            "reason": f"피라미딩 {next_level}차: 수익 {profit_rate:+.1%} 도달, 남은 현금 {add_ratio:.0%} 추가",
        }

    def is_pyramid_eligible(self, code: str, current_price: float) -> bool:
        """추가매수(피라미딩) 자격 여부 — 보유 중 AND 다음 레벨 수익 기준 도달.

        현금 무관(plan_add_on의 수익 게이트만 미러). churn 장벽 면제 판정에 쓴다:
        들고 있는 승자에 더 사는 건 '재매수 churn'이 아니라 승자 증폭이므로.
        """
        if not self.has_position(code) or current_price <= 0:
            return False
        pos = self._get_position(code)
        avg = pos.get("avg_price", 0)
        if avg <= 0:
            return False
        next_level = pos.get("pyramid_level", 0) + 1
        if next_level > len(self.pyramid_trigger_pcts):
            return False
        required = self.pyramid_trigger_pcts[next_level - 1]
        return (current_price - avg) / avg >= required

    def record_pyramid(self, code: str, level: int):
        """피라미딩 실행 기록."""
        pos = self._get_position(code)
        pos["pyramid_level"] = level
        _save_positions(self._positions)

    # ══════════════════════════════════════
    # 1. 분할 매수
    # ══════════════════════════════════════

    def plan_buy(self, code: str, total_amount: int, current_price: int,
                 tranches_override: list[float] | None = None,
                 skip_dip_check: bool = False) -> dict:
        """분할 매수 계획 생성.

        Args:
            tranches_override: 종목별 tranches 비율 override (인버스 헷지 등).
                미지정 시 self.buy_tranches 사용.
            skip_dip_check: 2차/3차 가격 하락 요건 검사 스킵. 인버스 헷지는
                bear_score 기반으로 진입 시점이 결정되므로 가격 dip 체크가 모순.

        Returns:
            {
                "tranche": 현재 몇 차 매수인지 (1, 2, 3),
                "amount": 이번에 투자할 금액,
                "qty": 이번에 매수할 수량,
                "remaining_tranches": 남은 매수 횟수,
            }
        """
        pos = self._get_position(code)

        tranches = tranches_override if tranches_override else self.buy_tranches
        tranche = pos.get("buy_tranche", 0) + 1

        if tranche > len(tranches):
            return {"tranche": 0, "amount": 0, "qty": 0, "remaining_tranches": 0, "skip_reason": "분할 매수 완료"}

        # 2차/3차 매수: 가격이 충분히 하락했을 때만 (skip_dip_check면 우회)
        if tranche >= 2 and not skip_dip_check:
            entry_price = pos.get("avg_price", current_price)
            if entry_price > 0:
                drop = (current_price - entry_price) / entry_price
                required_drop = self.buy_dip_pcts[tranche - 2] if tranche - 2 < len(self.buy_dip_pcts) else -0.03
                if drop > required_drop:
                    return {"tranche": tranche, "amount": 0, "qty": 0, "remaining_tranches": len(tranches) - tranche + 1,
                            "skip_reason": f"추가 매수 대기: 현재 {drop:+.1%}, 필요 {required_drop:.1%}"}

        ratio = tranches[tranche - 1]
        amount = int(total_amount * ratio)
        qty = amount // current_price if current_price > 0 else 0

        return {
            "tranche": tranche,
            "amount": amount,
            "qty": max(qty, 1) if amount >= current_price else 0,
            "remaining_tranches": len(tranches) - tranche,
        }

    def record_buy(self, code: str, name: str, qty: int, price: int):
        """매수 체결 기록."""
        pos = self._get_position(code)

        old_qty = pos.get("qty", 0)
        old_cost = pos.get("avg_price", 0) * old_qty

        new_qty = old_qty + qty
        weighted_avg = ((old_cost + price * qty) / new_qty) if new_qty > 0 else price
        if isinstance(price, float) or isinstance(pos.get("avg_price", 0), float):
            new_avg = round(weighted_avg, 4)
        else:
            new_avg = int(round(weighted_avg))

        pos.update({
            "name": name,
            "qty": new_qty,
            "avg_price": new_avg,
            "buy_tranche": pos.get("buy_tranche", 0) + 1,
            "sell_tranche": 0,
            "high_since_buy": max(pos.get("high_since_buy", price), price),
            "trailing_active": False,
            "rsi_trimmed": False,  # 추가 매수 = 새 라이딩 사이클
            "last_buy_date": datetime.now().isoformat(),
        })

        self._positions[code] = pos
        _save_positions(self._positions)
        logger.info("포지션 기록: %s %d차 매수 %d주 @ %s (평단가 %s)", name, pos["buy_tranche"], qty, f"{price:,}", f"{new_avg:,}")

    # ══════════════════════════════════════
    # 2. 분할 매도
    # ══════════════════════════════════════

    def plan_sell(self, code: str, current_price: int, total_qty: int) -> dict:
        """분할 매도 계획.

        Returns:
            {
                "tranche": 몇 차 매도,
                "qty": 매도 수량,
                "reason": "목표가 1차 도달" 등,
                "remaining_tranches": 남은 매도 횟수,
            }
        """
        pos = self._get_position(code)
        avg_price = pos.get("avg_price", 0)
        if avg_price <= 0 or total_qty <= 0:
            return {"tranche": 1, "qty": total_qty, "reason": "전량 매도"}

        profit_rate = (current_price - avg_price) / avg_price
        sell_tranche = pos.get("sell_tranche", 0) + 1

        if sell_tranche > len(self.sell_tranches):
            return {"tranche": sell_tranche, "qty": total_qty, "reason": "잔여 전량 매도"}

        # 목표가 도달 확인
        if sell_tranche >= 2:
            target_idx = sell_tranche - 2
            if target_idx < len(self.sell_target_pcts):
                target = self.sell_target_pcts[target_idx]
                if profit_rate < target:
                    return {"tranche": sell_tranche, "qty": 0,
                            "reason": f"목표 미도달: 현재 {profit_rate:+.1%}, 필요 +{target:.0%}",
                            "remaining_tranches": len(self.sell_tranches) - sell_tranche + 1}

        ratio = self.sell_tranches[sell_tranche - 1]
        qty = max(1, int(total_qty * ratio))

        # 마지막 차수면 나머지 전부
        if sell_tranche == len(self.sell_tranches):
            qty = total_qty

        return {
            "tranche": sell_tranche,
            "qty": min(qty, total_qty),
            "reason": f"{sell_tranche}차 매도 (수익률 {profit_rate:+.1%})",
            "remaining_tranches": len(self.sell_tranches) - sell_tranche,
        }

    def record_sell(self, code: str, qty: int):
        """매도 체결 기록."""
        pos = self._get_position(code)
        pos["sell_tranche"] = pos.get("sell_tranche", 0) + 1
        pos["qty"] = max(0, pos.get("qty", 0) - qty)

        if pos["qty"] <= 0:
            self._positions.pop(code, None)
        else:
            self._positions[code] = pos

        _save_positions(self._positions)

    def reconcile_holdings(self, held_codes, market: str = "KR",
                           grace_minutes: float = 5.0) -> list:
        """positions.json 을 실제 브로커 보유와 동기화 — 유령 포지션 제거.

        record_sell 누락·외부 매도·코드 재사용으로 positions.json 에 남은 phantom 은
        브로커 실보유가 0인데도 `has_position`=True 라, is_pyramid_eligible 가 '보유 승자'로
        오인해 추가매수 → stale peak 로 breakeven_protect 가 방금 산 물량을 즉시 매도 →
        buy↔sell churn(수수료 손실)을 무한 반복한다(실측: 256750, 35초 만에 매수→매도 -7,540원).

        market 의 코드만 대상(KR=6자리 숫자, US=그 외)으로, held_codes 에 없으면 제거한다.
        방금 매수한 포지션은 잔고 스냅샷 지연 가능성 때문에 grace_minutes 동안 보존.
        **호출측은 잔고 조회가 성공(휴장 빈 응답 아님)했을 때만 부를 것** — 빈 holdings 로
        부르면 전부 유령으로 오인해 정상 포지션을 날린다.
        """
        from datetime import datetime as _dt
        held = set(held_codes or [])

        def _is_kr(c: str) -> bool:
            return bool(c) and c.isdigit() and len(c) == 6
        removed = []
        now = _dt.now()
        for code in list(self._positions.keys()):
            belongs = _is_kr(code) if market == "KR" else not _is_kr(code)
            if not belongs or code in held:
                continue
            lb = self._positions[code].get("last_buy_date")
            if lb:
                try:
                    if (now - _dt.fromisoformat(lb)).total_seconds() < grace_minutes * 60:
                        continue  # 방금 매수 — 스냅샷 반영 전일 수 있어 보존
                except Exception:
                    pass
            self._positions.pop(code, None)
            removed.append(code)
        if removed:
            _save_positions(self._positions)
        return removed

    # ══════════════════════════════════════
    # 3. 트레일링 스톱
    # ══════════════════════════════════════

    def _ladder_lock(self, peak_profit: float) -> float | None:
        """수익 사다리 락 — 피크가 구간 임계 도달 시 고정 락 반환. 미달이면 None."""
        for thr, lock in self.profit_ladder:      # 내림차순 가정
            if peak_profit >= thr:
                return lock
        return None

    def breakeven_protect_floor(self, peak_profit: float) -> float:
        """수익 보존 바닥.

        - 피크 +10%↑ 큰 추세: 수익 사다리(구간 고정 락) — 추세에 숨 쉴 공간, 멀리 라이딩
          (peak +10%→+6%, +15%→+11%, +20%→+15%, +30%→+24%)
        - 작은 피크: 피크 비례 max(min_floor, peak − giveback_cap)
          (peak +3%→+1.5%, +5%→+2.5%, +8%→+5.5% — 되돌림 cap, anti-giveback)
        """
        ladder = self._ladder_lock(peak_profit)
        if ladder is not None:
            return ladder
        return max(self.breakeven_min_floor, peak_profit - self.breakeven_giveback_cap)

    def breakeven_should_protect(self, peak_profit: float, profit_rate: float,
                                 rsi_trimmed: bool = False) -> bool:
        """본전 보호/수익 사다리 발동 여부. KR(update_trailing_stop)·US 인라인 공통 단일 소스.

        - arm 미달이고 rsi_trim도 아니면 미발동(상승 초입 보호)
        - **손실 구간 발동 금지**: 갭다운으로 락을 뚫고 손실까지 빠지면
          여긴 발동하지 않는다. 수익 보호 장치는 손실을 확정하지 않는다(트레일링과 동일 원칙,
          실측 -264k). 극단적 낙폭은 hold floor / 하드스톱(-15%) / 깊은붕괴가 자본보호 담당.
        """
        if profit_rate <= 0:
            return False
        armed = peak_profit >= self.breakeven_arm_pct or bool(rsi_trimmed)
        return armed and profit_rate <= self.breakeven_protect_floor(peak_profit)

    def update_trailing_stop(self, code: str, current_price: int) -> dict | None:
        """현재가로 트레일링 스톱 갱신.

        Returns:
            {"action": "stop_triggered", "stop_price": ...} 또는 None
        """
        pos = self._get_position(code)
        avg_price = pos.get("avg_price", 0)
        if avg_price <= 0:
            return None

        profit_rate = (current_price - avg_price) / avg_price

        # 고점 갱신
        high = max(pos.get("high_since_buy", current_price), current_price)
        pos["high_since_buy"] = high

        # 활성화 조건: 수익률이 기준 이상일 때 트레일링 시작
        if profit_rate >= self.trailing_activate_pct:
            pos["trailing_active"] = True

        # 최고 수익률 추적 (본전 보호용)
        peak_profit = max(pos.get("peak_profit_rate", 0), profit_rate)
        pos["peak_profit_rate"] = peak_profit

        # ── 본전 보호: 피크 비례 보존 — 고정 +1.5% 바닥 → 피크 비례) ──
        # 작은 피크(+3%)는 기존 +1.5% 바닥 유지(상승 초입 과민매도 방지), 큰 피크일수록
        # 더 높은 곳에서 잠가 고점 수익 반납을 cap. RSI 트림 포지션은 peak가 arm 미달이어도
        # 보호 적용(트림=이미 +3%↑ 과매수 구간, 잔여 라이딩분 흘러내림 차단 — BAC/KO 실측).
        if self.breakeven_should_protect(peak_profit, profit_rate, pos.get("rsi_trimmed")):
            self._positions[code] = pos
            _save_positions(self._positions)
            logger.info("본전 보호 발동: %s 최고 %+.1f%% → 현재 %+.1f%% (보존바닥 %+.1f%%)",
                        pos.get("name", code), peak_profit * 100, profit_rate * 100,
                        self.breakeven_protect_floor(peak_profit) * 100)
            return {"action": "breakeven_protect", "stop_price": current_price,
                    "high": high, "peak_profit": peak_profit}

        if pos.get("trailing_active"):
            stop_price = int(high * (1 - self.trailing_stop_pct))
            pos["trailing_stop_price"] = stop_price

            if current_price <= stop_price:
                #: 트레일링 = 수익 보호 장치 — 손실 확정 금지. 실측 발동 2건 전패
                # (-264k, 둘 다 순손실 상태 발동 = 변질된 손절컷). 수수료 차감 순익(+0.5%↑)일
                # 때만 발동, 손실 구간은 hold floor/하드스톱(-15%)/깊은붕괴가 자본보호 담당.
                if profit_rate <= 0.005:
                    self._positions[code] = pos
                    _save_positions(self._positions)
                    logger.info("트레일링 손실발동 억제: %s 수익 %+.1f%% — 수익보호 장치는 손실에서 안 팖, 홀드",
                                pos.get("name", code), profit_rate * 100)
                    return None
                self._positions[code] = pos
                _save_positions(self._positions)
                logger.info("트레일링 스톱 발동: %s 고점 %s → 현재 %s ≤ 손절선 %s",
                            pos.get("name", code), f"{high:,}", f"{current_price:,}", f"{stop_price:,}")
                return {"action": "stop_triggered", "stop_price": stop_price, "high": high}

        self._positions[code] = pos
        _save_positions(self._positions)
        return None

    def mark_rsi_trimmed(self, code: str):
        """RSI 과매수 트림 직후 호출 — 잔여 라이딩분에 본전보호 즉시 개시.

        트림 시점 = 이미 +3%↑ 수익 과매수 구간. peak가 trailing_activate_pct(+7%) 미달이어도
        잔여분이 +1.5% 이하로 되돌리면 본전보호 익절로 수익 소멸을 막는다.
        """
        pos = self._get_position(code)
        if pos.get("avg_price", 0) > 0:
            pos["rsi_trimmed"] = True
            self._positions[code] = pos
            _save_positions(self._positions)

    def get_trailing_info(self, code: str) -> dict:
        """트레일링 스톱 현황."""
        pos = self._get_position(code)
        return {
            "active": pos.get("trailing_active", False),
            "high": pos.get("high_since_buy", 0),
            "stop_price": pos.get("trailing_stop_price", 0),
        }

    def get_peak_profit(self, code: str) -> float:
        """추적된 최고 수익률(peak_profit_rate). 모호 익절(pop-then-fade) 되돌림 측정용."""
        try:
            return float(self._get_position(code).get("peak_profit_rate", 0.0) or 0.0)
        except Exception:
            return 0.0

    # ══════════════════════════════════════
    # 3-1. 급락 감지 + 즉시 대응 (API 비용 $0)
    # ══════════════════════════════════════

    #: 캘리브레이션 학습 키 → PositionManager 속성. calibrate_from_history.py 가 갱신하면
    # 봇이 재시작 없이 런타임 재적용하는 화이트리스트(안전 — 청산 파라미터만).
    _LEARNED_ATTRS = {
        "profit_ladder": "profit_ladder",
        "breakeven_giveback_cap": "breakeven_giveback_cap",
        "breakeven_arm_pct": "breakeven_arm_pct",
        "breakeven_min_floor": "breakeven_min_floor",
    }

    def apply_learned_params(self, learned: dict) -> list:
        """캘리브레이션 학습 파라미터를 런타임에 재적용 — 재시작 없이 fresh calibration 반영.

        화이트리스트 키(청산 파라미터)만 PositionManager 속성에 덮어쓴다(안전 — 손절선·하드스톱·
        급락 임계 같은 자본보호 레일은 학습 대상 아님). 타입이 맞는 값만 반영(잘못된 값 무시).
        반환: 실제 적용된 키 목록. 봇의 _refresh_learned_params 가 파일 변경 시 호출한다.
        """
        applied = []
        if not isinstance(learned, dict):
            return applied
        for key, attr in self._LEARNED_ATTRS.items():
            if key not in learned:
                continue
            val = learned[key]
            if key == "profit_ladder":
                if not isinstance(val, list):
                    continue
            elif not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            setattr(self, attr, val)
            applied.append(key)
        return applied

    def _crash_instant_threshold(self, df, current_price: int) -> float:
        """당일 급락(crash_instant) 임계를 종목 변동성(ATR%)에 비례해 깊게 보정.

        변동성 큰 종목(예: ATR 5%)은 일상적인 -4~5% 출렁임이 노이즈인데, 고정 임계(-4%)로는
        매 출렁임마다 패닉컷 후보가 됐다(crash_instant 0%승률 바닥투매의 한 원인). ATR% 가
        baseline 을 넘는 만큼 임계를 깊게 늘려(컷을 완화) 정상 변동에 안 던지게 한다.

        반환은 항상 base(고정 임계)와 같거나 더 깊다(절대 더 얕아지지 않음 — 저변동주 보호 유지).
        깊은 붕괴(crash_from_high/grace_catastrophic/하드스톱)는 이 보정과 무관하다(자본보호 불변).
        """
        base = self.crash_instant_sell
        if not self.crash_atr_scaling_enabled or df is None or current_price <= 0:
            return base
        try:
            from zusik.analysis.indicators import atr as _atr
            a = _atr(df)
            if a <= 0:
                return base
            atr_pct = a / float(current_price)
            excess = max(0.0, atr_pct - self.crash_atr_baseline)
            scale = min(self.crash_atr_scale_cap, 1.0 + excess * self.crash_atr_mult)
            return base * scale
        except Exception:
            return base

    def check_crash(self, code: str, current_price: int, df=None) -> dict | None:
        """급락 감지 → 즉시 매도 액션 반환.

        4가지 급락 시나리오 (Claude 호출 없이 로컬에서 즉시 판단):
          1. 당일 -7% 이상 급락 → 전량 즉시 매도
          2. 고점 대비 -10% 급락 → 전량 매도 (트레일링보다 먼저 발동)
          3. 시가 대비 갭 하락 -5% → 절반 매도 (장 시작 폭락)
          4. 거래량 5배 폭증 + 하락 → 전량 매도 (패닉셀)

        Returns:
            {"action": ..., "sell_ratio": 0.5~1.0, "reason": ...} 또는 None
        """
        pos = self._get_position(code)
        avg_price = pos.get("avg_price", 0)
        if avg_price <= 0:
            return None

        name = pos.get("name", code)
        high = pos.get("high_since_buy", avg_price)

        # grace period 판정 — crash_from_high/gap_down만 비활성
        # 당일 -7% 같은 진짜 폭락(crash_instant)은 grace에 영향 X
        in_grace = False
        last_buy = pos.get("last_buy_date")
        if last_buy:
            try:
                from datetime import datetime as _dt
                buy_dt = _dt.fromisoformat(last_buy)
                in_grace = (_dt.now() - buy_dt).total_seconds() < 3600  # 1시간
            except Exception:
                pass

        if df is None or len(df) < 2:
            return None

        curr_close = df["close"].iloc[-1]
        prev_close = df["close"].iloc[-2]
        curr_open = df["open"].iloc[-1] if "open" in df.columns else curr_close

        # ── 1. 당일 급락 → 전량 매도. 단, 매수 직후 grace 중엔 카타스트로픽만 (노이즈 반등 보호) ──
        daily_change = (curr_close - prev_close) / prev_close if prev_close > 0 else 0
        # 변동성(ATR%) 비례 보정 — 변동성 큰 종목의 정상 출렁임에 패닉컷 안 하도록 임계를 깊게.
        instant_thresh = self._crash_instant_threshold(df, current_price)
        if in_grace:
            # grace 중엔 더 깊은 임계 요구 (둘 중 더 낮은 값) — fresh-buy 노이즈 급락 보류
            instant_thresh = min(instant_thresh, self.crash_grace_catastrophic)
        if daily_change <= instant_thresh:
            tag = " [grace 카타스트로픽]" if in_grace else ""
            logger.critical("급락 즉시매도%s: %s 당일 %+.1f%%", tag, name, daily_change * 100)
            return {
                "action": "crash_instant",
                "sell_ratio": 1.0,
                "change": daily_change,
                "reason": f"당일 {daily_change:+.1%} 급락 — 추가 하락 방지 전량 매도",
            }
        if in_grace and daily_change <= self.crash_instant_sell:
            logger.info("급락 감지(%+.1f%%)했으나 매수 직후 보호 — crash_instant 보류: %s",
                        daily_change * 100, name)

        # ── 2. 고점 대비 -10% 급락 (grace 1시간 동안 비활성) ──
        if high > 0 and not in_grace:
            from_high = (current_price - high) / high
            if from_high <= self.crash_from_high_sell:
                logger.critical("고점 급락 매도: %s 고점 %s 대비 %+.1f%%", name, f"{high:,}", from_high * 100)
                return {
                    "action": "crash_from_high",
                    "sell_ratio": 1.0,
                    "change": from_high,
                    "reason": f"고점 {high:,} 대비 {from_high:+.1%} 급락 — 전량 매도",
                }

        # ── 3. 갭 하락 (시가가 전일 종가 대비 -5%, grace 1시간 동안 비활성) ──
        if prev_close > 0 and not in_grace:
            gap = (curr_open - prev_close) / prev_close
            if gap <= self.crash_gap_down:
                logger.warning("갭하락 감지: %s 전일종가 %s → 시가 %s (%+.1f%%)",
                               name, f"{prev_close:,}", f"{curr_open:,}", gap * 100)
                return {
                    "action": "crash_gap_down",
                    "sell_ratio": 0.5,
                    "change": gap,
                    "reason": f"갭 하락 {gap:+.1%} — 절반 매도 (추가 하락 대비)",
                }

        # ── 4. 거래량 폭증 + 하락 (패닉셀) ──
        if len(df) >= 21:
            vol_avg = df["volume"].iloc[-21:-1].mean()
            vol_today = df["volume"].iloc[-1]
            if vol_avg > 0 and vol_today > vol_avg * self.crash_vol_spike_ratio and daily_change < -0.02:
                logger.critical("패닉셀 감지: %s 거래량 %.1f배 + 하락 %+.1f%%",
                                name, vol_today / vol_avg, daily_change * 100)
                return {
                    "action": "crash_panic_sell",
                    "sell_ratio": 1.0,
                    "change": daily_change,
                    "reason": f"패닉셀 — 거래량 {vol_today / vol_avg:.0f}배 폭증 + {daily_change:+.1%} 하락, 전량 매도",
                }

        return None

    # ══════════════════════════════════════
    # 3-2. 급등 감지 + 대응
    # ══════════════════════════════════════

    @staticmethod
    def _surge_momentum_intact(df, rsi_max: float = 82.0) -> bool:
        """급등 모멘텀이 강하게 살아있는가 — 라이딩 트림 판단용.

        조건(모두 충족): 당일 양봉 + 거래량이 최근 평균 이상(매수세 유지) +
        RSI가 과열(rsi_max) 미만. 하나라도 깨지면 추세 둔화로 보고 정상 절반익절.
        """
        if df is None or len(df) < 21:
            return False
        try:
            curr = df.iloc[-1]
            if curr["close"] <= curr["open"]:
                return False  # 양봉 아님 → 모멘텀 둔화
            vol_avg = df["volume"].iloc[-21:-1].mean()
            if not (vol_avg > 0 and curr["volume"] >= vol_avg):
                return False  # 거래량 안 받쳐줌
            close = df["close"]
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rsi = (100 - 100 / (1 + gain / loss)).iloc[-1]
            if pd.notna(rsi) and rsi >= rsi_max:
                return False  # 과열 → 라이딩 위험, 정상 익절
            return True
        except Exception:
            return False

    def check_surge(self, code: str, current_price: int, df=None) -> dict | None:
        """급등 감지 → 익절 액션 반환.

        3가지 급등 시나리오:
          1. +10% 이상 급등 → 절반 즉시 익절 (운 좋은 건 바로 챙기기)
          2. +25% 이상 (상한가 근접) → 전량 매도 (더 갈 확률 < 내릴 확률)
          3. 급등 후 거래량 급감 → 세력 빠지는 신호, 전량 매도

        Returns:
            {"action": "surge_half_sell" | "surge_full_sell" | "surge_vol_fade",
             "sell_ratio": 0.5 | 1.0, "reason": ...}
            또는 None
        """
        pos = self._get_position(code)
        avg_price = pos.get("avg_price", 0)
        if avg_price <= 0:
            return None

        profit_rate = (current_price - avg_price) / avg_price
        name = pos.get("name", code)
        quick_profit = self.surge_quick_profit
        limit_sell = self.surge_limit_sell

        # 고변동/강추세 종목은 급등 익절 기준을 조금 늦춰 큰 추세를 더 보유
        if df is not None and len(df) >= 20:
            returns = df["close"].pct_change().dropna()
            realized_vol = float(returns.tail(20).std()) if len(returns) >= 5 else 0.0
            atr_pct = 0.0
            tr = pd.concat([
                df["high"] - df["low"],
                (df["high"] - df["close"].shift()).abs(),
                (df["low"] - df["close"].shift()).abs(),
            ], axis=1).max(axis=1)
            atr = tr.rolling(14).mean().iloc[-1]
            if pd.notna(atr) and current_price > 0:
                atr_pct = float(atr) / float(current_price)

            trend_extension = min(
                self.surge_dynamic_quick_cap,
                max(realized_vol * self.surge_dynamic_vol_mult,
                    atr_pct * self.surge_dynamic_atr_mult),
            )
            limit_extension = min(self.surge_dynamic_limit_cap, trend_extension * 2)
            quick_profit += trend_extension
            limit_sell += limit_extension

        # ── 상한가 근접 (+25% 이상) → 전량 매도 ──
        if profit_rate >= limit_sell:
            logger.info("급등 전량매도: %s +%.1f%% (상한가 근접)", name, profit_rate * 100)
            return {
                "action": "surge_full_sell",
                "sell_ratio": 1.0,
                "profit_rate": profit_rate,
                "reason": f"급등 +{profit_rate:.0%} — 동적 전량 익절 기준 {limit_sell:.0%} 도달",
            }

        # ── +10% 이상 급등 → 익절 (강한 모멘텀이면 적게 덜고 추세 라이딩) ──
        if profit_rate >= quick_profit:
            # 이미 급등 익절한 적 있으면 스킵
            if pos.get("surge_sold"):
                pass
            else:
                #: 모멘텀 강하면 0.25만 덜고 나머지는 트레일링으로 라이딩 →
                # 폭등 수익 극대화. 모멘텀 둔화면 기존 0.5 절반익절(이익 확정).
                ride = (self.surge_ride_enabled
                        and self._surge_momentum_intact(df, self.surge_ride_rsi_max))
                trim = self.surge_ride_trim_ratio if ride else 0.5
                pos["surge_sold"] = True
                self._positions[code] = pos
                _save_positions(self._positions)
                action = "surge_ride_trim" if ride else "surge_half_sell"
                note = (f"강모멘텀 라이딩 — {trim:.0%}만 익절, 나머지 추세 보유(트레일링 보호)"
                        if ride else f"동적 1차 익절 기준 {quick_profit:.0%} 도달")
                logger.info("급등 %s: %s +%.1f%% (trim %.0f%%)",
                            action, name, profit_rate * 100, trim * 100)
                return {
                    "action": action,
                    "sell_ratio": trim,
                    "profit_rate": profit_rate,
                    "reason": f"급등 +{profit_rate:.0%} — {note}",
                }

        # ── 급등 후 거래량 급감 (세력 이탈) ──
        if df is not None and len(df) >= 3 and profit_rate >= 0.05:
            # 최근 3봉의 거래량 추이
            vols = df["volume"].iloc[-3:]
            if len(vols) == 3 and vols.iloc[-3] > 0:
                # 2봉 전 대비 현재 거래량이 50% 이하로 감소
                vol_ratio = vols.iloc[-1] / vols.iloc[-3]
                if vol_ratio <= self.surge_vol_fade_ratio and vols.iloc[-3] > vols.iloc[-2] > vols.iloc[-1]:
                    logger.info("급등 후 거래량 감소: %s 거래량 %.0f%% 감소, 세력 이탈 의심", name, (1 - vol_ratio) * 100)
                    return {
                        "action": "surge_vol_fade",
                        "sell_ratio": 1.0,
                        "profit_rate": profit_rate,
                        "reason": f"급등 후 거래량 {1 - vol_ratio:.0%} 감소 — 세력 이탈, 전량 매도",
                    }

        return None

    # ══════════════════════════════════════
    # 4. 멀티 타임프레임
    # ══════════════════════════════════════

    @staticmethod
    def multi_timeframe_check(df_daily: pd.DataFrame, df_hourly: pd.DataFrame | None) -> dict:
        """일봉 + 시간봉 시그널 교차 확인.

        Returns:
            {
                "daily_trend": "up" / "down" / "sideways",
                "hourly_timing": "good_entry" / "wait" / "late",
                "aligned": True면 둘 다 같은 방향,
                "confidence_boost": 정렬 시 +0.1~0.2 부스트,
            }
        """
        # 일봉 추세
        if len(df_daily) < 20:
            return {"daily_trend": "unknown", "hourly_timing": "unknown", "aligned": False, "confidence_boost": 0}

        ma5 = df_daily["close"].rolling(5).mean().iloc[-1]
        ma20 = df_daily["close"].rolling(20).mean().iloc[-1]
        price = df_daily["close"].iloc[-1]

        if pd.isna(ma5) or pd.isna(ma20):
            daily_trend = "unknown"
        elif price > ma5 > ma20:
            daily_trend = "up"
        elif price < ma5 < ma20:
            daily_trend = "down"
        else:
            daily_trend = "sideways"

        # 시간봉 타이밍
        hourly_timing = "unknown"
        if df_hourly is not None and len(df_hourly) >= 10:
            h_close = df_hourly["close"]
            h_ma5 = h_close.rolling(5).mean()

            if pd.notna(h_ma5.iloc[-1]) and pd.notna(h_ma5.iloc[-2]):
                # 시간봉 단기 MA가 상승 전환 중이면 좋은 진입
                if h_close.iloc[-1] > h_ma5.iloc[-1] and h_close.iloc[-2] <= h_ma5.iloc[-2]:
                    hourly_timing = "good_entry"
                elif h_close.iloc[-1] > h_ma5.iloc[-1]:
                    hourly_timing = "late"  # 이미 올라간 상태
                else:
                    hourly_timing = "wait"

        # 정렬 여부
        aligned = (daily_trend == "up" and hourly_timing == "good_entry")
        boost = 0.15 if aligned else (0.05 if daily_trend == "up" and hourly_timing != "wait" else 0)

        return {
            "daily_trend": daily_trend,
            "hourly_timing": hourly_timing,
            "aligned": aligned,
            "confidence_boost": boost,
        }

    # ══════════════════════════════════════
    # 5. 상관관계 필터
    # ══════════════════════════════════════

    def check_correlation(self, new_code: str, held_codes: list[str],
                          ohlcv_data: dict[str, pd.DataFrame],
                          threshold: float | None = None) -> dict:
        """신규 매수 종목이 기존 보유 종목과 상관관계가 높은지 체크.

        threshold가 None이면 self.max_correlation 사용 (config 정적 값).
        값을 주면 adaptive 동적 임계로 그 사이클만 덮어씀.

        Returns:
            {"allowed": True/False, "reason": ..., "max_corr": 최대 상관계수}
        """
        eff_threshold = threshold if threshold is not None else self.max_correlation
        if not held_codes or new_code not in ohlcv_data:
            return {"allowed": True, "reason": "", "max_corr": 0}

        new_df = ohlcv_data.get(new_code)
        if new_df is None or len(new_df) < 20:
            return {"allowed": True, "reason": "데이터 부족", "max_corr": 0}

        new_returns = new_df["close"].pct_change().dropna()
        max_corr = 0
        max_corr_code = ""

        for held_code in held_codes:
            held_df = ohlcv_data.get(held_code)
            if held_df is None or len(held_df) < 20:
                continue

            held_returns = held_df["close"].pct_change().dropna()

            # 길이 맞추기
            min_len = min(len(new_returns), len(held_returns))
            if min_len < 10:
                continue

            corr = new_returns.iloc[-min_len:].corr(held_returns.iloc[-min_len:])
            if not np.isnan(corr) and abs(corr) > max_corr:
                max_corr = abs(corr)
                max_corr_code = held_code

        if max_corr >= eff_threshold:
            return {
                "allowed": False,
                "reason": f"상관관계 {max_corr:.2f} ({max_corr_code}와 유사, 임계 {eff_threshold:.2f}) — 섹터 분산 필요",
                "max_corr": max_corr,
            }

        return {"allowed": True, "reason": "", "max_corr": max_corr}

    # ══════════════════════════════════════
    # 6. 실적 캘린더
    # ══════════════════════════════════════

    def check_earnings_blackout(self, stock_code: str, stock_name: str, news_text: str) -> dict:
        """실적 발표 임박 여부 확인.

        Claude 뉴스 검색 결과에서 실적 발표 키워드를 감지.

        Returns:
            {"in_blackout": True/False, "reason": ...}
        """
        earnings_keywords = [
            "실적 발표", "어닝", "earnings", "분기 실적",
            "실적발표", "잠정실적", "영업이익 발표", "매출 발표",
            "earnings report", "earnings call", "quarterly results",
        ]
        upcoming_keywords = [
            "예정", "앞두고", "다가오", "이번 주", "내일", "오늘",
            "upcoming", "scheduled", "expected", "tomorrow",
        ]

        text_lower = news_text.lower()
        has_earnings = any(k in text_lower for k in earnings_keywords)
        has_upcoming = any(k in text_lower for k in upcoming_keywords)

        if has_earnings and has_upcoming:
            return {
                "in_blackout": True,
                "reason": f"{stock_name} 실적 발표 임박 — 매수 보류 (발표 후 방향 확인 필요)",
            }

        return {"in_blackout": False, "reason": ""}

    # ══════════════════════════════════════
    # 내부
    # ══════════════════════════════════════

    def _get_position(self, code: str) -> dict:
        return self._positions.get(code, {})

    def has_position(self, code: str) -> bool:
        pos = self._positions.get(code, {})
        return pos.get("qty", 0) > 0
