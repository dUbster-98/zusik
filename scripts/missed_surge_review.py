#!/usr/bin/env python3
"""놓친 급등 분석 — 감시/후보 종목이 급등했는데 봇이 진입을 놓친 사례 + 기회비용 리포트.

'삼성전자가 급등했는데 타이밍을 놓치고 그 대신 손실 매매를 했다' 같은 상황을 데이터로 가시화한다.
종목별로 (1) 급등 구간을 탐지하고, (2) 그 구간에 봇이 보유/진입했는지 trades.json 으로 확인,
(3) 미보유면 '놓친 급등'으로 기록해 놓친 수익(기회비용)을 추정한다. 같은 기간 실제 실현손익과
대비해 봇이 어디서 방향을 잘못 잡았는지(놓친 상승 vs 실현 손익)를 보여준다.

  python3 scripts/missed_surge_review.py                  # KR 유니버스, 60일, +8% 급등
  python3 scripts/missed_surge_review.py --days 120 --surge-pct 0.10
  python3 scripts/missed_surge_review.py --us             # US 포함

읽기 전용. KIS API(시세) + data/trades.json. ExecStartPre/CI 대상 아님 — 운영자 수동 분석.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _normdate(x) -> str:
    """Timestamp/'YYYY-MM-DD'/'YYYYMMDD' → 'YYYY-MM-DD'."""
    s = str(x)
    if len(s) >= 10 and s[4:5] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _load_universe(config_path):
    import yaml
    cfg = yaml.safe_load(open(config_path, encoding="utf-8")) or {}
    kr = [(s.get("code"), s.get("name", s.get("code")))
          for s in (cfg.get("stocks") or []) if s.get("code")]
    us = [(s.get("ticker"), s.get("name", s.get("ticker")), s.get("exchange", "NASD"))
          for s in (cfg.get("us_stocks") or []) if s.get("ticker")]
    return kr, us


def _hold_fns(trades, code):
    """trades.json 으로 종목 보유 타임라인 구성 → (held_on, bought_between)."""
    evs = sorted([t for t in trades if t.get("code") == code and t.get("type") in ("buy", "sell")],
                 key=lambda t: t.get("date", ""))
    timeline, qty = [], 0
    for t in evs:
        q = int(t.get("qty", 0) or 0)
        qty += q if t["type"] == "buy" else -q
        timeline.append((t.get("date", ""), qty))

    def held_on(d):
        cur = 0
        for dd, q in timeline:
            if dd <= d:
                cur = q
            else:
                break
        return cur > 0

    def bought_between(d0, d1):
        return any(t["type"] == "buy" and d0 <= t.get("date", "") <= d1 for t in evs)
    return held_on, bought_between


def _find_surges(dates, closes, surge_pct, window):
    """forward window 봉 내 최고 상승률 ≥ surge_pct → 급등. 겹치는 구간은 건너뛴다."""
    surges, n, i = [], len(closes), 0
    while i < n - 1:
        seg = closes[i + 1:i + 1 + window]
        if not seg:
            break
        ret = (max(seg) - closes[i]) / closes[i] if closes[i] > 0 else 0.0
        if ret >= surge_pct:
            end_i = min(i + window, n - 1)
            surges.append((dates[i], dates[end_i], ret))
            i = end_i + 1
        else:
            i += 1
    return surges


def main() -> int:
    p = argparse.ArgumentParser(description="놓친 급등 분석 (기회비용)")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--surge-pct", type=float, default=0.08, help="급등 판정 최소 상승률 (0.08=+8%%)")
    p.add_argument("--window", type=int, default=5, help="급등 판정 forward 봉 수")
    p.add_argument("--capital", type=int, default=1_000_000, help="놓친 수익 추정용 가정 포지션(원)")
    p.add_argument("--us", action="store_true")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()

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

    kr, us = _load_universe(args.config)
    targets = [("KR", c, n, None) for c, n in kr]
    if args.us:
        targets += [("US", t, n, e) for t, n, e in us]

    missed, captured, skipped = [], 0, 0
    for market, sym, name, exch in targets:
        try:
            if market == "KR":
                df = client.get_daily_long(sym, days=args.days)
            else:
                df = client.get_us_ohlcv(sym, exchange=exch, period="D")
                if df is not None:
                    df = df.tail(args.days + 5)
        except Exception:
            skipped += 1
            continue
        if df is None or len(df) < 10:
            skipped += 1
            continue
        # 날짜: get_daily_long 은 date 를 인덱스로 둔다(set_index). column 우선, 없으면 index.
        raw = df["date"].tolist() if "date" in df.columns else df.index.tolist()
        dates = [_normdate(x) for x in raw]
        # 실제 날짜(YYYY-MM-DD)가 아니면 보유 매칭 불가 → 스킵
        if not dates or "-" not in dates[-1]:
            skipped += 1
            continue
        closes = [float(x) for x in df["close"].tolist()]
        held_on, bought_between = _hold_fns(trades, sym)
        for start, end, ret in _find_surges(dates, closes, args.surge_pct, args.window):
            if held_on(start) or bought_between(start, end):
                captured += 1
            else:
                missed.append((market, sym, name, start, end, ret, int(ret * args.capital)))

    print(f"\n놓친 급등 분석 — {len(targets)}종목 · {args.days}일 · 급등기준 +{args.surge_pct*100:.0f}%/{args.window}봉")
    print("=" * 62)
    if not missed:
        print(f"놓친 급등 없음 (포착 {captured}건, 스킵 {skipped}). 감시 종목 급등을 잘 잡고 있음.")
        return 0

    print(f"{'시장':<4} {'종목':<14} {'급등 시작':<12} {'상승':>6} {'놓친 추정':>13}")
    print("-" * 62)
    tot = 0
    for market, sym, name, start, end, ret, est in sorted(missed, key=lambda x: -x[5]):
        print(f"{market:<4} {name[:14]:<14} {start:<12} {ret*100:>+5.1f}% {est:>+13,}")
        tot += est

    # 같은 기간(가장 이른 놓친 급등 이후) 실제 실현손익 — 놓친 상승 대비 '방향' 점검
    anchor = min(m[3] for m in missed)
    realized = sum(int(t.get("realized_pnl", 0) or 0) for t in trades
                   if t.get("type") == "sell" and t.get("date", "") >= anchor)
    print("-" * 62)
    print(f"놓친 급등 {len(missed)}건 · 포착 {captured}건 · 스킵 {skipped} · "
          f"놓친 기회비용 추정(등가중) {tot:>+,}원")
    print(f"같은 기간({anchor}~) 실제 실현손익 {realized:>+,}원  ← 놓친 상승 대비 '방향' 점검")
    if realized < 0 and tot > 0:
        print("[주의] 상승을 놓치는 동안 실현손익은 마이너스 — 진입 타이밍/방향 재점검 권장 "
              "(fast_entry 모멘텀 하한·매수 게이트).")
    print("\n(기회비용은 '가정 포지션 × 상승률' 추정치 — 실제 진입가/체결은 다를 수 있음)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
