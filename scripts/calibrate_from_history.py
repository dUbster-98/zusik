#!/usr/bin/env python3
"""몇 년치 실거래 종목 일봉으로 청산 파라미터(수익 사다리/본전보호)를 학습.

설계:
  1. 우리가 실제 거래한 종목(trades.json) 유니버스의 다년 일봉을 받아 디스크 캐시
  2. 진입 신호(20일 돌파)는 종목별 1회만 계산 — 청산 파라미터와 무관
  3. 청산 후보 그리드를 실제 PositionManager 로직으로 빠르게 재생
  4. walk-forward(앞 60% 학습 / 뒤 40% 검증)로 오버피팅 차단
  5. 검증 구간에서 베이스라인 이상인 최선 후보만 data/learned_params.json에 기록
  6. 압축 요약만 출력 (원본 데이터는 콘텍스트로 올리지 않음)

운영자 수동 실행: /usr/bin/python3 calibrate_from_history.py [--days 900]
"""
from __future__ import annotations

import os, sys  # scripts/ 이동 — 저장소 루트를 import 경로에 추가 (`import zusik`)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import json
import os
import time
from datetime import datetime

CACHE_DIR = os.path.join("data", "ohlcv_cache")
LEARNED = os.path.join("data", "learned_params.json")
REPORT = os.path.join("data", "calibration_report.json")


def _client():
    from dotenv import load_dotenv
    load_dotenv()
    from zusik.clients.kis_client import KISClient
    return KISClient(
        os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", ""),
        os.getenv("KIS_ACCOUNT_NO", ""), os.getenv("KIS_ACCOUNT_PROD", "01"),
        os.getenv("KIS_VIRTUAL", "false").lower() == "true",
    )


def universe_from_trades():
    """실제 거래 종목 유니버스 (sym, market, exchange) 중복 제거."""
    trades = json.load(open("data/trades.json"))
    seen = {}
    for t in trades:
        sym = t.get("code") or t.get("ticker")
        if not sym or sym in seen:
            continue
        seen[sym] = dict(sym=sym, market=t.get("market", "KR"),
                         exchange=t.get("exchange", "NASD"),
                         name=t.get("name") or sym)
    return list(seen.values())


def load_or_fetch(client, u, days):
    """디스크 캐시 우선, 없으면 KIS에서 받아 캐시. df(date index, OHLCV) 반환."""
    import pandas as pd
    os.makedirs(CACHE_DIR, exist_ok=True)
    fp = os.path.join(CACHE_DIR, f"{u['market']}_{u['sym']}.json")
    if os.path.exists(fp):
        try:
            raw = json.load(open(fp))
            df = pd.DataFrame(raw)
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date").sort_index()
        except Exception:
            pass
    try:
        if u["market"] == "US":
            df = client.get_us_daily_long(u["sym"], u.get("exchange", "NASD"), days=days)
        else:
            df = client.get_daily_long(u["sym"], days=days)
    except Exception:
        df = None
    time.sleep(0.3)
    if df is None or df.empty:
        return None
    out = df.reset_index()
    out["date"] = out["date"].astype(str)
    json.dump(out.to_dict("records"), open(fp, "w"))
    return df


def entry_signals(df, lookback=20, ma=60):
    """20일 돌파 + MA 위 진입 신호 배열 (청산 파라미터 무관, 1회 계산)."""
    import numpy as np
    close = df["close"].values
    high = df["high"].values
    n = len(close)
    sig = np.zeros(n, dtype=bool)
    if n < ma + 2:
        return sig
    ma_arr = df["close"].rolling(ma).mean().values
    for i in range(ma, n):
        prior_high = high[i - lookback:i].max()
        if close[i] > prior_high and close[i] > ma_arr[i]:
            sig[i] = True
    return sig


def make_pm(candidate):
    """후보 파라미터로 PositionManager 생성 (pure 청산 메서드만 사용)."""
    from zusik.core.position_manager import PositionManager
    return PositionManager({"position": candidate})


ALL_IN = ((0.0, 1.0),)  # 기본: 진입 즉시 전량 (트리거 0.0 = 진입가)


def simulate(df, entries, pm, lo, hi, hard=-0.15, trail_from_high=-0.08,
             buy_schedule=ALL_IN):
    """[lo:hi] 구간 봉 단위 시뮬 (명시적 shares+cash 모델, 물타기 지원).

    buy_schedule = ((트리거, 비중), ...). 트리거 0.0=진입 즉시, 음수=원진입가 대비 그만큼
    하락 시 예약분 투입(물타기). 비중 합 1.0 → 슬롯당 자본 고정(레버리지 없음, 공정 비교).
    청산은 pm.breakeven_should_protect + hold-floor + 하드스톱. blended avg 기준.

    Returns: (compounded_return, max_drawdown)
    """
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    cash = 1.0
    shares = 0.0; spent = 0.0
    entry_cash = 0.0; orig_entry = 0.0; tr_i = 0
    peak = 0.0; hsb = 0.0
    equity_peak = 1.0; max_dd = 0.0
    for i in range(lo, hi):
        px = close[i]
        if shares > 0:
            # 물타기: 원진입가 대비 트리거 도달 시 예약 트랜치 투입 (blended avg 하락)
            while (tr_i < len(buy_schedule) and buy_schedule[tr_i][0] < 0
                   and low[i] <= orig_entry * (1 + buy_schedule[tr_i][0])):
                add_cash = buy_schedule[tr_i][1] * entry_cash
                add_px = orig_entry * (1 + buy_schedule[tr_i][0])  # 트리거가 체결 가정
                if add_cash <= cash + 1e-12 and add_px > 0:
                    shares += add_cash / add_px; spent += add_cash; cash -= add_cash
                tr_i += 1
            avg = spent / shares
            profit = (px - avg) / avg
            peak = max(peak, (high[i] - avg) / avg)
            hsb = max(hsb, high[i])
            from_high = (px - hsb) / hsb if hsb else 0
            exit_px = None
            if low[i] <= avg * (1 + hard):                       # 하드스톱
                exit_px = avg * (1 + hard)
            elif pm.breakeven_should_protect(peak, profit):       # 본전보호/사다리 (손실가드 내장)
                exit_px = avg * (1 + pm.breakeven_protect_floor(peak))
            elif from_high <= trail_from_high and profit > 0.005:  # 모멘텀 청산 (수익 한정)
                exit_px = px
            if exit_px is not None:
                cash += shares * exit_px
                shares = 0.0; spent = 0.0; peak = 0.0; hsb = 0.0; tr_i = 0
        elif entries[i]:
            entry_cash = cash; orig_entry = px
            spend = buy_schedule[0][1] * entry_cash
            shares = spend / px; spent = spend; cash -= spend
            tr_i = 1; peak = 0.0; hsb = px
        eq = cash + shares * px
        equity_peak = max(equity_peak, eq)
        max_dd = max(max_dd, (equity_peak - eq) / equity_peak)
    if shares > 0:
        cash += shares * close[hi - 1]
    return cash - 1.0, max_dd


CANDIDATES = {
    "old_fixed_1.5":   dict(profit_ladder=[], breakeven_giveback_cap=9.99),
    "committed_prop":  dict(profit_ladder=[], breakeven_giveback_cap=0.025),
    "ladder_shipped":  dict(profit_ladder=[[0.30, 0.24], [0.20, 0.15], [0.15, 0.11], [0.10, 0.06]], breakeven_giveback_cap=0.025),
    "ladder_tight":    dict(profit_ladder=[[0.30, 0.26], [0.20, 0.18], [0.15, 0.13], [0.10, 0.08]], breakeven_giveback_cap=0.025),
    "ladder_loose":    dict(profit_ladder=[[0.30, 0.22], [0.20, 0.13], [0.15, 0.09], [0.10, 0.04]], breakeven_giveback_cap=0.025),
    "ladder_cap02":    dict(profit_ladder=[[0.30, 0.24], [0.20, 0.15], [0.15, 0.11], [0.10, 0.06]], breakeven_giveback_cap=0.02),
    "ladder_cap03":    dict(profit_ladder=[[0.30, 0.24], [0.20, 0.15], [0.15, 0.11], [0.10, 0.06]], breakeven_giveback_cap=0.03),
}


def walk_forward_folds(df, ent, pm, folds=3, buy_schedule=ALL_IN, warmup=60):
    """확장창(anchored) 롤링 walk-forward — fold별 (valid_ret, valid_dd) 리스트 반환.

    [warmup:n] 을 folds+1 등분해, fold i 는 [warmup:a_i] 로 누적 학습 후 [a_i:a_{i+1}] 검증.
    단일 60/40 split 의 국면 의존을 줄여 파라미터 강건성을 평가한다.
    데이터가 부족하면 단일 split 으로 폴백.
    """
    n = len(df)
    if n - warmup < (folds + 1) * 20:
        split = int(n * 0.6)
        return [simulate(df, ent, pm, split, n, buy_schedule=buy_schedule)]
    step = (n - warmup) // (folds + 1)
    out = []
    for i in range(1, folds + 1):
        v0 = warmup + i * step
        v1 = (warmup + (i + 1) * step) if i < folds else n
        out.append(simulate(df, ent, pm, v0, v1, buy_schedule=buy_schedule))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=900, help="종목당 받을 일봉 수 (가능한 만큼)")
    args = ap.parse_args()

    uni = universe_from_trades()
    print(f"유니버스: {len(uni)}종목 (실거래 기준)")
    client = _client()

    # 1) 데이터 수집 + 진입신호 1회 계산
    data = []
    total_bars = 0
    for u in uni:
        df = load_or_fetch(client, u, args.days)
        if df is None or len(df) < 120:
            continue
        ent = entry_signals(df)
        if ent.sum() == 0:
            continue
        data.append((u, df, ent))
        total_bars += len(df)
    if not data:
        print("사용 가능한 데이터 없음 — 중단")
        return
    avg_bars = total_bars / len(data)
    print(f"학습 대상: {len(data)}종목, 평균 {avg_bars:.0f}봉 (~{avg_bars/250:.1f}년), 진입신호 종목별 평균 "
          f"{sum(e.sum() for _,_,e in data)/len(data):.1f}건")

    # 2) 후보별 롤링 다중 fold walk-forward (현대화: 단일 split → 확장창 3-fold)
    # 한 split 의 운(국면 의존)을 줄이고, '어느 국면에서도 안 무너지는' 강건한
    # 파라미터를 고르기 위해 fold별 검증 + 최악 fold 점수를 함께 본다.
    import statistics as st
    FOLDS = 3
    results = {}
    for name, cand in CANDIDATES.items():
        pm = make_pm(cand)
        fold_ret = [[] for _ in range(FOLDS)]
        fold_dd = [[] for _ in range(FOLDS)]
        cret = []
        for u, df, ent in data:
            n = len(df)
            cret.append(simulate(df, ent, pm, 60, int(n * 0.6))[0])  # 학습구간 표시용
            for fi, (vr, vd) in enumerate(walk_forward_folds(df, ent, pm, FOLDS)):
                if fi < FOLDS:
                    fold_ret[fi].append(vr); fold_dd[fi].append(vd)
        fold_scores = [st.mean(fold_ret[fi]) * 100 - 0.5 * st.mean(fold_dd[fi]) * 100
                       for fi in range(FOLDS) if fold_ret[fi]]
        all_vr = [r for f in fold_ret for r in f]
        all_vd = [d for f in fold_dd for d in f]
        results[name] = dict(
            calib_ret=st.mean(cret) * 100 if cret else 0.0,
            valid_ret=st.mean(all_vr) * 100 if all_vr else 0.0,
            valid_dd=st.mean(all_vd) * 100 if all_vd else 0.0,
            mean_score=st.mean(fold_scores) if fold_scores else -999.0,
            worst_score=min(fold_scores) if fold_scores else -999.0,
        )

    # 3) 후보 보고 — fold 평균 점수 + 최악 fold 점수(강건성)
    best = max(results, key=lambda k: results[k]["mean_score"])
    base = results["committed_prop"]
    print(f"\n후보            학습수익  검증수익  검증DD  fold평균  최악fold ({FOLDS}-fold WF)")
    for name, r in sorted(results.items(), key=lambda x: -x[1]["mean_score"]):
        mark = " ←최고" if name == best else ""
        print(f"  {name:16} {r['calib_ret']:+6.1f}% {r['valid_ret']:+6.1f}% {r['valid_dd']:5.1f}%  "
              f"{r['mean_score']:+6.2f}  {r['worst_score']:+6.2f}{mark}")

    # 4) 채택: fold평균·최악fold 둘 다 베이스라인 이상일 때만 (강건성 — 오버피팅/국면취약 차단)
    robust = (best != "committed_prop"
              and results[best]["mean_score"] >= base["mean_score"]
              and results[best]["worst_score"] >= base["worst_score"] - 1e-9)
    adopt = best if robust else "committed_prop"
    print(f"\n최고(fold평균): {best} / 강건성 검증 후 채택: {adopt}")
    if adopt == "committed_prop" and best != "committed_prop":
        print(f"  {best}는 fold평균/최악fold 강건성 미달 → 오버피팅·국면취약 의심, 베이스라인 유지")

    chosen = CANDIDATES[adopt]
    learned = dict(
        profit_ladder=chosen["profit_ladder"],
        breakeven_giveback_cap=chosen["breakeven_giveback_cap"],
        calibrated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        chosen_candidate=adopt, n_symbols=len(data), avg_bars=round(avg_bars),
    )
    json.dump(learned, open(LEARNED, "w"), ensure_ascii=False, indent=2)
    json.dump({k: results[k] for k in results}, open(REPORT, "w"), ensure_ascii=False, indent=2)
    print(f"\n학습 파라미터 기록: {LEARNED}")
    print(f"  profit_ladder={chosen['profit_ladder']}  giveback_cap={chosen['breakeven_giveback_cap']}")
    print("* 봇은 load_config()에서 이 파일을 position 설정에 최종 오버레이 → 재시작 시 자동 대응")

    # ── 물타기(averaging-down) 검증 — 청산은 채택된 exit 고정, 매수 스케줄만 변형 ──
    # 예약 모델: 슬롯당 자본 1.0 고정. all-in vs 하락 시 예약분 투입(레버리지 없음, 공정 비교).
    # drawdown 페널티로 위험 반영 — 물타기가 평균 손실을 줄여도 DD를 키우면 점수에서 깎임.
    import statistics as st
    print("\n" + "=" * 64)
    print("물타기(averaging-down) 검증 — exit=committed_prop 고정, 매수 스케줄만 변형")
    print("=" * 64)
    SCHEDULES = {
        "all_in(현행)":   ((0.0, 1.0),),
        "avgdown_mild":   ((0.0, 0.6), (-0.05, 0.2), (-0.10, 0.2)),
        "avgdown_med":    ((0.0, 0.5), (-0.05, 0.25), (-0.10, 0.25)),
        "avgdown_deep":   ((0.0, 0.5), (-0.07, 0.25), (-0.14, 0.25)),
        "avgdown_aggr":   ((0.0, 0.4), (-0.06, 0.3), (-0.12, 0.3)),
    }
    pm_fixed = make_pm(CANDIDATES["committed_prop"])
    av = {}
    for name, sched in SCHEDULES.items():
        cret = []; cdd = []; vret = []; vdd = []
        for u, df, ent in data:
            n = len(df); split = int(n * 0.6)
            r1, d1 = simulate(df, ent, pm_fixed, 60, split, buy_schedule=sched)
            r2, d2 = simulate(df, ent, pm_fixed, split, n, buy_schedule=sched)
            cret.append(r1); cdd.append(d1); vret.append(r2); vdd.append(d2)
        av[name] = dict(
            calib_ret=st.mean(cret) * 100, valid_ret=st.mean(vret) * 100,
            valid_dd=st.mean(vdd) * 100,
            valid_score=st.mean(vret) * 100 - 0.5 * st.mean(vdd) * 100,
        )
    print("\n스케줄          학습수익  검증수익  검증DD  검증점수")
    for name, r in sorted(av.items(), key=lambda x: -x[1]["valid_score"]):
        print(f"  {name:14} {r['calib_ret']:+6.1f}% {r['valid_ret']:+7.1f}% {r['valid_dd']:5.1f}%  {r['valid_score']:+6.2f}")
    base_av = av["all_in(현행)"]
    best_av = max(av, key=lambda k: av[k]["valid_score"])
    json.dump(av, open(os.path.join("data", "avgdown_report.json"), "w"),
              ensure_ascii=False, indent=2)
    print(f"\n베이스라인 all_in 검증점수 {base_av['valid_score']:+.2f}")
    if best_av != "all_in(현행)" and av[best_av]["valid_score"] > base_av["valid_score"]:
        print(f"{best_av}가 검증 우위(점수 {av[best_av]['valid_score']:+.2f}) — 물타기 도입 검토 가치")
    else:
        print("어떤 물타기도 all_in을 검증에서 못 이김 — 도입 보류 권장 (자본 묶임/DD 손해)")
    print("* 검증 전용 — 자동 적용 안 함. 결과 보고 사용자가 buy_tranches 도입 여부 결정.")


if __name__ == "__main__":
    main()
