#!/usr/bin/env python3
"""소액 계좌 구매 가능 종목 대량 조사 + 로컬 백테스트 ranking.

수집 대상: KR 저가 대표주/ETF 약 40종 + US 저가 테크/핀테크/인버스 약 40개
필터: 매수 가격이 `--max-krw` / `--max-usd` 이하인 종목만
평가:
  - 상위 로컬 전략 3종 (macd_rsi, ma_cross, adaptive)의 평균 수익률·승률
  - 최근 `hold_score` (상승 지속 가능성)
  - split_profit 패턴 빈도
  - 최종 종합 점수로 ranking

출력:
  data/top_candidates.json   — 전체 순위
  stdout                     — 상위 15종 요약
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime

from zusik.paths import ROOT
os.chdir(str(ROOT))

from dotenv import load_dotenv
load_dotenv(".env")

from zusik.clients.kis_client import KISClient
from zusik.analysis.backtest import simulate, _build_strategy

KR_CANDIDATES: list[str] = [
    # 저가 대형·중형주
    "001740", "003850", "005380", "008560", "010140", "011070",
    "011170", "011200", "015760", "020150", "024110", "034020",
    "034220", "035250", "042670", "086280", "088980", "089860",
    "145990", "181710", "204320", "271560", "298050", "316140",
    "336260", "000400", "000660", "017670", "029780", "096530",
    # 코스닥 저가 유망
    "053800", "078340", "112040", "194480", "195870", "263920",
    "090360", "217270", "192080", "068760", "086520",
    # 금융·산업 저가
    "006360", "001120", "002350", "028050", "003000", "006650",
    "010780", "000720", "007310", "009150",
    # ETF (지수·인버스·섹터·레버리지)
    "069500", "114800", "122630", "252670", "409820", "278240",
    "091160", "139660", "102110", "229200", "233740", "117680",
    "251340", "102780",
]

US_CANDIDATES: list[tuple[str, str]] = [
    # 저가 테크
    ("SOFI", "NASD"), ("NIO", "NYSE"), ("RIVN", "NASD"),
    ("LCID", "NASD"), ("PLTR", "NYSE"), ("HOOD", "NASD"),
    ("NU", "NYSE"), ("GRAB", "NASD"), ("LYFT", "NASD"),
    ("SNAP", "NYSE"), ("WBD", "NASD"), ("BBAI", "NYSE"),
    ("OPEN", "NASD"), ("SPCE", "NYSE"), ("JOBY", "NYSE"),
    ("CHPT", "NYSE"), ("PLUG", "NASD"), ("FCEL", "NASD"),
    ("BLNK", "NASD"), ("APLD", "NASD"),
    # 저가 크립토 관련
    ("MARA", "NASD"), ("RIOT", "NASD"), ("HUT", "NASD"),
    ("CLSK", "NASD"),
    # 전통 저가
    ("F", "NYSE"), ("BAC", "NYSE"), ("T", "NYSE"),
    ("PFE", "NYSE"), ("CCL", "NYSE"), ("NOK", "NYSE"),
    ("BB", "NYSE"), ("SIRI", "NASD"), ("GOLD", "NYSE"),
    ("LUMN", "NYSE"), ("RLX", "NYSE"),
    # 인버스 / 지수
    ("SH", "NYSE"), ("SQQQ", "NASD"), ("SPXU", "NYSE"),
    ("QQQ", "NASD"), ("SPY", "AMEX"),
    # 대중 주목주
    ("AMC", "NYSE"), ("GME", "NYSE"), ("TLRY", "NASD"),
]


def score_candidate(result_by_strategy: dict, hold_score_now: float, max_price: float,
                    current_price: float, volatility: float, avg_volume: float,
                    total_trades: int) -> float:
    """백테스트 + 상승 점수 + 가격 여유 + 리스크 조정 종합 점수.

    개선 (v2):
      - volatility 페널티: 일별 수익률 std > 5%면 -5, > 8%면 -10
      - avg_volume < 50,000 (거래량 부족) → -10 (유동성 위험)
      - 총 매도 건수 < 2 (표본 부족) → -5
      - split_profit 비중 강화 (× 0.3)
    """
    if not result_by_strategy:
        return -999.0
    avg_return = sum(r["return_rate"] for r in result_by_strategy.values()) / len(result_by_strategy)
    avg_wr = sum(r["win_rate"] for r in result_by_strategy.values()) / len(result_by_strategy)
    split_ratio = 0.0
    total_sells = 0
    for r in result_by_strategy.values():
        for pat, ps in r.get("pattern_stats", {}).items():
            if pat == "split_profit":
                split_ratio += ps["count"]
            total_sells += ps["count"]
    split_pct = (split_ratio / total_sells * 100) if total_sells else 0

    price_fit = 1.0 if current_price <= max_price else max(0.1, max_price / current_price)

    score = avg_return + avg_wr * 0.05 + hold_score_now * 10 + split_pct * 0.3 + price_fit * 5

    # 변동성 페널티 — 너무 출렁이는 종목 감점
    if volatility > 0.08:
        score -= 10
    elif volatility > 0.05:
        score -= 5

    # 유동성 필터 — 평균 거래량이 극히 낮으면 체결 리스크
    if avg_volume > 0 and avg_volume < 50_000:
        score -= 10

    # 표본 부족 페널티 — 백테스트 거래가 2건 미만이면 평가 의미 희박
    if total_trades < 2:
        score -= 5

    # v3: 극단치 필터
    # 승률 < 30%: 한두 번의 대박으로 평균 수익률만 높은 종목 감점 (AMC 같은 케이스)
    if avg_wr < 30:
        score -= 8
    # 누적 손실 종목은 추가 감점
    if avg_return < 0:
        score -= 3
    # 비정상적으로 높은 수익률은 오버피팅 의심 — 소폭 감점으로 과신 방지
    if avg_return > 50:
        score -= 2

    return round(score, 2)


def _volatility(df) -> float:
    try:
        closes = df["close"]
        returns = closes.pct_change().dropna().tail(20)
        return float(returns.std()) if len(returns) else 0.0
    except Exception:
        return 0.0


def _avg_volume(df) -> float:
    try:
        if "volume" not in df.columns:
            return 0.0
        return float(df["volume"].tail(20).mean())
    except Exception:
        return 0.0


# 알려진 종목명 매핑 (config 업데이트 시 name 필드 채우기용)
KR_NAMES = {
    "096530": "씨젠", "034220": "LG디스플레이", "102780": "TIGER 나스닥100",
    "409820": "KODEX 미국나스닥100인버스(H)", "117680": "KODEX 철강",
    "003850": "보령", "010140": "삼성중공업", "229200": "KODEX 코스닥150",
    "000400": "롯데손해보험", "001740": "SK네트웍스", "112040": "위메이드",
    "010780": "아이에스동서", "217270": "넵튠", "002350": "넥센타이어",
    "024110": "기업은행", "034020": "두산에너빌리티", "042670": "두산인프라코어",
    "089860": "이녹스첨단소재", "035250": "강원랜드", "020150": "일진머티리얼즈",
    "015760": "한국전력", "011200": "HMM", "011170": "롯데케미칼",
    "008560": "메리츠증권", "011070": "LG이노텍", "006360": "GS건설",
    "001120": "LG상사", "028050": "삼성E&A", "003000": "부광약품",
    "006650": "대한유화", "000720": "현대건설", "007310": "오뚜기",
    "009150": "삼성전기", "053800": "안랩", "078340": "컴투스",
    "194480": "데브시스터즈", "195870": "해성디에스", "263920": "블루콤",
    "090360": "로보스타", "192080": "더블유게임즈",
    "068760": "셀트리온제약", "086520": "에코프로", "069500": "KODEX 200",
    "114800": "KODEX 인버스", "122630": "KODEX 레버리지",
    "252670": "KODEX 200선물인버스2X", "278240": "TIGER 코스닥150 인버스",
    "091160": "KODEX 반도체", "139660": "KODEX 에너지화학", "102110": "TIGER 200",
    "233740": "KODEX 코스닥150 레버리지", "251340": "KODEX 코스닥150선물인버스",
}

US_NAMES = {
    "NOK": "Nokia", "GME": "GameStop", "AMC": "AMC Entertainment",
    "BB": "BlackBerry", "MARA": "MARA Holdings", "CLSK": "CleanSpark",
    "SIRI": "Sirius XM", "NIO": "NIO Inc.", "SNAP": "Snap",
    "BLNK": "Blink Charging", "RIVN": "Rivian", "RIOT": "Riot Platforms",
    "OPEN": "Opendoor", "T": "AT&T", "CCL": "Carnival", "GRAB": "Grab",
    "SOFI": "SoFi Technologies", "FCEL": "FuelCell Energy",
    "PLUG": "Plug Power", "PFE": "Pfizer", "F": "Ford", "BAC": "Bank of America",
    "CHPT": "ChargePoint", "JOBY": "Joby Aviation", "SPCE": "Virgin Galactic",
    "HOOD": "Robinhood", "LYFT": "Lyft", "NU": "Nu Holdings",
    "LCID": "Lucid", "PLTR": "Palantir", "LUMN": "Lumen", "RLX": "RLX",
    "TLRY": "Tilray", "WBD": "Warner Bros. Discovery", "BBAI": "BigBear.ai",
    "APLD": "Applied Digital", "HUT": "Hut 8", "GOLD": "Barrick Gold",
    "SH": "ProShares Short S&P500", "SQQQ": "ProShares UltraPro Short QQQ",
    "SPXU": "ProShares UltraPro Short S&P500", "QQQ": "Invesco QQQ",
    "SPY": "SPDR S&P 500",
}


def apply_to_config(ranked: list, kr_limit: int, us_limit: int) -> None:
    """상위 후보를 config.yaml의 stocks / us_stocks에 병합 저장.

    기존 항목은 보존, 새 항목은 추가. config.yaml 백업 생성.
    """
    import yaml
    import shutil
    path = "config.yaml"
    shutil.copy(path, path + ".bak_apply_20260419")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    kr_existing = {s.get("code") for s in cfg.get("stocks", [])}
    us_existing = {s.get("ticker") for s in cfg.get("us_stocks", [])}

    added_kr = []
    added_us = []
    kr_added_count = 0
    us_added_count = 0

    for c in ranked:
        if c["market"] == "KR" and kr_added_count < kr_limit:
            if c["symbol"] not in kr_existing:
                name = KR_NAMES.get(c["symbol"], c["symbol"])
                cfg.setdefault("stocks", []).append({"code": c["symbol"], "name": name})
                added_kr.append(f"{name}({c['symbol']})")
                kr_existing.add(c["symbol"])
                kr_added_count += 1
        elif c["market"] == "US" and us_added_count < us_limit:
            if c["symbol"] not in us_existing:
                name = US_NAMES.get(c["symbol"], c["symbol"])
                cfg.setdefault("us_stocks", []).append({
                    "ticker": c["symbol"], "name": name,
                    "exchange": c.get("exchange", "NASD"),
                })
                added_us.append(f"{name}({c['symbol']})")
                us_existing.add(c["symbol"])
                us_added_count += 1

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False, indent=2)

    print("\nconfig.yaml 업데이트:", flush=True)
    print(f"  백업: {path}.bak_apply_20260419", flush=True)
    print(f"  KR 추가 ({len(added_kr)}): {', '.join(added_kr) or '(없음)'}", flush=True)
    print(f"  US 추가 ({len(added_us)}): {', '.join(added_us) or '(없음)'}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-krw", type=int, default=12000, help="KR 최대 매수가 (원)")
    parser.add_argument("--max-usd", type=float, default=15.0, help="US 최대 매수가 ($)")
    parser.add_argument("--top", type=int, default=15, help="상위 N종 출력")
    parser.add_argument("--strategies", nargs="+",
                        default=["macd_rsi", "ma_cross", "adaptive"],
                        help="평가할 로컬 전략")
    parser.add_argument("--apply", action="store_true",
                        help="상위 종목을 config.yaml의 stocks/us_stocks에 자동 반영")
    parser.add_argument("--apply-top-kr", type=int, default=5,
                        help="--apply 시 KR 추가할 상위 종목 수")
    parser.add_argument("--apply-top-us", type=int, default=5,
                        help="--apply 시 US 추가할 상위 종목 수")
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print(f"▶ 소액 계좌 종목 스크리너 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"   KR 상한: {args.max_krw:,}원 / US 상한: ${args.max_usd}", flush=True)
    print("=" * 60, flush=True)

    client = KISClient(
        os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", ""),
        os.getenv("KIS_ACCOUNT_NO", ""), "01", False,
    )

    from zusik.analysis.indicators import hold_score

    all_ranked: list[dict] = []
    cache_dir = "data/ohlcv_cache"
    os.makedirs(cache_dir, exist_ok=True)

    # ── KR ──
    print(f"\n[1/2] KR 후보 {len(KR_CANDIDATES)}종 조사...", flush=True)
    for code in KR_CANDIDATES:
        try:
            df = client.get_ohlcv(code, period="D")
            if df is None or len(df) < 30:
                print(f"  {code}: 데이터 부족 ({len(df) if df is not None else 0}봉)", flush=True)
                time.sleep(0.4)
                continue
            curr_price = float(df["close"].iloc[-1])
            if curr_price > args.max_krw:
                print(f"  {code}: {curr_price:,.0f}원 > {args.max_krw:,}원 스킵", flush=True)
                time.sleep(0.4)
                continue
            # 캐시 저장
            df.to_json(f"{cache_dir}/KR_{code}.json", orient="records", date_format="iso")

            # 백테스트
            strat_results = {}
            for sname in args.strategies:
                try:
                    strategy = _build_strategy(sname)
                    r = simulate(df, strategy, initial_capital=1_000_000)
                    strat_results[sname] = r
                except Exception:
                    pass
            if not strat_results:
                continue

            hs = hold_score(df).get("score", 0.5)
            vol = _volatility(df)
            avg_vol = _avg_volume(df)
            total_trades = sum(r["total_trades"] for r in strat_results.values())
            score = score_candidate(strat_results, hs, args.max_krw, curr_price,
                                    vol, avg_vol, total_trades)

            all_ranked.append({
                "market": "KR", "symbol": code, "exchange": "",
                "current_price": curr_price, "bars": len(df),
                "hold_score": round(hs, 3),
                "strategies": {s: {"return": r["return_rate"], "win_rate": r["win_rate"],
                                   "trades": r["total_trades"]} for s, r in strat_results.items()},
                "score": score,
            })
            print(f"  {code}: {curr_price:,.0f}원 · bars {len(df)} · "
                  f"hold {hs:.2f} · score {score:+.2f}", flush=True)
        except Exception as e:
            print(f"  {code}: 실패 ({str(e)[:60]})", flush=True)
        time.sleep(0.4)

    # ── US ──
    print(f"\n[2/2] US 후보 {len(US_CANDIDATES)}종 조사...", flush=True)
    for ticker, exchange in US_CANDIDATES:
        try:
            df = client.get_us_ohlcv(ticker, exchange=exchange, period="D")
            if df is None or len(df) < 30:
                print(f"  {ticker}: 데이터 부족", flush=True)
                time.sleep(0.4)
                continue
            curr_price = float(df["close"].iloc[-1])
            if curr_price > args.max_usd:
                print(f"  {ticker}: ${curr_price:.2f} > ${args.max_usd} 스킵", flush=True)
                time.sleep(0.4)
                continue
            df.to_json(f"{cache_dir}/US_{ticker}.json", orient="records", date_format="iso")

            strat_results = {}
            for sname in args.strategies:
                try:
                    strategy = _build_strategy(sname)
                    r = simulate(df, strategy, initial_capital=1_000_000)
                    strat_results[sname] = r
                except Exception:
                    pass
            if not strat_results:
                continue

            hs = hold_score(df).get("score", 0.5)
            vol = _volatility(df)
            avg_vol = _avg_volume(df)
            total_trades = sum(r["total_trades"] for r in strat_results.values())
            score = score_candidate(strat_results, hs, args.max_usd, curr_price,
                                    vol, avg_vol, total_trades)

            all_ranked.append({
                "market": "US", "symbol": ticker, "exchange": exchange,
                "current_price": curr_price, "bars": len(df),
                "hold_score": round(hs, 3),
                "strategies": {s: {"return": r["return_rate"], "win_rate": r["win_rate"],
                                   "trades": r["total_trades"]} for s, r in strat_results.items()},
                "score": score,
            })
            print(f"  {ticker}/{exchange}: ${curr_price:.2f} · bars {len(df)} · "
                  f"hold {hs:.2f} · score {score:+.2f}", flush=True)
        except Exception as e:
            print(f"  {ticker}: 실패 ({str(e)[:60]})", flush=True)
        time.sleep(0.4)

    all_ranked.sort(key=lambda x: -x["score"])

    # 저장
    os.makedirs("data", exist_ok=True)
    with open("data/top_candidates.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now().isoformat(),
                   "max_krw": args.max_krw, "max_usd": args.max_usd,
                   "strategies": args.strategies, "ranked": all_ranked}, f,
                  ensure_ascii=False, indent=2)
    print(f"\n저장: data/top_candidates.json (총 {len(all_ranked)}종)", flush=True)

    # 상위 N 요약
    print(f"\n{'=' * 60}", flush=True)
    print(f"상위 {args.top}개 유망 종목", flush=True)
    print("=" * 60, flush=True)
    print(f"{'순위':<4s}{'장':<4s}{'심볼':<10s}{'현재가':>12s}{'hold':>7s}{'평균수익':>10s}{'평균승률':>9s}{'점수':>8s}", flush=True)
    print("─" * 62, flush=True)
    for i, c in enumerate(all_ranked[:args.top], 1):
        avg_ret = sum(s["return"] for s in c["strategies"].values()) / len(c["strategies"])
        avg_wr = sum(s["win_rate"] for s in c["strategies"].values()) / len(c["strategies"])
        price_fmt = (f"{c['current_price']:,.0f}원" if c["market"] == "KR"
                     else f"${c['current_price']:.2f}")
        print(f"{i:<4d}{c['market']:<4s}{c['symbol']:<10s}{price_fmt:>12s}"
              f"{c['hold_score']:>7.2f}{avg_ret:>+9.2f}%{avg_wr:>8.1f}%{c['score']:>+8.2f}",
              flush=True)

    if args.apply:
        apply_to_config(all_ranked, kr_limit=args.apply_top_kr, us_limit=args.apply_top_us)

    print(f"\n완료: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)


if __name__ == "__main__":
    main()
