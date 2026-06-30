#!/usr/bin/env python3
"""실현 손익 분포 + 폭락/폭등 대처 정확성 리뷰 (운영자 수동 실행).

ExecStartPre 대상 아님 — 봇 기동과 무관한 분석 도구.

사용법:
  python3 pnl_review.py                # 손익 분포만 (API 불필요)
  python3 pnl_review.py --days 30      # 최근 30일 매도만
  python3 pnl_review.py --verify       # + 폭락/폭등 매도의 "대처 정확성" (KIS 시세 조회)
  python3 pnl_review.py --verify --lookforward 7

대처 정확성:
  - 손절/급락 컷  : 매도 후 가격이 더 빠졌으면 '정확'(추가손실 회피), 반등했으면 '성급'(바닥 투매)
  - 익절/급등 매도: 매도 후 가격이 더 올랐으면 '조기'(추가수익 놓침), 안 올랐으면 '적절'
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from zusik.paths import data_path, env_path
TRADES_FILE = data_path("trades.json")

# 보호성(손실 최소화) 매도 vs 수익실현 매도 분류
LOSS_CUT_PATTERNS = {"crash_instant", "forced_stop", "trailing_stop", "slow_bleed", "breakeven_protect"}
PROFIT_TAKE_PATTERNS = {"split_profit", "rsi_overbought"}


def load_sells(days: int | None = None) -> list[dict]:
    with open(TRADES_FILE, encoding="utf-8") as f:
        trades = json.load(f)
    sells = [t for t in trades if t.get("type") == "sell"]
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sells = [t for t in sells if t.get("date", "") >= cutoff]
    return sells


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def distribution_report(sells: list[dict]) -> None:
    pnls = [t.get("realized_pnl", 0) or 0 for t in sells]
    rates = [t.get("realized_rate", 0) or 0 for t in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    print("=" * 64)
    print(f"실현 손익 분포   (매도 {len(sells)}건)")
    print("=" * 64)
    if not pnls:
        print("매도 기록 없음")
        return

    gp, gl = sum(wins), sum(losses)
    pf = gp / abs(gl) if gl else float("inf")
    print(f"총 실현손익 : {sum(pnls):+,.0f}원")
    print(f"승률        : {len(wins)}/{len(wins) + len(losses)} = "
          f"{len(wins) / max(1, len(wins) + len(losses)) * 100:.1f}%")
    print(f"profit factor: {pf:.2f}  (총이익 {gp:+,.0f} / 총손실 {gl:+,.0f})")
    print(f"평균/중앙값 : {statistics.mean(pnls):+,.0f} / {statistics.median(pnls):+,.0f}원")
    print(f"최대익/최대손: {max(pnls):+,.0f} / {min(pnls):+,.0f}원")
    print(f"손익 분위수 : p10 {_pct(pnls, 10):+,.0f} | p25 {_pct(pnls, 25):+,.0f} | "
          f"p50 {_pct(pnls, 50):+,.0f} | p75 {_pct(pnls, 75):+,.0f} | p90 {_pct(pnls, 90):+,.0f}")
    print(f"수익률 분위 : p10 {_pct(rates, 10):+.1f}% | p50 {_pct(rates, 50):+.1f}% | "
          f"p90 {_pct(rates, 90):+.1f}%")

    # 간이 히스토그램 (수익률 구간)
    print("\n── 수익률 히스토그램 ──")
    bins = [(-99, -10), (-10, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 10), (10, 999)]
    for lo, hi in bins:
        n = len([r for r in rates if lo <= r < hi])
        label = f"{lo:+.0f}~{hi:+.0f}%" if hi < 999 else f"{lo:+.0f}%+"
        bar = "█" * n
        print(f"  {label:>10} | {bar} {n}")

    print("\n── 매도 패턴별 ──")
    pat: dict[str, list[float]] = defaultdict(list)
    for t in sells:
        pat[t.get("sell_pattern", "?")].append(t.get("realized_pnl", 0) or 0)
    for k, v in sorted(pat.items(), key=lambda x: sum(x[1])):
        w = len([p for p in v if p > 0])
        l = len([p for p in v if p < 0])
        wr = w / max(1, w + l) * 100
        print(f"  {k:18} n={len(v):3} 합 {sum(v):>11,.0f} 승률 {wr:3.0f}% 평균 {statistics.mean(v):>8,.0f}")

    print("\n── 시장별 ──")
    for mk in ("KR", "US"):
        g = [t.get("realized_pnl", 0) or 0 for t in sells if t.get("market") == mk]
        if g:
            w = len([p for p in g if p > 0])
            print(f"  {mk}: n={len(g):3} 합 {sum(g):>11,.0f} 승률 {w / len(g) * 100:.0f}%")


def accuracy_report(sells: list[dict], lookforward: int = 5) -> None:
    print("\n" + "=" * 64)
    print(f"폭락/폭등 대처 정확성   (매도 후 최대 {lookforward}봉 가격 대비)")
    print("=" * 64)
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path(), override=True)
        from zusik.clients.kis_client import KISClient
        import pandas as pd
        client = KISClient(
            app_key=os.getenv("KIS_APP_KEY"), app_secret=os.getenv("KIS_APP_SECRET"),
            account_no=os.getenv("KIS_ACCOUNT_NO"), account_prod=os.getenv("KIS_ACCOUNT_PROD", "01"),
            is_virtual=os.getenv("KIS_VIRTUAL", "true").lower() == "true",
        )
    except Exception as e:
        print(f"KIS 초기화 실패 — 정확성 검증 건너뜀: {e}")
        return
    try:
        fx = float(client.get_usd_krw_rate())
    except Exception:
        fx = 1500.0

    targets = [t for t in sells if t.get("sell_pattern") in (LOSS_CUT_PATTERNS | PROFIT_TAKE_PATTERNS)]
    if not targets:
        print("검증 대상(손절/급락/익절) 매도 없음")
        return

    cut = {"정확": 0, "성급": 0, "판정불가": 0}
    take = {"적절": 0, "조기": 0, "판정불가": 0}
    detail: list[str] = []

    for t in targets:
        code = t.get("code") or t.get("ticker")
        mk = t.get("market", "KR")
        sell_price = t.get("price", 0) or 0
        date = t.get("date", "")
        pat = t.get("sell_pattern", "")
        if not code or not sell_price or not date:
            continue
        try:
            if mk == "US":
                df = client.get_us_ohlcv(code, exchange=t.get("exchange", "NASD"))
            else:
                df = client.get_ohlcv(code)
        except Exception:
            df = None
        if df is None or len(df) == 0:
            (cut if pat in LOSS_CUT_PATTERNS else take)["판정불가"] += 1
            continue
        try:
            fwd = df[df.index > pd.Timestamp(date)].head(lookforward)
        except Exception:
            fwd = df.tail(lookforward)
        if len(fwd) == 0:
            (cut if pat in LOSS_CUT_PATTERNS else take)["판정불가"] += 1
            continue

        cur = fx if mk == "US" else 1.0  # US df는 USD → KRW 환산 (sell_price는 KRW)
        float(fwd["low"].min()) * cur
        fwd_max = float(fwd["high"].max()) * cur
        if pat in LOSS_CUT_PATTERNS:
            # 매도 후 가격이 별로 안 올랐으면(반등 < +3%) 컷이 정확, 크게 반등했으면 성급
            recovered = (fwd_max - sell_price) / sell_price if sell_price else 0
            verdict = "성급" if recovered >= 0.03 else "정확"
            cut[verdict] += 1
            detail.append(f"  [컷 {verdict}] {mk} {code} {date} {pat}: 매도 {sell_price:,} → "
                          f"이후 최고 {fwd_max:,.0f} ({recovered:+.1%})")
        else:
            # 매도 후 더 올랐으면(추가 상승 ≥ +3%) 조기 매도, 아니면 적절
            extra = (fwd_max - sell_price) / sell_price if sell_price else 0
            verdict = "조기" if extra >= 0.03 else "적절"
            take[verdict] += 1
            detail.append(f"  [익절 {verdict}] {mk} {code} {date} {pat}: 매도 {sell_price:,} → "
                          f"이후 최고 {fwd_max:,.0f} ({extra:+.1%})")

    print("손절/급락 컷:")
    tot_cut = sum(cut.values())
    if tot_cut:
        print(f"  정확(추가손실 회피) {cut['정확']} | 성급(반등, 바닥투매) {cut['성급']} | "
              f"판정불가 {cut['판정불가']}  → 정확도 {cut['정확'] / max(1, cut['정확'] + cut['성급']) * 100:.0f}%")
    print("익절/급등 매도:")
    tot_take = sum(take.values())
    if tot_take:
        print(f"  적절 {take['적절']} | 조기(추가수익 놓침) {take['조기']} | "
              f"판정불가 {take['판정불가']}  → 적시성 {take['적절'] / max(1, take['적절'] + take['조기']) * 100:.0f}%")
    print("\n── 상세 ──")
    for d in detail[:40]:
        print(d)


def _make_client():
    from dotenv import load_dotenv
    load_dotenv(env_path(), override=True)
    from zusik.clients.kis_client import KISClient
    return KISClient(
        app_key=os.getenv("KIS_APP_KEY"), app_secret=os.getenv("KIS_APP_SECRET"),
        account_no=os.getenv("KIS_ACCOUNT_NO"), account_prod=os.getenv("KIS_ACCOUNT_PROD", "01"),
        is_virtual=os.getenv("KIS_VIRTUAL", "true").lower() == "true",
    )


def counterfactual_report(sells: list[dict], horizons=(5, 10)) -> None:
    """현재 방식(패닉 매도 완화) 반사실 손익 — '바닥에서 안 팔고 보유했다면'.

    레거시 급락/손절 컷은 0% 정확(전부 반등)이었으므로, 신규 룰은 이들을 홀드한다.
    같은 종목·수량을 매도일 이후 N봉 종가까지 보유했다고 가정해 손익을 재계산.
    (자본 묶임 기회비용은 무시한 추정치 — 방향성·규모 가늠용)."""
    print("\n" + "=" * 64)
    print("반사실 백테스트 — 급락/손절 컷을 '홀드'했다면 (같은 기간)")
    print("=" * 64)
    try:
        import pandas as pd
        client = _make_client()
    except Exception as e:
        print(f"KIS 초기화 실패 — 반사실 계산 건너뜀: {e}")
        return

    cuts = [t for t in sells if t.get("sell_pattern") in LOSS_CUT_PATTERNS]
    if not cuts:
        print("대상(급락/손절) 매도 없음")
        return

    # trades.json 가격/avg는 KRW 기준이고 get_us_ohlcv는 USD → US는 fx 환산 필요.
    try:
        fx = float(client.get_usd_krw_rate())
    except Exception:
        fx = 1500.0
    horizons = sorted(horizons)
    agg = {h: 0.0 for h in horizons}
    agg_max = 0.0
    realized_sum = 0.0
    n_used = 0
    rows = []

    for t in cuts:
        code = t.get("code") or t.get("ticker")
        mk = t.get("market", "KR")
        qty = t.get("qty", 0) or 0
        avg = t.get("avg_buy_price", 0) or 0
        date = t.get("date", "")
        realized = t.get("realized_pnl", 0) or 0
        if not (code and qty and avg and date):
            continue
        try:
            df = (client.get_us_ohlcv(code, exchange=t.get("exchange", "NASD"))
                  if mk == "US" else client.get_ohlcv(code))
            fwd = df[df.index > pd.Timestamp(date)]
        except Exception:
            continue
        if df is None or len(fwd) == 0:
            continue
        cur = fx if mk == "US" else 1.0  # US df는 USD → KRW 환산
        realized_sum += realized
        n_used += 1
        row = {"code": code, "date": date, "pat": t.get("sell_pattern"), "realized": realized}
        for h in horizons:
            seg = fwd.head(h)
            exit_px = float(seg["close"].iloc[-1]) * cur if len(seg) else avg
            row[h] = (exit_px - avg) * qty
            agg[h] += row[h]
        cf_max = (float(fwd.head(max(horizons))["high"].max()) * cur - avg) * qty
        agg_max += cf_max
        row["max"] = cf_max
        rows.append(row)

    if not n_used:
        print("forward 시세 부족 — 판정 불가")
        return

    print(f"검증된 급락/손절 컷: {n_used}건")
    print(f"  실제 실현손익(컷)        : {realized_sum:+,.0f}원")
    for h in horizons:
        print(f"  반사실: {h}봉 후 종가 보유   : {agg[h]:+,.0f}원   (개선 {agg[h] - realized_sum:+,.0f})")
    print(f"  반사실: 구간 최고가 청산   : {agg_max:+,.0f}원   (개선 {agg_max - realized_sum:+,.0f})")
    print("\n  ※ '개선'은 같은 기간 같은 종목을 패닉 매도 대신 보유했을 때의 손익 차이 추정.")
    print("\n── 종목별 (실제 컷 → N봉 후 보유) ──")
    for r in sorted(rows, key=lambda x: x["realized"])[:20]:
        h0 = horizons[0]
        print(f"  {r['code']:8} {r['date']} {r['pat']:13} 실제 {r['realized']:>+10,.0f} → "
              f"{h0}봉보유 {r[h0]:>+10,.0f} | 최고가 {r['max']:>+10,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="실현 손익 분포 + 폭락/폭등 대처 정확성 리뷰")
    ap.add_argument("--days", type=int, default=None, help="최근 N일 매도만")
    ap.add_argument("--verify", action="store_true", help="KIS 시세로 대처 정확성 검증")
    ap.add_argument("--counterfactual", action="store_true",
                    help="급락/손절 컷을 '홀드'했다면(현재 방식) 손익 반사실 백테스트")
    ap.add_argument("--lookforward", type=int, default=5, help="매도 후 N봉으로 정확성 판정")
    args = ap.parse_args()

    sells = load_sells(args.days)
    distribution_report(sells)
    if args.verify:
        accuracy_report(sells, args.lookforward)
    if args.counterfactual:
        counterfactual_report(sells)


if __name__ == "__main__":
    main()
