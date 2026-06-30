#!/usr/bin/env python3
"""과거 OHLCV 데이터로 전략을 시뮬레이션해 가상 PnL과 sell_pattern 분포를 측정.

사용 예:
    python3 backtest.py --code 005930 --days 120 --strategy adaptive
    python3 backtest.py --ticker NIO --exchange NASD --days 60 --strategy adaptive
    python3 backtest.py --code 005930 --strategy adaptive --capital 1000000

`ExecStartPre` 자동 테스트 대상은 아님 — 운영자가 필요 시 수동 실행.
Claude 전략은 API 비용 때문에 대상에서 제외; adaptive/ma_cross/rsi/bollinger/macd_rsi 등 로컬 전략만.
"""
from __future__ import annotations

import argparse
import logging
import os
from collections import defaultdict

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


LOCAL_STRATEGIES = {
    "adaptive": ("zusik.strategies.adaptive", "AdaptiveStrategy"),
    "ma_cross": ("zusik.strategies.ma_cross", "MACrossStrategy"),
    "rsi": ("zusik.strategies.rsi", "RSIStrategy"),
    "bollinger": ("zusik.strategies.bollinger", "BollingerBandStrategy"),
    "macd_rsi": ("zusik.strategies.macd_rsi", "MACDRSIStrategy"),
    "momentum_breakout": ("zusik.strategies.momentum_breakout", "MomentumBreakoutStrategy"),
    "dual_momentum": ("zusik.strategies.dual_momentum", "DualMomentumStrategy"),
    "volatility_breakout": ("zusik.strategies.volatility_breakout", "VolatilityBreakoutStrategy"),
}


def _build_strategy(name: str):
    if name not in LOCAL_STRATEGIES:
        raise SystemExit(f"지원하지 않는 전략: {name}. 사용 가능: {list(LOCAL_STRATEGIES.keys())}")
    mod_path, cls_name = LOCAL_STRATEGIES[name]
    mod = __import__(mod_path, fromlist=[cls_name])
    return getattr(mod, cls_name)()


def _load_ohlcv(code: str | None, ticker: str | None, exchange: str, days: int):
    """KIS API로 OHLCV 조회. 환경에 따라 .env 로드 필요."""
    from dotenv import load_dotenv
    load_dotenv()
    from zusik.clients.kis_client import KISClient
    client = KISClient(
        os.getenv("KIS_APP_KEY", ""),
        os.getenv("KIS_APP_SECRET", ""),
        os.getenv("KIS_ACCOUNT_NO", ""),
        os.getenv("KIS_ACCOUNT_PROD", "01"),
        os.getenv("KIS_VIRTUAL", "false").lower() == "true",
    )
    if code:
        df = client.get_ohlcv(code, period="D")
    elif ticker:
        df = client.get_us_ohlcv(ticker, exchange=exchange, period="D")
    else:
        raise SystemExit("--code 또는 --ticker 필요")
    if df is None or len(df) < 30:
        raise SystemExit(f"OHLCV 데이터 부족: {len(df) if df is not None else 0}봉")
    return df.tail(days + 20).reset_index(drop=True)  # 워밍업 20봉 추가


def simulate(df, strategy, initial_capital: int = 1_000_000,
             warmup: int = 20) -> dict:
    """봉 단위 가상 매매. 각 매도는 sell_pattern과 함께 기록."""
    from zusik.storage.portfolio_tracker import PortfolioTracker
    cash = initial_capital
    qty = 0
    avg_price = 0.0
    peak_profit = 0.0
    high_since_buy = 0.0
    trades = []

    for i in range(warmup, len(df)):
        window = df.iloc[:i + 1]
        try:
            signal = strategy.analyze(window)
        except Exception:
            signal = "hold"
        price = float(window["close"].iloc[-1])

        if qty > 0:
            profit_rate = (price - avg_price) / avg_price if avg_price > 0 else 0
            peak_profit = max(peak_profit, profit_rate)
            high_since_buy = max(high_since_buy, price)
            from_high = (price - high_since_buy) / high_since_buy if high_since_buy else 0

            reason = None
            if profit_rate <= -0.15:
                reason = f"강제 손절 -15% 도달 ({profit_rate:+.1%})"
            elif from_high <= -0.08:
                reason = f"트레일링 스톱 고점 대비 {from_high:+.1%}"
            elif peak_profit >= 0.05 and profit_rate <= 0.015:
                reason = f"본전 보호 — 최고 +{peak_profit*100:.1f}%"
            elif signal == "sell":
                # 분할 익절 이름이 강조되도록 reason 조합
                if profit_rate >= 0.10:
                    reason = f"[2차 익절 +10%] 전략 sell 신호 ({profit_rate:+.1%})"
                elif profit_rate >= 0.05:
                    reason = f"[1차 익절 +5%] 전략 sell 신호 ({profit_rate:+.1%})"
                else:
                    reason = f"전략 sell 신호 ({profit_rate:+.1%})"

            if reason:
                pnl = int((price - avg_price) * qty)
                pattern = PortfolioTracker._classify_sell_pattern(reason)
                cash += price * qty
                trades.append({
                    "action": "sell", "price": price, "qty": qty,
                    "pnl": pnl, "reason": reason, "sell_pattern": pattern,
                    "date": str(window["date"].iloc[-1]) if "date" in window.columns else str(i),
                })
                qty = 0
                avg_price = 0.0
                peak_profit = 0.0
                high_since_buy = 0.0
                continue

        if signal in ("buy", "long_term_buy") and qty == 0 and cash >= price:
            buy_qty = int(cash * 0.95 / price)
            if buy_qty > 0:
                cash -= buy_qty * price
                qty = buy_qty
                avg_price = price
                peak_profit = 0.0
                high_since_buy = price
                trades.append({
                    "action": "buy", "price": price, "qty": buy_qty,
                    "date": str(window["date"].iloc[-1]) if "date" in window.columns else str(i),
                })

    final_value = cash + qty * float(df["close"].iloc[-1])
    wins = sum(1 for t in trades if t["action"] == "sell" and t.get("pnl", 0) > 0)
    losses = sum(1 for t in trades if t["action"] == "sell" and t.get("pnl", 0) < 0)

    pattern_stats = defaultdict(lambda: {"count": 0, "wins": 0, "pnl_sum": 0})
    for t in trades:
        if t["action"] != "sell":
            continue
        p = t.get("sell_pattern", "other")
        pattern_stats[p]["count"] += 1
        if t.get("pnl", 0) > 0:
            pattern_stats[p]["wins"] += 1
        pattern_stats[p]["pnl_sum"] += t.get("pnl", 0)

    return {
        "initial_capital": initial_capital,
        "final_value": int(final_value),
        "return_rate": (final_value - initial_capital) / initial_capital * 100,
        "total_trades": len(trades),
        "sells": wins + losses,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / (wins + losses) * 100) if (wins + losses) else 0.0,
        "pattern_stats": {k: dict(v) for k, v in pattern_stats.items()},
        "trades": trades,
    }


def print_report(result: dict, label: str):
    print(f"\n{'=' * 60}")
    print(f"{label}")
    print("=" * 60)
    print(f"초기 자본: {result['initial_capital']:>12,}원")
    print(f"최종 평가: {result['final_value']:>12,}원")
    print(f"수익률:    {result['return_rate']:>+12.2f}%")
    print(f"총 매매:   {result['total_trades']:>12}건 (매도 {result['sells']}건)")
    print(f"승률:      {result['win_rate']:>12.1f}% ({result['wins']}승 {result['losses']}패)")
    print()
    if result["pattern_stats"]:
        print("매도 패턴 분포:")
        for pat, s in sorted(result["pattern_stats"].items(),
                             key=lambda x: -x[1]["pnl_sum"]):
            rate = (s["wins"] / s["count"] * 100) if s["count"] else 0
            avg = s["pnl_sum"] // s["count"] if s["count"] else 0
            print(f"  {pat:<22s} {s['count']:>3d}건 · 승률 {rate:>3.0f}% · "
                  f"총 {s['pnl_sum']:>+10,d}원 · 건당 {avg:>+8,d}")


def main():
    parser = argparse.ArgumentParser(description="전략 백테스트")
    parser.add_argument("--code", help="KR 종목코드 (6자리)")
    parser.add_argument("--ticker", help="US 티커 (예: NIO, AAPL)")
    parser.add_argument("--exchange", default="NASD", help="US 거래소 (NASD/NYSE/AMEX)")
    parser.add_argument("--days", type=int, default=90, help="백테스트 기간 (봉)")
    parser.add_argument("--strategy", default="adaptive",
                        choices=list(LOCAL_STRATEGIES.keys()))
    parser.add_argument("--capital", type=int, default=1_000_000, help="초기 자본 (원)")
    args = parser.parse_args()

    if not args.code and not args.ticker:
        parser.error("--code 또는 --ticker 중 하나는 필수")

    print(f"▶ 데이터 로드: {args.code or args.ticker} / {args.days}봉 / 전략 {args.strategy}")
    df = _load_ohlcv(args.code, args.ticker, args.exchange, args.days)
    strategy = _build_strategy(args.strategy)
    result = simulate(df, strategy, initial_capital=args.capital)
    label = f"{args.code or args.ticker} / {args.strategy} / {args.days}봉 / 초기 {args.capital:,}원"
    print_report(result, label)


if __name__ == "__main__":
    main()
