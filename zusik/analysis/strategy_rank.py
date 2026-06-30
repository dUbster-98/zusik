from __future__ import annotations
"""전략(모델) 랭킹 백테스트 — KIS 1년 일봉으로 봇의 로컬 전략들을 종목 풀 전체에 돌려
가장 수익 나는 전략/선정법을 데이터로 도출. 결과로 모델선택(adaptive 풀)·선정법을 재설계.

실행: python3 -m zusik.analysis.strategy_rank          # KR 샘플
      python3 -m zusik.analysis.strategy_rank --full   # 전체 풀
      python3 -m zusik.analysis.strategy_rank --us     # US 포함
"""
import sys
import numpy as np
from zusik.analysis.backtest import _build_strategy, simulate, LOCAL_STRATEGIES

STRATS = [s for s in LOCAL_STRATEGIES if s != "adaptive"]   # 개별 전략 + 아래서 adaptive 별도
WARMUP = 60


def _rank(samples: list, fetch, label: str):
    print(f"\n{'='*70}\n{label} — {len(samples)}종목 × {len(STRATS)}전략 (warmup {WARMUP})\n{'='*70}")
    # strat -> list of per-stock return_rate
    agg = {s: [] for s in STRATS}
    agg_wr = {s: [] for s in STRATS}
    agg_tr = {s: 0 for s in STRATS}
    adaptive_rets = []
    n_ok = 0
    for i, (code, name) in enumerate(samples):
        df = fetch(code)
        if df is None or len(df) < WARMUP + 30:
            continue
        df = df.reset_index(drop=True)
        n_ok += 1
        print(f"  [{i+1}/{len(samples)}] {code} ({len(df)}봉) 시뮬...", flush=True)
        per = {}
        for s in STRATS:
            try:
                r = simulate(df, _build_strategy(s), warmup=WARMUP)
                agg[s].append(r["return_rate"]); agg_wr[s].append(r["win_rate"])
                agg_tr[s] += r["total_trades"]; per[s] = r["return_rate"]
            except Exception:
                per[s] = None
        # adaptive: 각 종목에서 자기 백테스트로 고른 전략의 성과 ≈ per-stock best의 근사
        try:
            ra = simulate(df, _build_strategy("adaptive"), warmup=WARMUP)
            adaptive_rets.append(ra["return_rate"])
        except Exception:
            pass
    if n_ok == 0:
        print("  데이터 부족 — 스킵"); return None
    print(f"\n{'전략':22} {'평균수익%':>9} {'중앙%':>7} {'수익종목%':>8} {'평균승률%':>8} {'총거래':>6}")
    print("-"*66)
    rows = []
    for s in STRATS:
        rr = agg[s]
        if not rr: continue
        mean = float(np.mean(rr)); med = float(np.median(rr))
        win_stocks = sum(1 for x in rr if x > 0) / len(rr) * 100
        mwr = float(np.mean(agg_wr[s])) if agg_wr[s] else 0
        rows.append((s, mean, med, win_stocks, mwr, agg_tr[s]))
    rows.sort(key=lambda r: -r[1])
    for s, mean, med, ws, mwr, tr in rows:
        print(f"{s:22} {mean:+8.1f} {med:+7.1f} {ws:7.0f} {mwr:7.0f} {tr:6d}")
    if adaptive_rets:
        am = float(np.mean(adaptive_rets))
        aws = sum(1 for x in adaptive_rets if x > 0) / len(adaptive_rets) * 100
        print(f"{'adaptive(자동선택)':22} {am:+8.1f} {'':>7} {aws:7.0f}")
    print(f"\n  → 평균수익 1위: {rows[0][0]} ({rows[0][1]:+.1f}%). "
          f"adaptive가 1위보다 낮으면 '자동선택이 오히려 손해' = 고정 best가 유리.")
    return rows


def main():
    from zusik.analysis.pnl_review import _make_client
    from zusik.analysis.auto_screener import KR_CANDIDATE_POOL, US_CANDIDATE_POOL
    c = _make_client()
    full = "--full" in sys.argv
    do_us = "--us" in sys.argv

    kr = KR_CANDIDATE_POOL if full else KR_CANDIDATE_POOL[::18]   # ~24종 다양 샘플
    _rank(kr, lambda code: c.get_daily_long(code, days=180), f"KR ({'전체' if full else '샘플'})")

    if do_us:
        us = US_CANDIDATE_POOL if full else US_CANDIDATE_POOL[::24]
        _rank([(t, n) for t, n, *_ in us],
              lambda t: c.get_us_daily_long(t, exchange="NASD", days=250), "US (get_us_daily_long 250봉)")

    print("\n[결론 사용처] 1위 전략 → 모델선택 default/우선. adaptive가 열위면 고정 best로 단순화.")


if __name__ == "__main__":
    main()
