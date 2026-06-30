#!/usr/bin/env python3
"""장휴장 시간에 KIS API로 학습 자료 대량 수집.

수집 내용:
  1. 종목별 OHLCV 시계열 → data/ohlcv_cache/*.json
  2. 각 종목 × 로컬 전략 8종 백테스트 → data/backtest_warmup.json
  3. 전략·패턴별 종합 성과 리포트 (stdout)

대상 종목:
  - config.yaml의 stocks / us_stocks
  - smart_signals.KR_INVERSE_ETF / US_INVERSE_ETF
  - 지수 프록시 (KODEX 200, QQQ, SPY)
  - trades.json에 등장한 과거 거래 종목

봇이 돌고 있어도 KIS 토큰은 공유되고 rate limiter가 작동하므로 안전.
실행 시간: ~3~5분 (종목 수에 따라).
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime

from zusik.paths import ROOT
os.chdir(str(ROOT))

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")

from dotenv import load_dotenv
load_dotenv()

import yaml
with open("config.yaml") as f:
    config = yaml.safe_load(f)

from zusik.clients.kis_client import KISClient
from zusik.analysis.smart_signals import SmartSignals
from zusik.analysis.backtest import simulate, _build_strategy, LOCAL_STRATEGIES

print("=" * 60)
print(f"▶ 학습 자료 수집 시작 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
print("=" * 60)

client = KISClient(
    os.getenv("KIS_APP_KEY", ""),
    os.getenv("KIS_APP_SECRET", ""),
    os.getenv("KIS_ACCOUNT_NO", ""),
    os.getenv("KIS_ACCOUNT_PROD", "01"),
    os.getenv("KIS_VIRTUAL", "false").lower() == "true",
)

# ── 1) 종목 리스트 구성 ──
kr_codes: set[str] = set()
for s in config.get("stocks", []):
    if s.get("code"):
        kr_codes.add(s["code"])
kr_codes.update(SmartSignals.KR_INVERSE_ETF.keys())
kr_codes.add("069500")  # KODEX 200 프록시

us_tickers: set[tuple[str, str]] = set()
for s in config.get("us_stocks", []):
    if s.get("ticker"):
        us_tickers.add((s["ticker"], s.get("exchange", "NASD")))
for t, meta in SmartSignals.US_INVERSE_ETF.items():
    us_tickers.add((t, meta.get("exchange", "NASD")))
us_tickers.add(("QQQ", "NASD"))
us_tickers.add(("SPY", "AMEX"))

# 과거 거래 종목 추가
try:
    with open("data/trades.json") as f:
        trades = json.load(f)
    for t in trades:
        mk = t.get("market")
        if mk == "KR" and t.get("code") and t["code"].isdigit():
            kr_codes.add(t["code"])
        elif mk == "US" and (t.get("ticker") or t.get("code")):
            us_tickers.add((t.get("ticker") or t["code"], "NASD"))
except Exception as e:
    print(f"  [warn] trades.json 로드 실패: {e}")

kr_codes = sorted(kr_codes)
us_tickers = sorted(us_tickers)
print(f"\n대상: KR {len(kr_codes)}종목 / US {len(us_tickers)}티커", flush=True)
print(f"  KR: {', '.join(kr_codes[:10])}{'...' if len(kr_codes) > 10 else ''}", flush=True)
print(f"  US: {', '.join(t for t, _ in us_tickers[:10])}{'...' if len(us_tickers) > 10 else ''}", flush=True)

# ── 2) OHLCV 수집 ──
CACHE_DIR = "data/ohlcv_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

kr_dfs: dict[str, object] = {}
print("\n[1/3] KR OHLCV 수집...", flush=True)
for code in kr_codes:
    try:
        df = client.get_ohlcv(code, period="D")
        if df is None or len(df) == 0:
            print(f"  {code}: 데이터 없음")
            continue
        kr_dfs[code] = df
        df.to_json(f"{CACHE_DIR}/KR_{code}.json", orient="records", date_format="iso")
        print(f"  {code}: {len(df)}봉 저장")
    except Exception as e:
        print(f"  {code}: 실패 ({str(e)[:60]})")
    time.sleep(0.4)  # rate limit 여유

us_dfs: dict[tuple[str, str], object] = {}
print("\n[2/3] US OHLCV 수집...", flush=True)
for ticker, exchange in us_tickers:
    try:
        df = client.get_us_ohlcv(ticker, exchange=exchange, period="D")
        if df is None or len(df) == 0:
            print(f"  {ticker}: 데이터 없음")
            continue
        us_dfs[(ticker, exchange)] = df
        df.to_json(f"{CACHE_DIR}/US_{ticker}.json", orient="records", date_format="iso")
        print(f"  {ticker}/{exchange}: {len(df)}봉 저장")
    except Exception as e:
        print(f"  {ticker}: 실패 ({str(e)[:60]})")
    time.sleep(0.4)

# ── 3) 백테스트 매트릭스 ──
print(f"\n[3/3] 전략 백테스트 매트릭스 ({len(LOCAL_STRATEGIES)}전략 × {len(kr_dfs) + len(us_dfs)}종목)...", flush=True)
results: dict = {"KR": {}, "US": {}}
total_runs = 0
start = time.time()

for code, df in kr_dfs.items():
    if len(df) < 30:
        continue
    results["KR"][code] = {}
    for sname in LOCAL_STRATEGIES:
        try:
            strategy = _build_strategy(sname)
            r = simulate(df, strategy, initial_capital=1_000_000)
            results["KR"][code][sname] = {
                "return_rate": r["return_rate"],
                "win_rate": r["win_rate"],
                "total_trades": r["total_trades"],
                "pattern_stats": r["pattern_stats"],
            }
            total_runs += 1
        except Exception as e:
            print(f"  KR {code}/{sname} 실패: {str(e)[:60]}")

for (ticker, exchange), df in us_dfs.items():
    if len(df) < 30:
        continue
    results["US"][ticker] = {}
    for sname in LOCAL_STRATEGIES:
        try:
            strategy = _build_strategy(sname)
            r = simulate(df, strategy, initial_capital=1_000_000)
            results["US"][ticker][sname] = {
                "return_rate": r["return_rate"],
                "win_rate": r["win_rate"],
                "total_trades": r["total_trades"],
                "pattern_stats": r["pattern_stats"],
            }
            total_runs += 1
        except Exception as e:
            print(f"  US {ticker}/{sname} 실패: {str(e)[:60]}")

elapsed = time.time() - start
print(f"  ▶ {total_runs}회 백테스트 완료 ({elapsed:.1f}초)", flush=True)

# ── 4) 저장 ──
os.makedirs("data", exist_ok=True)
with open("data/backtest_warmup.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n저장: data/backtest_warmup.json", flush=True)
print(f"   OHLCV 캐시: {CACHE_DIR}/ (총 {len(kr_dfs) + len(us_dfs)}종목)", flush=True)

# ── 5) 종합 리포트 ──
print(f"\n{'=' * 60}", flush=True)
print("전략별 평균 성과 (KR+US 전체)", flush=True)
print("=" * 60)

strategy_perf: dict[str, list] = defaultdict(lambda: [0, 0.0, 0.0, 0])
pattern_dist: dict[str, int] = defaultdict(int)
pattern_pnl: dict[str, int] = defaultdict(int)

for market in ("KR", "US"):
    for symbol, strats in results[market].items():
        for s, r in strats.items():
            strategy_perf[s][0] += 1
            strategy_perf[s][1] += r["return_rate"]
            strategy_perf[s][2] += r["win_rate"]
            strategy_perf[s][3] += r["total_trades"]
            for pat, pstat in r.get("pattern_stats", {}).items():
                pattern_dist[pat] += pstat["count"]
                pattern_pnl[pat] += pstat["pnl_sum"]

print(f"{'전략':<22s} {'평균 수익률':>12s} {'평균 승률':>10s} {'평균 거래':>8s}", flush=True)
print("─" * 56)
for s, (n, ret, wr, tr) in sorted(strategy_perf.items(), key=lambda x: -x[1][1] / max(x[1][0], 1)):
    if n == 0:
        continue
    print(f"  {s:<20s} {ret / n:>+10.2f}% {wr / n:>9.1f}% {tr / n:>7.1f}")

print("\n전체 매도 패턴 분포", flush=True)
print("─" * 56)
print(f"{'pattern':<22s} {'건수':>6s} {'총 PnL':>14s} {'건당':>12s}", flush=True)
for pat, cnt in sorted(pattern_dist.items(), key=lambda x: -pattern_pnl[x[0]]):
    avg = pattern_pnl[pat] // cnt if cnt else 0
    print(f"  {pat:<20s} {cnt:>6d} {pattern_pnl[pat]:>+14,d} {avg:>+12,d}")

print(f"\n완료: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
