#!/usr/bin/env python3
"""유니버스 장기 수익 백테스트 — "이 전략이 긴 실제 기간에 실제로 돈을 버는가" 검증.

`config.yaml` 의 종목 유니버스(stocks + us_stocks)를 장기 일봉으로 각각
`zusik.analysis.backtest.simulate` 에 태우고, 포트폴리오 차원의 수익(총 수익률·실현손익·승률·
매도패턴)을 합산 리포트한다. 단일 종목 `backtest.py` 의 엔진을 그대로 재사용한다(중복 없음).

  python3 scripts/earnings_backtest.py                       # KR 유니버스, 250봉(~1년), momentum_breakout
  python3 scripts/earnings_backtest.py --days 750            # ~3년 장기
  python3 scripts/earnings_backtest.py --strategy adaptive   # 봇의 로컬 선택(느림)
  python3 scripts/earnings_backtest.py --us                  # US 티커도 포함(데이터 길이는 API 의존)

KIS API · .env 필요. 빠른 단위 게이트(tests/test_bot.py)와 달리 **실데이터·시간 소요** 라
ExecStartPre/CI 기본 게이트 대상이 아니다 — 운영자 수동 검증 또는 나이틀리 cron 용.
종료코드: 포트폴리오 수익률 < 0 이면 1 → "돈 버는지" 자동 게이트로도 쓸 수 있다.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict


def _load_universe(config_path: str):
    import yaml
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    kr = [(s.get("code"), s.get("name", s.get("code")))
          for s in (cfg.get("stocks") or []) if s.get("code")]
    us = [(s.get("ticker"), s.get("name", s.get("ticker")), s.get("exchange", "NASD"))
          for s in (cfg.get("us_stocks") or []) if s.get("ticker")]
    return kr, us


def main() -> int:
    parser = argparse.ArgumentParser(description="유니버스 장기 수익 백테스트")
    parser.add_argument("--days", type=int, default=250, help="백테스트 기간(봉). 250≈1년, 750≈3년")
    parser.add_argument("--strategy", default="momentum_breakout",
                        help="로컬 전략 (adaptive 는 봇 로컬 선택이나 느림)")
    parser.add_argument("--capital", type=int, default=1_000_000, help="종목당 초기 자본(원, 등가중)")
    parser.add_argument("--us", action="store_true", help="US 티커도 포함")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--min-bars", type=int, default=60, help="이 미만이면 종목 스킵")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    from zusik.analysis import backtest
    from zusik.clients.kis_client import KISClient

    client = KISClient(
        os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", ""),
        os.getenv("KIS_ACCOUNT_NO", ""), os.getenv("KIS_ACCOUNT_PROD", "01"),
        os.getenv("KIS_VIRTUAL", "false").lower() == "true",
    )
    kr, us = _load_universe(args.config)
    targets = [("KR", c, n, None) for c, n in kr]
    if args.us:
        targets += [("US", t, n, e) for t, n, e in us]
    if not targets:
        print("유니버스가 비었습니다 (config.stocks / us_stocks).")
        return 2

    print(f"▶ 장기 수익 백테스트 — {len(targets)}종목 · {args.days}봉 · 전략 {args.strategy} · "
          f"종목당 {args.capital:,}원")

    rows, skipped = [], []
    agg_init = agg_final = 0
    agg_wins = agg_losses = 0
    agg_pat = defaultdict(lambda: {"count": 0, "wins": 0, "pnl_sum": 0})

    for market, sym, name, exch in targets:
        try:
            if market == "KR":
                df = client.get_daily_long(sym, days=args.days)
            else:
                df = client.get_us_ohlcv(sym, exchange=exch, period="D")
                if df is not None:
                    df = df.tail(args.days + 20).reset_index(drop=True)
        except Exception as e:
            skipped.append((sym, name, f"조회 실패: {str(e)[:50]}"))
            continue
        if df is None or len(df) < args.min_bars:
            skipped.append((sym, name, f"데이터 부족 ({0 if df is None else len(df)}봉)"))
            continue
        strat = backtest._build_strategy(args.strategy)   # 종목마다 새 인스턴스 (상태 격리)
        r = backtest.simulate(df, strat, initial_capital=args.capital)
        rows.append((market, sym, name, r))
        agg_init += r["initial_capital"]
        agg_final += r["final_value"]
        agg_wins += r["wins"]
        agg_losses += r["losses"]
        for p, s in r["pattern_stats"].items():
            agg_pat[p]["count"] += s["count"]
            agg_pat[p]["wins"] += s["wins"]
            agg_pat[p]["pnl_sum"] += s["pnl_sum"]

    if not rows:
        print("백테스트 가능한 종목이 없습니다 (데이터 부족).")
        for s, n, why in skipped:
            print(f"  - {n}({s}): {why}")
        return 2

    # ── 종목별 ──
    print(f"\n{'시장':<4} {'종목':<18} {'수익률':>9} {'매도':>4} {'승률':>5}")
    print("-" * 48)
    for market, sym, name, r in sorted(rows, key=lambda x: -x[3]["return_rate"]):
        print(f"{market:<4} {name[:18]:<18} {r['return_rate']:>+8.1f}% {r['sells']:>4} {r['win_rate']:>4.0f}%")

    # ── 포트폴리오 합산 ──
    port_ret = (agg_final - agg_init) / agg_init * 100 if agg_init else 0.0
    realized = agg_final - agg_init
    win_rate = (agg_wins / (agg_wins + agg_losses) * 100) if (agg_wins + agg_losses) else 0.0
    print("\n" + "=" * 48)
    print(f"포트폴리오 (등가중 {len(rows)}종목 · {args.days}봉)")
    print("=" * 48)
    print(f"  총 투입   : {agg_init:>14,}원")
    print(f"  총 평가   : {agg_final:>14,}원")
    print(f"  수익률    : {port_ret:>+13.2f}%")
    print(f"  실현손익  : {realized:>+14,}원")
    print(f"  승률      : {win_rate:>13.1f}% ({agg_wins}승 {agg_losses}패)")
    if skipped:
        print(f"  스킵      : {len(skipped)}종목 (데이터 부족/조회 실패)")
    if agg_pat:
        print("\n  매도 패턴 분포(전 종목 합산):")
        for pat, s in sorted(agg_pat.items(), key=lambda x: -x[1]["pnl_sum"]):
            rate = (s["wins"] / s["count"] * 100) if s["count"] else 0
            print(f"    {pat:<20s} {s['count']:>4d}건 · 승률 {rate:>3.0f}% · 총 {s['pnl_sum']:>+12,d}원")
    print()
    print("수익률 > 0 → exit 0 / 손실 → exit 1 (나이틀리 cron 게이트용)")
    return 0 if port_ret >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
