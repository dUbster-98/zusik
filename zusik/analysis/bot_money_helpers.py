from __future__ import annotations
"""bot.py 자산 계산 / Monte Carlo / Kelly 헬퍼.

bot.py가 너무 커져서 정적 헬퍼들을 별도 모듈로 분리. 모두 순수 함수라
테스트하기 쉽고 다른 곳에서도 재사용 가능.

주의: 이 모듈은 외부 의존성 (KIS client, Vortex runner) 없이 작동.
Vortex 호출은 호출자가 처리.
"""

import logging
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # 타입 주석 전용 — 런타임은 함수 내 지연 import (numpy 무거움)
    import numpy as np

logger = logging.getLogger(__name__)


# ── 자산 계산 ──

def compute_total_equity(kr_balance: dict, us_balance: dict, fx_rate: float) -> dict:
    """진짜 총자산 = KR 정산 후 + US 정산 후 (모든 미정산 매도 + 환전 대기 포함).

    한투 inquire-present-balance.output3.tot_asst_amt가 사용 가능하면 한투 표시값과 일치하므로
    그대로 신뢰. 없을 때만 USD 구성요소 + 외화계좌 KRW 잔고를 직접 합산.

    Args:
        kr_balance: KISClient.get_balance() 결과
        us_balance: KISClient.get_us_balance() 결과
        fx_rate: USD/KRW 환율
    """
    # KR 현금: T+2 미정산 매도 대금도 자산에 포함한다 — US 처리(us_pending_net_krw)와 대칭.
    # 매도 직후 d2_cash(정산 후 잔고)가 in-transit 매도대금을 누락해, 자산을 일시 과소표시
    # (가짜 -손익)하던 문제 차단(예: 금요일 매도 4.16M이 화요일 결제 전까지 누락 → 가짜 -16.57%).
    # orderable(cash)은 주문 즉시 반영되고 '재사용 가능한 매도대금'을 포함하므로 d2_cash 와 max.
    # (매수 직후 stale-high 로 자산을 부풀리던 nxdy 경유 total_cash 는 1차로 쓰지 않는다 —
    #  33,550 매수 후 total_cash 50,202 그대로 / d2_cash 16,652 정상이던 원버그 회귀 방지.)
    _d2c = int(kr_balance.get("d2_cash", 0) or 0)
    _ordc = int(kr_balance.get("cash", 0) or 0)
    kr_settled = max(_d2c, _ordc) or int(kr_balance.get("total_cash", 0) or 0)
    kr_eval = kr_balance.get("total_eval", 0)

    us_cash_usd = float(us_balance.get("cash_usd", 0) or 0)
    us_pending_usd = float(us_balance.get("sell_pending_usd", 0) or 0)
    us_eval_usd = float(us_balance.get("us_eval_usd", 0) or 0)
    # 외화 계좌의 원화 잔고 (환전 전 입금자금) — 누락 방지
    us_krw_in_account = int(us_balance.get("us_krw_in_account", 0) or 0)

    # us_total v7): 미정산 매도-매수의 net을 자산에 합산.
    # v6에서 sell_pending 전부 제외 → 5/13에 매수가 결제 완료(buy_pending=0)되고
    # 매도만 남은 케이스에서 자산이 -118k 부풀려 빠짐 (cash 59k vs 한투 177k).
    # 진짜 공식: 매도미정산은 곧 들어올 cash, 매수미정산은 이미 cash에서 차감된 자금.
    # net = sell_pending - buy_pending → 자산에 그대로 반영.
    # - 매수↔매도 동시 미정산(짝): net≈0, 자산 부풀림 없음 (v6 의도 그대로 작동)
    # - 매수만 정산되고 매도만 남음: net>0, 매도 cash 자산 반영 (오늘 시나리오)
    # - 매도만 정산되고 매수만 남음: net<0, 이중차감 방지
    us_pending_sell_krw_in = int(us_balance.get("unsettled_sell_krw", 0) or 0)
    us_pending_buy_krw_in = int(us_balance.get("unsettled_buy_krw", 0) or 0)
    us_pending_net_krw = us_pending_sell_krw_in - us_pending_buy_krw_in

    us_total_usd = us_cash_usd + us_eval_usd
    us_total_krw_calc = (int(us_total_usd * fx_rate)
                          + us_krw_in_account
                          + us_pending_net_krw)

    hantu_tot = int(us_balance.get("total_asset_krw", 0) or 0)
    # v9: hantu_total_asset_krw가 KR+외화 통합 자산이라 KR을 더하면 중복.
    # 증거: KR cash 22M 입금 후 hantu_tot=22,238,405 ≈ KR cash + KR eval + 외화 (BAC + USD).
    # 이전 v8에서 KR을 또 더해 44M으로 2배 부풀려 표시되던 문제.
    # 따라서 hantu_tot 신뢰 모드면 그 자체를 total로 사용, KR 합산은 폴백 경로에만.
    if hantu_tot > 0:
        direct_total = kr_settled + kr_eval + us_total_krw_calc
        diff = direct_total - hantu_tot
        # T+2 유령 손익 차단: 한투 present-balance(output3.tot_asst_amt)가
        # 미정산 US 자산을 누락해 총자산을 절반 수준으로 과소표시하는 케이스 발견
        # (입금 22.24M인데 한투 10.48M → 가짜 -52.88%; 직접합산 22.70M ≈ 입금).
        # 직접합산이 한투보다 '크게' 높으면(=한투가 US를 누락) 직접합산을 신뢰한다.
        # - 임계: 1M↑ AND 한투의 10%↑ (소액 T+2 노이즈엔 flip 안 함 → 한투 유지)
        # - 반대(한투 > 직접합산)는 v9 그대로 한투 신뢰 (직접합산이 무언가 누락한 케이스)
        underreport = diff > max(1_000_000, int(hantu_tot * 0.10))
        if underreport:
            total = direct_total
            us_total_krw = us_total_krw_calc
            logger.warning(
                "compute_total_equity: 한투 과소보고 의심 → 직접합산 사용 "
                "(한투 %s, 직접합산 %s, 차이 %+d, US미정산 net %+d). T+2 유령 손익 방지.",
                f"{hantu_tot:,}", f"{direct_total:,}", diff, us_pending_net_krw,
            )
        else:
            total = hantu_tot
            us_total_krw = hantu_tot - kr_settled - kr_eval  # 표시용 (외화 부분만 분리 추정)
            if abs(diff) >= 30_000:
                logger.info(
                    "compute_total_equity: 한투 %s 신뢰 (직접합산 %s, 차이 %+d, net pending %+d)",
                    f"{hantu_tot:,}", f"{direct_total:,}", diff, us_pending_net_krw,
                )
    else:
        us_total_krw = us_total_krw_calc
        total = kr_settled + kr_eval + us_total_krw
        diff = 0

    return {
        "total": total,
        "kr_settled": kr_settled,
        "kr_eval": kr_eval,
        "us_total_usd": us_total_usd,
        "us_total_krw": us_total_krw,
        "us_cash_usd": us_cash_usd,
        "us_pending_usd": us_pending_usd,
        "us_eval_usd": us_eval_usd,
        "us_krw_in_account": us_krw_in_account,
        "hantu_total_krw": hantu_tot,
        "vs_hantu_diff": diff,
    }


def compute_pnl_vs_deposit(total_equity: int, deposits: int) -> dict:
    """입금 대비 진짜 손익률 (drawdown 부풀림 보정)."""
    if deposits <= 0:
        return {"pnl": 0, "pnl_pct": 0.0}
    pnl = total_equity - deposits
    pct = round(pnl / deposits * 100, 2)
    return {"pnl": pnl, "pnl_pct": pct}


# ── Monte Carlo 헬퍼 ──

def format_mc_for_llm(mc: Optional[dict]) -> str:
    """MC 결과를 LLM 컨텍스트 한 줄로."""
    if not mc:
        return ""
    return (
        f"MC통계(1만 시뮬, 30일): "
        f"P(profit>0)={mc.get('p_profit', 0) * 100:.0f}%, "
        f"평균수익 {mc.get('mean_profit', 0) * 100:+.1f}%, "
        f"VaR(95%) {mc.get('var95', 0) * 100:+.1f}%, "
        f"평균MaxDD {mc.get('mean_max_dd', 0) * 100:+.1f}%"
    )


def compute_kelly_fraction(mc: Optional[dict]) -> float:
    """Kelly criterion (Half-Kelly + 보수적 클램프 [0.2, 1.5]).

    f* = (P × W - (1-P) × L) / W
    Half-Kelly + 0.5 baseline → 보수적 사이즈 multiplier.
    """
    if not mc:
        return 1.0
    p = mc.get("p_profit", 0.5)
    mean = mc.get("mean_profit", 0)
    var95 = abs(mc.get("var95", -0.05))
    if p < 0.5 or var95 == 0:
        return 0.5
    mean_per_win = mean / p if p > 0 else 0
    if mean_per_win <= 0:
        return 0.5
    full_kelly = (p * mean_per_win - (1 - p) * var95) / mean_per_win
    return float(max(0.2, min(1.5, 0.5 + full_kelly * 0.5)))


def mc_buy_gate_decision(mc: Optional[dict],
                         min_p_profit: float = 0.55,
                         min_var95: float = -0.15) -> tuple[bool, str]:
    """MC 매수 게이트 — True면 통과, False면 차단."""
    if not mc:
        return True, "MC 미가용 — 통과"
    p = mc.get("p_profit", 0)
    var95 = mc.get("var95", 0)
    mean = mc.get("mean_profit", 0)
    if p < min_p_profit:
        return False, (f"MC P(profit>0)={p * 100:.0f}% < {min_p_profit * 100:.0f}% "
                       f"(mean {mean * 100:+.2f}%, VaR95 {var95 * 100:+.1f}%)")
    if var95 < min_var95:
        return False, f"MC VaR(95%)={var95 * 100:+.1f}% < {min_var95 * 100:.0f}% (꼬리 위험)"
    return True, ""


def compute_returns_from_ohlcv(df, lookback: int = 60) -> Optional["np.ndarray"]:
    """OHLCV df에서 일별 raw returns 추출 (MC 입력용)."""
    if df is None or len(df) < 20:
        return None
    try:
        import numpy as np
        close = df["close"].astype(float).values
        returns = (close[1:] / close[:-1] - 1.0).astype(np.float32)
        if len(returns) < 10:
            return None
        return returns[-lookback:] if len(returns) >= lookback else returns
    except Exception:
        return None


def monte_carlo_bootstrap_numpy(hist_returns, n_paths: int = 2000, t_forward: int = 30,
                                stop_loss: float = -0.10, trailing_stop: float = -0.05,
                                target_profit: float = 0.10, seed: int = 0) -> dict:
    """Monte Carlo bootstrap — path-dependent boundary 평가(numpy Vortex 제거 후 상시 경로).

    과거 수익률에서 부트스트랩 샘플링한 경로마다 stop_loss/target/trailing 경계를 적용해
    종목별 동적 임계(손절/익절/트레일링)를 산출. 종목 선택·리스크 산정에 사용."""
    import numpy as np
    import time as _time
    t0 = _time.time()
    rng = np.random.default_rng(seed)
    sample_idx = rng.integers(0, len(hist_returns), size=(n_paths, t_forward))
    returns = hist_returns[sample_idx]
    prices = np.cumprod(1.0 + returns, axis=1)
    peaks = np.maximum.accumulate(prices, axis=1)
    from_peak = (prices - peaks) / peaks
    profits = prices - 1.0
    n_paths_, t_ = prices.shape
    final_profit = np.full(n_paths_, 0.0, dtype=np.float32)
    max_dd = np.zeros(n_paths_, dtype=np.float32)
    exit_day = np.full(n_paths_, t_, dtype=np.int32)
    for i in range(n_paths_):
        for d in range(t_):
            fp = from_peak[i, d]
            if fp < max_dd[i]:
                max_dd[i] = fp
            pr = profits[i, d]
            if pr <= stop_loss:
                final_profit[i] = pr; exit_day[i] = d + 1; break
            if pr >= target_profit:
                final_profit[i] = pr; exit_day[i] = d + 1; break
            if peaks[i, d] >= 1.05 and fp <= trailing_stop:
                final_profit[i] = pr; exit_day[i] = d + 1; break
            if d == t_ - 1:
                final_profit[i] = pr
    return {
        "p_profit": float((final_profit > 0).mean()),
        "mean_profit": float(final_profit.mean()),
        "median_profit": float(np.median(final_profit)),
        "var95": float(np.percentile(final_profit, 5)),
        "mean_max_dd": float(max_dd.mean()),
        "mean_exit_day": float(exit_day.mean()),
        "elapsed_ms": (_time.time() - t0) * 1000,
        "n_paths": n_paths,
    }


def run_mc_with_fallback(returns, runner=None, n_paths: int = 10000,
                         t_forward: int = 30,
                         stop_loss: float = -0.10,
                         trailing_stop: float = -0.05,
                         target_profit: float = 0.10) -> Optional[dict]:
    """Monte Carlo (numpy). `runner` 인자는 하위호환용 — 무시(Vortex 제거)."""
    if returns is None or len(returns) < 10:
        return None
    seed = int(time.time()) & 0xFFFF
    return monte_carlo_bootstrap_numpy(
        returns, n_paths=min(n_paths, 2000), t_forward=t_forward,
        stop_loss=stop_loss, trailing_stop=trailing_stop,
        target_profit=target_profit, seed=seed,
    )
