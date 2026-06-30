#!/usr/bin/env python3
"""매도 타이밍 사후분석 — 팔고 난 뒤 종목이 얼마나 더 올랐나(놓친 상승)/빠졌나(막은 하락).

각 매도를 당일 장중 + forward(1/3/5/10 거래일)로 추적해 sell_pattern 별로 집계한다.
'어떤 매도 패턴이 상승을 놓치고(조기매도), 어떤 패턴이 하락을 막았나(보호 성공)'를 데이터로
보여줘 매도 타이밍을 어디서 고쳐야 수익이 느는지 가리킨다. 결과를 data/sell_timing.json 에
캐시해 리포트(results HTML)가 읽는다.

  python3 scripts/sell_timing_review.py                 # 전체 매도, 150일 시세
  python3 scripts/sell_timing_review.py --primary 3 --days 200

읽기 전용. KIS API(시세) + data/trades.json. ExecStartPre/CI 대상 아님 — 운영자 수동 분석.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zusik.reporting.sell_timing import _normdate, analyze_sell_timing  # noqa: E402


def _make_fetcher(client, days):
    def fetch(market, symbol, exchange):
        if market == "KR":
            df = client.get_daily_long(symbol, days=days)
        else:
            try:
                df = client.get_us_daily_long(symbol, exchange=exchange, days=days)
            except Exception:
                df = client.get_us_ohlcv(symbol, exchange=exchange, period="D")
        if df is None or len(df) < 2:
            return None
        raw = df["date"].tolist() if "date" in df.columns else df.index.tolist()
        dates = [_normdate(x) for x in raw]
        lows = [float(x) for x in df["low"].tolist()]
        highs = [float(x) for x in df["high"].tolist()]
        closes = [float(x) for x in df["close"].tolist()]
        order = sorted(range(len(dates)), key=lambda i: dates[i])  # 날짜 오름차순 정렬
        dates = [dates[i] for i in order]
        lows = [lows[i] for i in order]
        highs = [highs[i] for i in order]
        closes = [closes[i] for i in order]
        if not dates or "-" not in dates[-1]:
            return None
        return {"dates": dates, "lows": lows, "highs": highs, "closes": closes}
    return fetch


def main() -> int:
    ap = argparse.ArgumentParser(description="매도 타이밍 사후분석(읽기 전용)")
    ap.add_argument("--days", type=int, default=150, help="시세 조회 기간(거래일 forward 확보용)")
    ap.add_argument("--primary", type=int, default=5, help="패턴 분류 기준 horizon(거래일)")
    ap.add_argument("--json", default=os.path.join("data", "sell_timing.json"))
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

    res = analyze_sell_timing(trades, _make_fetcher(client, args.days), primary=args.primary)
    bp, ov = res["by_pattern"], res["overall"]

    print(f"\n매도 타이밍 사후분석 — 분석 {ov['analyzed']}건 (보류 {ov['pending']}건, "
          f"기준 {args.primary}거래일)")
    print("=" * 92)
    print(f"  {'패턴':<16}{'건수':>4}  {'당일놓침':>8}  {'놓친상승':>8}  {'막은하락':>8}  "
          f"{'홀드종가':>8}  {'조기/보호':>9}  판정")
    print("-" * 92)
    for pat, s in bp.items():
        print(f"  {pat:<16}{s['count']:>4}  {s['avg_same_day_missed']:>7.2f}%  "
              f"{s['avg_missed_upside']:>7.2f}%  {s['avg_avoided_drop']:>7.2f}%  "
              f"{s['avg_net_if_held']:>7.2f}%  {s['too_early']:>3}/{s['protected']:<4}  {s['verdict']}")
    print("-" * 92)
    print(f"  {'전체':<16}{ov['analyzed']:>4}  {ov['avg_same_day_missed']:>7.2f}%  "
          f"{ov['avg_missed_upside']:>7.2f}%  {ov['avg_avoided_drop']:>7.2f}%")
    print("\n  · 당일놓침 = 팔고 나서 같은 날 장중 최대 추가 상승%")
    print("  · 놓친상승/막은하락 = 매도 후 N거래일 forward high/low. 홀드종가 = N거래일 뒤 종가 기준")
    print("  · '조기매도' 판정 패턴은 더 늦게(defer) 팔도록, '보호 성공'은 유지가 데이터 권고")

    payload = {"by_pattern": bp, "overall": ov, "primary": args.primary,
               "horizons": res["horizons"]}
    try:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n  캐시 저장: {args.json} (리포트가 읽음)")
    except Exception as e:
        print(f"\n  캐시 저장 실패: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
