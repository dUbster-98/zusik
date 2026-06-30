from __future__ import annotations
"""종목선택 평가 — 고른 종목이 지수 대비 얼마나 더 벌었나(alpha) + 그날 놓친 최고 상승종목.

각 매수를 보유 window(기본 10거래일) 동안 추적해:
  - pick 수익 = 매수일 종가 → window 뒤 종가 (또는 가용 마지막)
  - 지수 수익 = 같은 구간 지수 프록시(KR 069500 / US QQQ)
  - alpha = pick - 지수   (>0: 지수보다 더 벌었다 = 선택이 유효)
를 계산하고, 유니버스가 주어지면 매수일별로 '그날 사지 않은 종목 중 최고 상승'을 찾아
봇의 선택이 얼마나 뒤처졌는지(놓친 최고종목과의 gap) 본다.

순수 함수: trades + 미리 받아둔 series dict 만 받는다(네트워크 비의존, 테스트 가능).
series 형식은 sell_timing 과 동일: {"dates":[YYYY-MM-DD], "closes":[...], ...}.
"""


def _normdate(x) -> str:
    s = str(x)
    if len(s) >= 10 and s[4:5] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _fwd_return(series, on_date: str, window: int):
    """on_date 이하 마지막 거래일 종가 → window 거래일 뒤 종가 수익률. 불가하면 None."""
    if not series or not series.get("dates"):
        return None
    dates, closes = series["dates"], series["closes"]
    idx = None
    for i, d in enumerate(dates):
        if d <= on_date:
            idx = i
        else:
            break
    if idx is None or idx >= len(dates) - 1:
        return None
    base = closes[idx]
    if base <= 0:
        return None
    end = min(idx + window, len(dates) - 1)
    return closes[end] / base - 1.0


def analyze_selection_alpha(trades, get_series, get_index_series, *,
                            window: int = 10, universe=None):
    """종목선택 alpha + (옵션)그날 놓친 최고종목 집계.

    get_series(market, symbol, exchange) -> series | None  (심볼별 캐시 권장)
    get_index_series(market) -> series | None              (KR/US 지수 프록시)
    universe: [(market, symbol, exchange, name), ...] — 주면 매수일별 놓친 최고종목 계산.
    반환: {"alpha": {...}, "by_market": {...}, "missed_best": {...}|None, "details": [...]}
    """
    idx_cache: dict = {}

    def _index(market):
        if market not in idx_cache:
            try:
                idx_cache[market] = get_index_series(market)
            except Exception:
                idx_cache[market] = None
        return idx_cache[market]

    details = []
    buy_days: dict = {}  # date -> set(symbol) 그날 산 종목
    for t in trades:
        if t.get("type") != "buy":
            continue
        market = (t.get("market") or ("US" if t.get("ticker") else "KR")).upper()
        symbol = (t.get("ticker") if market == "US" else t.get("code")) or t.get("code")
        if not symbol:
            continue
        bd = _normdate(t.get("date") or t.get("timestamp"))
        if "-" not in bd:
            continue
        buy_days.setdefault(bd, set()).add(symbol)
        try:
            s = get_series(market, symbol, t.get("exchange", "NASD"))
        except Exception:
            s = None
        pick = _fwd_return(s, bd, window)
        if pick is None:
            continue
        ir = _fwd_return(_index(market), bd, window)
        details.append({
            "code": symbol, "name": t.get("name", symbol), "market": market, "date": bd,
            "pick_return": pick, "index_return": ir,
            "alpha": (pick - ir) if ir is not None else None,
        })

    def _mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    alphas = [d["alpha"] for d in details if d["alpha"] is not None]
    picks = [d["pick_return"] for d in details]
    alpha_out = {
        "count": len(details),
        "avg_pick_return": round(_mean(picks) * 100, 2),
        "avg_alpha": round(_mean(alphas) * 100, 2),
        "beat_index_rate": round(100 * sum(1 for a in alphas if a > 0) / len(alphas), 1) if alphas else 0.0,
        "window": window,
    }
    by_market = {}
    for mk in ("KR", "US"):
        ms = [d for d in details if d["market"] == mk]
        mal = [d["alpha"] for d in ms if d["alpha"] is not None]
        if ms:
            by_market[mk] = {
                "count": len(ms),
                "avg_pick_return": round(_mean([d["pick_return"] for d in ms]) * 100, 2),
                "avg_alpha": round(_mean(mal) * 100, 2),
                "beat_index_rate": round(100 * sum(1 for a in mal if a > 0) / len(mal), 1) if mal else 0.0,
            }

    missed_best = None
    if universe:
        uni_series: dict = {}
        for (mk, sym, exch, _nm) in universe:
            try:
                uni_series[(mk, sym)] = get_series(mk, sym, exch)
            except Exception:
                uni_series[(mk, sym)] = None
        uni_name = {(mk, sym): nm for (mk, sym, _e, nm) in universe}
        gaps, bot_rets, best_rets, day_rows = [], [], [], []
        for bd, bought in sorted(buy_days.items()):
            # 그날 유니버스 각 종목의 window 수익
            day = []
            for (mk, sym), s in uni_series.items():
                r = _fwd_return(s, bd, window)
                if r is not None:
                    day.append((mk, sym, r))
            if not day:
                continue
            best = max(day, key=lambda x: x[2])
            not_bought = [x for x in day if x[1] not in bought]
            best_missed = max(not_bought, key=lambda x: x[2]) if not_bought else None
            bot = [x for x in day if x[1] in bought]
            bot_best = max(bot, key=lambda x: x[2]) if bot else None
            if best_missed and bot_best:
                gaps.append(best_missed[2] - bot_best[2])
                bot_rets.append(bot_best[2])
                best_rets.append(best_missed[2])
                day_rows.append({
                    "date": bd,
                    "bot_best": {"code": bot_best[1], "name": uni_name.get((bot_best[0], bot_best[1]), bot_best[1]),
                                 "ret": round(bot_best[2] * 100, 2)},
                    "missed_best": {"code": best_missed[1], "name": uni_name.get((best_missed[0], best_missed[1]), best_missed[1]),
                                    "ret": round(best_missed[2] * 100, 2)},
                })
        if gaps:
            missed_best = {
                "days": len(gaps),
                "avg_bot_best_return": round(_mean(bot_rets) * 100, 2),
                "avg_missed_best_return": round(_mean(best_rets) * 100, 2),
                "avg_gap": round(_mean(gaps) * 100, 2),
                "days_detail": day_rows[-20:],  # 최근 20일
            }

    return {"alpha": alpha_out, "by_market": by_market, "missed_best": missed_best,
            "details": details}
