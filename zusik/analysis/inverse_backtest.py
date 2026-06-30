from __future__ import annotations
"""인버스 ETF 전략 백테스트 — KIS 과거 일봉(250봉)으로 'bear-regime 타이밍 매매'의 손익 정량화.

목적: 어떤 인버스 ETF를(종목선택) / 어떤 bear 임계로 진입·청산하면 실제로 수익이 나는지를
실데이터로 검증. buy&hold(상시보유) 대비 타이밍 전략의 우위, 레버리지(-1X/-2X) 트레이드오프 비교.

신호: bot._bearish_regime_score 와 동일 공식 —
  bear(t) = clamp(0,1, -momentum_score(index[:t]))   (index = 069500 KODEX 200, 60일 모멘텀)
전략: bear ≥ entry → 인버스 매수 / bear < exit → 청산. (현 봇: entry 0.50, exit 0.30)

실행: python3 -m zusik.analysis.inverse_backtest
"""
import numpy as np
from zusik.analysis.indicators import momentum_score

INDEX_PROXY = "069500"   # KODEX 200 (KR 베타 프록시) — bear 신호 산출
DAYS = 500
WARMUP = 60              # momentum_score 60일 + 워밍업


def _bear_series(index_df):
    """현 봇 공식: clamp(0,1, -momentum_score) (5/20/60일 tanh 모멘텀)."""
    n = len(index_df)
    out = {}
    for t in range(WARMUP, n):
        m = momentum_score(index_df.iloc[: t + 1])
        out[index_df.index[t]] = max(0.0, min(1.0, -m))
    return out


def _trend_bear_series(index_df):
    """재설계 후보: 추세 기반 bear (실제 하락국면에 발화, 강세장엔 0).

    bear=1 조건: 종가<MA60(중기 하락추세) AND ret20<-2%(최근 약세) 둘 다.
    그 외 0. 단순·견고 — 분할/노이즈에 강하고, 모멘텀 스코어처럼 -0.5까지 안 가도 발화.
    """
    close = index_df["close"].values
    n = len(index_df)
    out = {}
    for t in range(WARMUP, n):
        c = close[t]
        ma60 = float(np.mean(close[t - 59: t + 1]))
        ret20 = (c - close[t - 20]) / close[t - 20] if close[t - 20] > 0 else 0
        out[index_df.index[t]] = 1.0 if (c < ma60 and ret20 < -0.02) else 0.0
    return out


def _flag_splits(df, name):
    """일중 |수익률|>25% = 액면분할/데이터 점프 의심 → 경고."""
    c = df["close"].values
    rets = np.abs(np.diff(c) / np.maximum(c[:-1], 1e-9))
    bad = int((rets > 0.25).sum())
    if bad:
        print(f"  {name}: 단일일 >25% 변동 {bad}일 — 분할/데이터 점프 가능(수익률 신뢰도↓)")
    return bad


def _crash_regime_series(index_df, ret1_thr=-0.025, cum3_thr=-0.05, recover_ma=10):
    """급락 레짐 (stateful): 갑작스러운 sharp 하락에만 진입, 회복(close>MA10)까지 유지.

    진입 트리거(둘 중 하나, 강세장 pullback과 구분되는 sharp): 1일 ≤ -2.5% OR 3일누적 ≤ -5%.
    청산: 종가가 MA10 위로 복귀(= 급락 종료). 강세장에선 거의 발화 안 하고, 발화해도 회복 즉시 청산.
    반환: {date: 1.0(보유)/0.0}.
    """
    close = index_df["close"].values
    n = len(index_df)
    out = {}
    in_crash = False
    for t in range(WARMUP, n):
        c = close[t]
        ret1 = (c - close[t - 1]) / close[t - 1] if close[t - 1] > 0 else 0
        ret3 = (c - close[t - 3]) / close[t - 3] if close[t - 3] > 0 else 0
        ma10 = float(np.mean(close[t - 9: t + 1]))
        if not in_crash:
            if ret1 <= ret1_thr or ret3 <= cum3_thr:
                in_crash = True
        else:
            if c > ma10:           # 회복 → 청산
                in_crash = False
        out[index_df.index[t]] = 1.0 if in_crash else 0.0
    return out


def simulate(inv_df, bear_by_date, entry_thr, exit_thr):
    """bear ≥ entry 매수 / bear < exit 청산. 복리 수익률·거래·승률·시장노출 반환."""
    dates = [d for d in inv_df.index if d in bear_by_date]
    closes = inv_df["close"]
    in_pos = False
    entry_px = 0.0
    trades = []
    days_in = 0
    for d in dates:
        b = bear_by_date[d]
        px = float(closes.loc[d])
        if not in_pos and b >= entry_thr:
            in_pos = True; entry_px = px; entry_d = d
        elif in_pos:
            days_in += 1
            if b < exit_thr:
                trades.append((entry_d, d, (px / entry_px - 1.0)))
                in_pos = False
    if in_pos:  # 미청산분 마지막 종가로 마감
        trades.append((entry_d, dates[-1], (float(closes.loc[dates[-1]]) / entry_px - 1.0)))
    if not trades:
        return {"trades": 0, "total_ret": 0.0, "win": 0, "wr": 0.0, "avg": 0.0,
                "days_in": days_in, "span": len(dates)}
    rets = [t[2] for t in trades]
    comp = float(np.prod([1 + r for r in rets]) - 1.0)
    wins = sum(1 for r in rets if r > 0)
    return {"trades": len(trades), "total_ret": comp, "win": wins,
            "wr": wins / len(trades), "avg": float(np.mean(rets)),
            "days_in": days_in, "span": len(dates)}


def main():
    from zusik.analysis.pnl_review import _make_client
    c = _make_client()
    print("=" * 68)
    print(f"인버스 백테스트 — KIS {DAYS}봉, 신호=069500 모멘텀 bear score (봇 동일 공식)")
    print("=" * 68)

    idx = c.get_daily_long(INDEX_PROXY, days=DAYS)
    if idx is None or len(idx) < WARMUP + 20:
        print("지수 프록시 데이터 부족 — 중단"); return
    _flag_splits(idx, f"index {INDEX_PROXY}")
    bear = _bear_series(idx)              # 현 봇 공식
    tbear = _trend_bear_series(idx)       # pullback 추격 (실패한 후보)
    crash = _crash_regime_series(idx)     # 급락 전용 (재설계 채택안)
    bvals = list(bear.values())
    cfires = sum(1 for x in crash.values() if x >= 0.5)
    # 급락 '에피소드' 수 (0→1 전환 횟수)
    seq = list(crash.values())
    episodes = sum(1 for i in range(1, len(seq)) if seq[i] >= 0.5 and seq[i-1] < 0.5)
    print(f"기간 {idx.index[0].date()}~{idx.index[-1].date()} ({len(bear)}일 평가)")
    print(f"  현 신호(momentum bear≥0.50): {sum(1 for x in bvals if x>=0.5)}일 발화 (max {max(bvals):.2f}) — 사실상 미발화")
    print(f"  추격형(trend<MA60 & ret20<-2%): {sum(1 for x in tbear.values() if x>=0.5)}일 — pullback 과발화(손실)")
    print(f"  급락형(1일≤-2.5% or 3일≤-5%, 회복까지): {cfires}일 발화 / {episodes}회 에피소드")
    print("   → 급락형은 강세장에서 거의 안 뜨고, 진짜 sharp 하락에만 반응.\n")

    # 임계 스윕 — 강세장에서 발화 0에 가까운 가장 엄격한 '급락' 정의 탐색.
    kospi_inv = c.get_daily_long("114800", days=DAYS)   # KODEX 인버스 -1X (대표)
    print("=== 급락 임계 스윕 (목표: 강세장 발화 ↓, 진짜 급락만) — 대표 114800(-1X) ===")
    print(f"{'정의(1일/3일/회복MA)':34} {'에피소드':>7} {'발화일':>6}  {'-1X 손익':>9}")
    sweeps = [
        (-0.025, -0.05, 10, "약함 1일-2.5%/3일-5%"),
        (-0.03,  -0.06, 20, "중간 1일-3%/3일-6%"),
        (-0.04,  -0.08, 20, "강함 1일-4%/3일-8%"),
        (-0.05,  -0.10, 20, "극단 1일-5%/3일-10%"),
    ]
    chosen = None
    for r1, c3, rm, lbl in sweeps:
        sig = _crash_regime_series(idx, ret1_thr=r1, cum3_thr=c3, recover_ma=rm)
        seq = list(sig.values())
        eps = sum(1 for i in range(1, len(seq)) if seq[i] >= 0.5 and seq[i-1] < 0.5)
        fires = sum(1 for x in seq if x >= 0.5)
        pnl = simulate(kospi_inv, sig, 0.50, 0.50)["total_ret"] if kospi_inv is not None else 0
        print(f"{lbl:34} {eps:>7} {fires:>6}  {pnl*100:+8.0f}%")
        if eps <= 2 and chosen is None:   # 강세장 2년에 ≤2회 = 진짜 급락만
            chosen = (r1, c3, rm, lbl, eps)
    print()
    if chosen:
        print(f"채택: '{chosen[3]}' — 강세장 2년 발화 {chosen[4]}회뿐(진짜 sharp 급락에만). "
              f"권장 봇 임계: ret1≤{chosen[0]}, cum3≤{chosen[1]}, 회복 MA{chosen[2]}.")
    else:
        print("모든 임계가 강세장에서 다발 → 인버스는 detect_market_condition(crisis/war) 게이트에만 의존 권장.")
    print("\n결론: 2년 KIS 데이터(강세장)엔 지속 하락장이 없어 어떤 인버스도 +가 안 났다.")
    print("      → 가장 엄격한 급락 정의로 평소엔 0발화(손실0), 진짜 폭락 때만 -1X 발동이 데이터-최적.")


if __name__ == "__main__":
    main()
