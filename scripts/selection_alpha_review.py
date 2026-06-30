#!/usr/bin/env python3
"""종목선택 평가 — 고른 종목이 지수 대비 얼마나 더 벌었나(alpha) + 그날 놓친 최고 상승종목.

각 매수의 보유 window 수익을 지수 프록시(KR 069500 / US QQQ) 대비 비교하고, 유니버스(config)
기준으로 매수일별 '놓친 최고 상승종목'을 찾아 봇 선택이 얼마나 뒤처졌는지 본다. 결과를
data/selection_alpha.json 에 캐시해 리포트(results HTML)가 읽는다.

  python3 scripts/selection_alpha_review.py                 # KR 유니버스, 10거래일
  python3 scripts/selection_alpha_review.py --window 5 --us --days 150

읽기 전용. KIS API(시세) + data/trades.json + config 유니버스. 운영자 수동 분석.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zusik.reporting.sell_timing import _normdate  # noqa: E402
from zusik.reporting.selection_alpha import analyze_selection_alpha  # noqa: E402

INDEX_PROXY = {"KR": ("069500", "NASD"), "US": ("QQQ", "NASD")}


def _series_from_df(df):
    if df is None or len(df) < 2:
        return None
    raw = df["date"].tolist() if "date" in df.columns else df.index.tolist()
    dates = [_normdate(x) for x in raw]
    closes = [float(x) for x in df["close"].tolist()]
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    dates = [dates[i] for i in order]
    closes = [closes[i] for i in order]
    if not dates or "-" not in dates[-1]:
        return None
    return {"dates": dates, "closes": closes}


def _make_fetcher(client, days):
    def fetch(market, symbol, exchange):
        if market == "KR":
            return _series_from_df(client.get_daily_long(symbol, days=days))
        try:
            return _series_from_df(client.get_us_daily_long(symbol, exchange=exchange, days=days))
        except Exception:
            return _series_from_df(client.get_us_ohlcv(symbol, exchange=exchange, period="D"))
    return fetch


def _load_universe(config_path, include_us):
    import yaml
    cfg = yaml.safe_load(open(config_path, encoding="utf-8")) or {}
    uni = [("KR", s.get("code"), "NASD", s.get("name", s.get("code")))
           for s in (cfg.get("stocks") or []) if s.get("code")]
    if include_us:
        uni += [("US", s.get("ticker"), s.get("exchange", "NASD"), s.get("name", s.get("ticker")))
                for s in (cfg.get("us_stocks") or []) if s.get("ticker")]
    return uni


def main() -> int:
    ap = argparse.ArgumentParser(description="종목선택 alpha 평가(읽기 전용)")
    ap.add_argument("--window", type=int, default=10, help="보유 평가 기간(거래일)")
    ap.add_argument("--days", type=int, default=150, help="시세 조회 기간")
    ap.add_argument("--us", action="store_true", help="US 유니버스 포함")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--json", default=os.path.join("data", "selection_alpha.json"))
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    from zusik.clients.kis_client import KISClient
    client = KISClient(os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", ""),
                       os.getenv("KIS_ACCOUNT_NO", ""), os.getenv("KIS_ACCOUNT_PROD", "01"),
                       os.getenv("KIS_VIRTUAL", "false").lower() == "true")
    try:
        trades = json.load(open(os.path.join("data", "trades.json"), encoding="utf-8"))
    except Exception:
        trades = []

    fetch = _make_fetcher(client, args.days)

    def index_fetch(market):
        sym, exch = INDEX_PROXY.get(market, ("069500", "NASD"))
        return fetch(market, sym, exch)

    universe = _load_universe(args.config, args.us)
    res = analyze_selection_alpha(trades, fetch, index_fetch,
                                  window=args.window, universe=universe)
    a = res["alpha"]

    print(f"\n종목선택 평가 — 매수 {a['count']}건, 보유 {args.window}거래일 기준")
    print("=" * 64)
    print(f"  평균 종목수익  {a['avg_pick_return']:+.2f}%")
    print(f"  평균 지수수익  대비 alpha  {a['avg_alpha']:+.2f}%p   (지수 초과 비율 {a['beat_index_rate']}%)")
    for mk, m in res["by_market"].items():
        print(f"   - {mk}: {m['count']}건  종목 {m['avg_pick_return']:+.2f}%  "
              f"alpha {m['avg_alpha']:+.2f}%p  초과율 {m['beat_index_rate']}%")
    mb = res["missed_best"]
    if mb:
        print(f"\n  놓친 최고종목 ({mb['days']}일 분석)")
        print(f"   봇 최선 픽 평균 {mb['avg_bot_best_return']:+.2f}%  vs  "
              f"놓친 최고 평균 {mb['avg_missed_best_return']:+.2f}%  → gap {mb['avg_gap']:+.2f}%p")
        for d in mb["days_detail"][-8:]:
            print(f"     {d['date']}  봇 {d['bot_best']['name']}({d['bot_best']['ret']:+.1f}%) "
                  f"| 놓침 {d['missed_best']['name']}({d['missed_best']['ret']:+.1f}%)")
    print("\n  · alpha>0 = 지수보다 더 벌었다(선택 유효). gap = 그날 최고 상승종목을 얼마나 놓쳤나")

    try:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"alpha": a, "by_market": res["by_market"],
                       "missed_best": mb, "window": args.window}, f,
                      ensure_ascii=False, indent=2)
        print(f"\n  캐시 저장: {args.json} (리포트가 읽음)")
    except Exception as e:
        print(f"\n  캐시 저장 실패: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
