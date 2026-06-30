#!/usr/bin/env python3
"""실제 거래 진입점에 (A)수익 사다리 / (C)물타기 사다리를 입혀 반사실 PnL 측정.

운영자 수동 실행 전용 (ExecStartPre 대상 아님). KIS 일봉으로 시뮬.
사용: python3 backtest_profit_mechanisms.py
주의: 일봉 해상도라 장중 고/저점은 근사. 방향성·규모 파악용.
"""
from __future__ import annotations

import os, sys  # scripts/ 이동 — 저장소 루트를 import 경로에 추가 (`import zusik`)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
from collections import defaultdict
from datetime import datetime


def _client():
    from dotenv import load_dotenv
    load_dotenv()
    import os
    from zusik.clients.kis_client import KISClient
    return KISClient(
        os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", ""),
        os.getenv("KIS_ACCOUNT_NO", ""), os.getenv("KIS_ACCOUNT_PROD", "01"),
        os.getenv("KIS_VIRTUAL", "false").lower() == "true",
    )


def reconstruct_round_trips(trades):
    """flat trades → 라운드트립 [{sym, market, exchange, entry_date, avg, exit_rate, ...}]."""
    def ts(x):
        try:
            return datetime.fromisoformat(x.get("timestamp", "").replace("Z", ""))
        except Exception:
            return None
    by_sym = defaultdict(list)
    for x in sorted([t for t in trades if ts(t)], key=ts):
        by_sym[x.get("code") or x.get("ticker")].append(x)

    rts = []
    for sym, lst in by_sym.items():
        qty = 0.0
        cost = 0.0          # 누적 매수금액
        first_date = None
        market = lst[0].get("market", "KR")
        exchange = lst[0].get("exchange", "NASD")
        for x in lst:
            q = x.get("qty", 0) or 0
            p = x.get("price", 0) or 0
            if x["type"] == "buy":
                if qty <= 1e-9:
                    first_date = x.get("date")
                    cost = 0.0
                qty += q
                cost += q * p
            elif x["type"] == "sell":
                if qty <= 1e-9:
                    continue
                avg = cost / qty if qty > 0 else 0
                sell_q = min(q, qty)
                # 라운드트립은 '포지션을 0으로 만드는 매도'에서 확정
                qty -= sell_q
                cost -= sell_q * avg
                if qty <= 1e-9 and avg > 0 and first_date:
                    rts.append(dict(
                        sym=sym, market=market, exchange=exchange,
                        entry_date=first_date, exit_date=x.get("date"),
                        avg=avg, exit_price=p,
                        realized_rate=(p - avg) / avg,
                        name=x.get("name") or x.get("ticker") or sym,
                    ))
                    qty = 0.0
                    cost = 0.0
    return rts


def fetch_ohlcv(client, rt):
    try:
        if rt["market"] == "US":
            df = client.get_us_daily_long(rt["sym"], rt.get("exchange", "NASD"), days=250)
        else:
            df = client.get_daily_long(rt["sym"], days=250)
        return df
    except Exception:
        return None


def ladder_lock(peak_rate):
    """수익 사다리 — 피크 +10%↑ 큰 추세 전용 락. 작은 피크는 None(기존 피크비례 보존 담당).

    이 메커니즘의 순수 한계효과만 측정하려고 +10% 미만은 건드리지 않음.
    """
    if peak_rate >= 0.30:
        return 0.24
    if peak_rate >= 0.20:
        return 0.15
    if peak_rate >= 0.15:
        return 0.11
    if peak_rate >= 0.10:
        return 0.06
    return None


def _clean_path(df, rt, horizon):
    """entry_date 이후 path 슬라이스 + 데이터 정합성 검증.

    avg 평단이 진입 직후 봉 가격대 안에 있어야 함 (액면분할/평단 오정합 행 제거).
    """
    import pandas as pd
    try:
        entry_dt = pd.to_datetime(rt["entry_date"])
        path = df.loc[df.index >= entry_dt].head(horizon)
    except Exception:
        return None
    if path is None or len(path) < 3:
        return None
    avg = rt["avg"]
    head = path.head(5)
    lo = float(head["low"].min()); hi = float(head["high"].max())
    # 평단이 진입 봉 가격대 ±15% 밖이면 데이터/재구성 오류 → 제외
    if avg <= 0 or avg < lo * 0.85 or avg > hi * 1.15:
        return None
    return path


def sim_ladder(df, rt, horizon=60):
    """진입 후 수익 사다리로 청산했다면? 반사실 청산 수익률 반환."""
    path = _clean_path(df, rt, horizon)
    if path is None:
        return None
    avg = rt["avg"]
    high = avg
    armed_ever = False
    for _, row in path.iterrows():
        high = max(high, float(row["high"]))
        peak = high / avg - 1
        lock = ladder_lock(peak)
        if lock is not None:
            armed_ever = True
            if float(row["low"]) <= avg * (1 + lock):  # 락 가격 체결 가정
                return lock
    # +10% 도달했으나 락 미히트 → 마지막 종가 / 아예 +10% 못 감 → 실제와 동일
    if armed_ever:
        return float(path.iloc[-1]["close"]) / avg - 1
    return rt["realized_rate"]


def prop_lock(peak_rate):
    """오늘 커밋된 피크 비례 보존 (peak-2.5%, 바닥 1.5%) — 사다리 비교 기준선."""
    if peak_rate < 0.03:
        return None
    return max(0.015, peak_rate - 0.025)


def sim_prop(df, rt, horizon=60):
    """커밋된 피크 비례 보존으로 라이딩 청산했다면? (사다리와 동일 라이딩 가정으로 공정 비교)."""
    path = _clean_path(df, rt, horizon)
    if path is None:
        return None
    avg = rt["avg"]
    high = avg
    armed_ever = False
    for _, row in path.iterrows():
        high = max(high, float(row["high"]))
        peak = high / avg - 1
        lock = prop_lock(peak)
        if lock is not None:
            armed_ever = True
            if float(row["low"]) <= avg * (1 + lock):
                return lock
    if armed_ever:
        return float(path.iloc[-1]["close"]) / avg - 1
    return rt["realized_rate"]


def sim_avg_down(df, rt, horizon=45, add_levels=(-0.05, -0.10), recover=0.03, hard=-0.15):
    """손실 라운드트립에 물타기 사다리 적용. 블렌디드 결과율 반환 (실패 시 None)."""
    path = _clean_path(df, rt, horizon)
    if path is None:
        return None
    avg0 = rt["avg"]
    shares = 1.0          # 최초 1단위
    cost = avg0
    adds_done = 0
    for _, row in path.iterrows():
        lo = float(row["low"]); hi = float(row["high"]); cl = float(row["close"])
        # 물타기: 원평단 대비 -5%, -10% 도달 시 1단위씩 추가 (최대 2회)
        while adds_done < len(add_levels) and lo <= avg0 * (1 + add_levels[adds_done]):
            add_px = avg0 * (1 + add_levels[adds_done])
            shares += 1.0
            cost += add_px
            adds_done += 1
        blended = cost / shares
        # 하드플로어: 원평단 -15% 이탈 → 전량 컷 (진짜 붕괴)
        if lo <= avg0 * (1 + hard):
            return ((avg0 * (1 + hard) * shares - cost) / cost, shares)
        # 회복: 블렌디드 +3% → 익절
        if hi >= blended * (1 + recover):
            return ((blended * (1 + recover) * shares - cost) / cost, shares)
    # 미해소 → 마지막 종가 평가
    return ((cl * shares - cost) / cost, shares)


def main():
    trades = json.load(open("data/trades.json"))
    rts = reconstruct_round_trips(trades)
    print(f"재구성된 라운드트립: {len(rts)}건")
    client = _client()

    # 심볼별 OHLCV 캐시 (API 절약)
    cache = {}

    def get_df(rt):
        if rt["sym"] not in cache:
            cache[rt["sym"]] = fetch_ohlcv(client, rt)
            time.sleep(0.3)
        return cache[rt["sym"]]

    # ── (A) 수익 사다리 vs 커밋된 피크비례 vs 실제 ──
    print("\n=== (A) 사다리 vs 피크비례(커밋됨) vs 실제 — 피크 +10%↑ 큰 추세만 ===")
    a_actual = a_ladder = a_prop = 0.0
    a_n = 0
    for rt in rts:
        df = get_df(rt)
        if df is None or df.empty:
            continue
        path = _clean_path(df, rt, 60)
        if path is None:
            continue
        peak = float(path["high"].max()) / rt["avg"] - 1
        if peak < 0.10:           # 큰 추세만 (사다리·피크비례 차이가 드러나는 구간)
            continue
        lad = sim_ladder(df, rt); prop = sim_prop(df, rt)
        if lad is None or prop is None:
            continue
        a_actual += rt["realized_rate"]; a_ladder += lad; a_prop += prop
        a_n += 1
        print(f"  {rt['name'][:14].ljust(14)} 피크 +{peak*100:4.1f}%  실제 {rt['realized_rate']*100:+5.1f}%  피크비례 {prop*100:+5.1f}%  사다리 {lad*100:+5.1f}%")
    if a_n:
        print(f"\n  큰추세 {a_n}건 평균:  실제 {a_actual/a_n*100:+.2f}%  →  피크비례 {a_prop/a_n*100:+.2f}%  →  사다리 {a_ladder/a_n*100:+.2f}%")
        print(f"  사다리 vs 피크비례 Δ{(a_ladder-a_prop)/a_n*100:+.2f}%p/건 (이게 +면 사다리 추가 가치, ~0이면 피크비례로 충분)")

    # ── (C) 물타기 사다리: 손실 라운드트립만 ──
    print("\n=== (C) 물타기 사다리 vs 실제 (손실 라운드트립만) ===")
    losers = [rt for rt in rts if rt["realized_rate"] < -0.005]
    # 기준 자본 1단위(=avg0)당 절대손익으로 집계 (실제는 1단위, 물타기는 최대 3단위 투입)
    actual_abs = avgd_abs = 0.0       # 단위당 절대 PnL (avg0=1 정규화)
    cap_deployed = 0.0
    c_n = recovered = worse = disaster = 0
    for rt in losers:
        df = get_df(rt)
        if df is None or df.empty:
            continue
        out = sim_avg_down(df, rt)
        if out is None:
            continue
        res, shares = out
        c_n += 1
        actual_abs += rt["realized_rate"]            # 1단위 투입
        avgd_abs += res * shares                     # shares단위 투입의 절대손익
        cap_deployed += shares
        if res > 0:
            recovered += 1
        if res < rt["realized_rate"] - 0.005:
            worse += 1
        if res <= -0.10:
            disaster += 1
        tag = "회복" if res > 0 else ("악화" if res < rt["realized_rate"] - 0.005 else "비슷")
        print(f"  {rt['name'][:14].ljust(14)} 실제 {rt['realized_rate']*100:+6.1f}%  물타기후 {res*100:+6.1f}% ({shares:.0f}배 투입)  {tag}")
    if c_n:
        print(f"\n  손실 {c_n}건 (자본 1단위 정규화):")
        print(f"   · 실제 합계(1배 투입)   : {actual_abs:+.2f} 단위")
        print(f"   · 물타기 합계(평균 {cap_deployed/c_n:.1f}배 투입): {avgd_abs:+.2f} 단위")
        print(f"   · 회복 {recovered}/{c_n}, 악화 {worse}/{c_n}, 재앙(-10%↓ on N배) {disaster}/{c_n}")
    print("\n* 일봉 근사. 핵심: 물타기는 자본 N배를 위험에 노출 — '평균율'이 아닌 '단위당 절대손익'과 재앙 빈도로 판단.")


if __name__ == "__main__":
    main()
