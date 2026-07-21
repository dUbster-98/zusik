#!/usr/bin/env python3
"""급락 hold-through 사후검증 — '급락일에 던졌다면' 이득이었나 손해였나.

position.whitelist_crash_exempt(핵심주 급락 면제)가 +EV 인지 데이터로 재확인하는 도구.
급락일을 두 부류로 나눠 forward 수익률을 비교한다:

  시장급락 — risk.fast_fall_guard 조건 충족(지수 급락 동반). 봇이 '급락 가드'로 인지하는 날.
  개별악재 — 지수는 멀쩡한데 종목만 급락(실적 쇼크 등).

1차 판정 기준은 **절대 forward 수익률**이다. 급락 매도는 _register_reentry_block 으로
24시간 재진입이 막히므로 이 봇의 실제 선택지는 '보유 vs 현금'이다. forward>0 이면 홀드가
현금을 이긴 것 = 판 게 손해. 기저율(같은 기간 아무 날 보유) 대비는 '그 자본을 평균적인
날에 넣었다면'이라는 기회비용 관점이라 참고 지표로만 본다. 평균>0 인데 중앙<0 이면 소수
급반등이 평균을 끌어올린 것이라 중앙값을 더 신뢰할 것.

2026-07-21 최초 실행 (5년·NVDA/AMD/INTC·지수 QQQ·20거래일):
  고정 -7% 기준   시장급락 52건 평균 +5.6%/중앙 +6.7%  → 홀드 우위 (기저대비도 +1.4%p)
  config 임계 기준 시장급락 59건 평균 +3.2%/중앙 +3.0%  → 홀드 우위 (기저대비는 -0.9%p)
  두 기준 모두   개별악재는 중앙값 마이너스(-2.8%), 플러스 비율 33~35% → 컷이 유리한 쪽
  지속 하락장(지수<200MA)에서도 시장급락 급락일은 20일 후 플러스 (평균 +3.6~7.9%)
→ '시장 급락 시 핵심주 면제를 해제한다'는 개선안을 이 결과로 기각했다. 개별악재 컷은
   방향은 유망하나 표본(12~26건)이 얇아 보류. whitelist 구성이나 crash 임계를 바꾸면
   다시 돌려 판단을 갱신할 것.

  python3 scripts/crash_hold_review.py                        # config whitelist, 5년
  python3 scripts/crash_hold_review.py --tickers NVDA,AMD --days 750
  python3 scripts/crash_hold_review.py --horizon 10 --no-cache

읽기 전용. KIS API(시세) + config.yaml. ExecStartPre/CI 대상 아님 — 운영자 수동 분석.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE_DIR = os.path.join("data", "ohlcv_cache")
OUT_JSON = os.path.join("data", "crash_hold_review.json")


def _client():
    from dotenv import load_dotenv
    load_dotenv()
    from zusik.clients.kis_client import KISClient
    return KISClient(
        os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", ""),
        os.getenv("KIS_ACCOUNT_NO", ""), os.getenv("KIS_ACCOUNT_PROD", "01"),
        os.getenv("KIS_VIRTUAL", "false").lower() == "true",
    )


def _load_config() -> dict:
    import yaml
    cfg = {}
    for name in ("config.yaml", "config.local.yaml"):
        if os.path.exists(name):
            try:
                with open(name, encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                for k, v in loaded.items():
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
            except Exception:
                pass
    return cfg


def _fetch(client, ticker: str, exchange: str, days: int, use_cache: bool):
    """일봉 DataFrame (date 오름차순). data/ohlcv_cache 재사용.

    거래소가 틀리면 KIS 는 예외가 아니라 빈 결과를 준다(SPY 는 NYSE 가 아니라 AMEX).
    지정 거래소 실패 시 나머지를 순회해 '데이터 없음'과 '거래소 오지정'을 구분한다.
    """
    import pandas as pd
    os.makedirs(CACHE_DIR, exist_ok=True)
    fp = os.path.join(CACHE_DIR, f"crashrev_US_{ticker}_{days}.json")
    if use_cache and os.path.exists(fp):
        try:
            return pd.DataFrame(json.load(open(fp, encoding="utf-8")))
        except Exception:
            pass
    df = None
    tried = [exchange] + [e for e in ("NASD", "NYSE", "AMEX") if e != exchange]
    for ex in tried:
        try:
            df = client.get_us_daily_long(ticker, exchange=ex, days=days)
        except Exception as e:
            print(f"  {ticker}({ex}): 조회 실패 — {e}")
            df = None
        time.sleep(0.3)
        if df is not None and len(df) >= 60:
            if ex != exchange:
                print(f"  {ticker}: {exchange} 실패 → {ex} 로 조회 성공")
            break
    time.sleep(0.3)
    if df is None or len(df) < 60:
        return None
    df = df.reset_index()
    if "date" not in df.columns:
        df = df.rename(columns={df.columns[0]: "date"})
    df["date"] = df["date"].astype(str).str[:10]
    df = df.sort_values("date").reset_index(drop=True)
    try:
        df.to_json(fp, orient="records", force_ascii=False)
    except Exception:
        pass
    return df


def _crash_threshold(cfg: dict, df, price: float) -> float:
    """실제 매매에 쓰이는 임계를 PositionManager 로직 그대로 계산 (ATR 보정 포함).

    스크립트가 임계를 자체 구현하면 config 를 바꿨을 때 분석과 실동작이 어긋난다.
    """
    from zusik.core.position_manager import PositionManager
    pm = PositionManager.__new__(PositionManager)
    pos = cfg.get("position", {}) or {}
    pm.crash_instant_sell = float(pos.get("crash_instant_sell", -0.04))
    pm.crash_atr_scaling_enabled = bool(pos.get("crash_atr_scaling_enabled", True))
    pm.crash_atr_baseline = float(pos.get("crash_atr_baseline", 0.02))
    pm.crash_atr_mult = float(pos.get("crash_atr_mult", 20.0))
    pm.crash_atr_scale_cap = float(pos.get("crash_atr_scale_cap", 2.0))
    return pm._crash_instant_threshold(df, price)


def _rets(df) -> dict:
    c = df["close"].astype(float).tolist()
    d = df["date"].tolist()
    return {d[i]: (c[i] - c[i - 1]) / c[i - 1] for i in range(1, len(c)) if c[i - 1] > 0}


def analyze(cfg, ticker, df, idx_df, horizon, use_config_threshold):
    """급락일 분류 + forward 수익률. 반환: (rows, base_rate)."""
    ffg = (cfg.get("risk", {}) or {}).get("fast_fall_guard", {}) or {}
    idx_sharp = float(ffg.get("index_sharp_pct", -2.5)) / 100.0
    megacap = float(ffg.get("megacap_drop_pct", -3.5)) / 100.0
    idx_confirm = float(ffg.get("index_confirm_pct", -1.0)) / 100.0

    r = _rets(df)
    ir = _rets(idx_df)
    closes = df["close"].astype(float).tolist()
    dates = df["date"].tolist()
    pos = {d: i for i, d in enumerate(dates)}
    icl = idx_df["close"].astype(float).tolist()

    base = [(closes[i + horizon] - closes[i]) / closes[i]
            for i in range(len(closes) - horizon) if closes[i] > 0]
    base_rate = st.mean(base) if base else 0.0

    rows = []
    for d, v in r.items():
        i = pos[d]
        thr = (_crash_threshold(cfg, df.iloc[max(0, i - 30):i + 1], closes[i])
               if use_config_threshold else -0.07)
        if v > thr:
            continue
        if i + horizon >= len(closes):
            continue
        iv = ir.get(d, 0.0)
        guard = iv <= idx_sharp or (v <= megacap and iv <= idx_confirm)
        # 레짐: 지수 200일 이동평균 아래 = 지속 하락장
        ii = {dd: k for k, dd in enumerate(idx_df["date"].tolist())}.get(d)
        bear = None
        if ii is not None and ii >= 200:
            bear = icl[ii] < sum(icl[ii - 200:ii]) / 200
        rows.append({
            "ticker": ticker, "date": d, "drop": v, "index": iv, "threshold": thr,
            "kind": "시장급락" if guard else "개별악재", "bear": bear,
            "fwd": (closes[i + horizon] - closes[i]) / closes[i],
        })
    return rows, base_rate


def _fmt(rows, base_rate=None):
    if not rows:
        return "  (해당 없음)"
    f = [x["fwd"] for x in rows]
    out = (f"{len(rows):>4}건  평균 {st.mean(f) * 100:>+6.1f}%  중앙 {st.median(f) * 100:>+6.1f}%  "
           f"플러스 {sum(1 for x in f if x > 0) / len(f) * 100:>3.0f}%")
    if base_rate is not None:
        out += f"  기저대비 {(st.mean(f) - base_rate) * 100:>+6.1f}%p"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="급락 hold-through 사후검증(읽기 전용)")
    ap.add_argument("--tickers", default="", help="쉼표 구분. 비우면 config whitelist_us")
    ap.add_argument("--index", default="SPY", help="시장 프록시 (기본 SPY)")
    ap.add_argument("--days", type=int, default=1250, help="조회 일수 (기본 1250≈5년)")
    ap.add_argument("--horizon", type=int, default=20, help="forward 거래일 (기본 20)")
    ap.add_argument("--fixed-threshold", action="store_true",
                    help="config 임계(ATR 보정) 대신 고정 -7% 사용")
    ap.add_argument("--no-cache", action="store_true", help="캐시 무시하고 재조회")
    ap.add_argument("--json", default=OUT_JSON)
    args = ap.parse_args()

    cfg = _load_config()
    if args.tickers:
        universe = [(t.strip().upper(), "NASD") for t in args.tickers.split(",") if t.strip()]
    else:
        wl = ((cfg.get("screening", {}) or {}).get("whitelist_us", []) or [])
        universe = [(w["ticker"], w.get("exchange", "NASD")) for w in wl if w.get("ticker")]
    if not universe:
        print("분석 대상 없음 — --tickers 로 지정하거나 config screening.whitelist_us 를 채우세요.")
        return 1

    client = _client()
    use_cache = not args.no_cache
    idx_df = _fetch(client, args.index, "NYSE" if args.index == "SPY" else "NASD",
                    args.days, use_cache)
    if idx_df is None:
        print(f"지수 프록시({args.index}) 조회 실패 — 중단")
        return 1

    exempt = (cfg.get("position", {}) or {}).get("whitelist_crash_exempt", True)
    print(f"\n급락 hold-through 사후검증 — 지수 {args.index}, {args.horizon}거래일 forward, "
          f"{args.days}일 조회")
    print(f"현재 설정: whitelist_crash_exempt={exempt} | "
          f"임계 {'고정 -7%' if args.fixed_threshold else 'config+ATR 보정'}")
    print("=" * 96)

    all_rows, bases = [], {}
    for ticker, exch in universe:
        df = _fetch(client, ticker, exch, args.days, use_cache)
        if df is None:
            print(f"  {ticker}: 데이터 부족 — 건너뜀")
            continue
        rows, base = analyze(cfg, ticker, df, idx_df, args.horizon,
                             not args.fixed_threshold)
        bases[ticker] = base
        all_rows += rows
        print(f"\n  [{ticker}]  기저율(아무 날 {args.horizon}일 보유) {base * 100:+.1f}%")
        for kind in ("시장급락", "개별악재"):
            sub = [x for x in rows if x["kind"] == kind]
            print(f"    {kind}  {_fmt(sub, base)}")

    if not all_rows:
        print("\n급락일이 없습니다 — 임계나 기간을 조정해 보세요.")
        return 0

    print("\n" + "=" * 96)
    print("  [전체]")
    for kind in ("시장급락", "개별악재"):
        sub = [x for x in all_rows if x["kind"] == kind]
        print(f"    {kind}  {_fmt(sub)}")
    for bear, label in ((True, "지속 하락장(지수<200MA)"), (False, "상승장(지수>200MA)")):
        sub = [x for x in all_rows if x["bear"] is bear]
        print(f"    {label:<22} {_fmt(sub)}")

    # 판정 — 이 봇의 실제 선택지는 '보유 vs 현금'이다.
    # 급락 매도는 _register_reentry_block 으로 24시간 재진입이 막히므로, 판 돈은 다른
    # 종목으로 즉시 재배치되지 않고 현금으로 남는다. 따라서 1차 기준은 절대 forward
    # (>0 이면 홀드가 현금을 이겼다 = 판 게 손해). 기저율 대비는 '그 자본을 평균적인
    # 날에 넣었다면' 이라는 기회비용 관점이라 참고 지표로만 둔다.
    print("\n  판정 (1차: 절대 forward = 홀드 vs 현금 / 괄호: 기저율 대비 기회비용)")
    for kind in ("시장급락", "개별악재"):
        sub = [x for x in all_rows if x["kind"] == kind]
        if not sub:
            continue
        f = [x["fwd"] for x in sub]
        avg, med = st.mean(f), st.median(f)
        exc = st.mean([x["fwd"] - bases.get(x["ticker"], 0.0) for x in sub])
        if avg > 0 and med > 0:
            verdict = "팔면 손해 — hold-through 유지"
        elif avg <= 0 and med <= 0:
            verdict = "컷이 유리 — 면제 해제 검토"
        else:
            verdict = "혼재(평균/중앙 불일치) — 소수 대박이 평균을 끌어올림, 판단 보류"
        warn = "  ※표본부족" if len(sub) < 30 else ""
        print(f"    {kind}: 평균 {avg * 100:+.1f}% / 중앙 {med * 100:+.1f}% "
              f"(기저대비 {exc * 100:+.1f}%p) → {verdict}  [{len(sub)}건]{warn}")
    print("\n  · 표본 30건 미만 부류는 참고만 — 우연이 결론을 뒤집기 쉽습니다.")
    print("  · forward 는 단순 보유 가정 — 실제 봇은 트레일링/익절이 개입해 달라집니다.")
    print("  · 평균>0 인데 중앙<0 이면 소수 급반등이 평균을 끌어올린 것 — 중앙값을 더 믿을 것.")

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "index": args.index, "horizon": args.horizon, "days": args.days,
        "whitelist_crash_exempt": exempt,
        "base_rates": bases, "events": all_rows,
    }
    try:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n  결과 저장: {args.json}")
    except Exception as e:
        print(f"\n  결과 저장 실패: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
