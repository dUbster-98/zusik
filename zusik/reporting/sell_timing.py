from __future__ import annotations
"""매도 사후분석 — '팔고 난 뒤' 종목이 얼마나 더 올랐나(놓친 상승) / 얼마나 빠졌나(막은 하락).

각 매도를 추적해 매도 시점 대비:
  - 당일 놓친 상승(same_day) = 매도일 high / ref - 1  (팔고 나서 그날 안에 최대 몇 % 더 갔나)
  - 놓친 상승(up)   = forward high / ref - 1   (>0: 팔고 더 올랐다 = 조기매도 손해)
  - 막은 하락(down) = forward low  / ref - 1   (<0: 팔아서 하락을 피했다 = 보호 성공)
  - 종가 기준(net)  = forward close / ref - 1   (홀드했으면 어땠나)
forward 는 1/3/5/10 거래일. 당일(same_day)은 매도 직후 같은 날 장중 되돌림 여부를 본다.
를 계산하고 sell_pattern 별로 집계한다. 패턴별 평균이
  · up 크고 down 얕음 → '조기매도(상승 놓침)' → 더 늦게 팔도록(defer) 보정 후보
  · down 깊고 up 얕음 → '보호 성공(하락 회피)' → 유지/강화
임을 알려줘, 어디서 매도 타이밍을 고쳐야 수익이 느는지 데이터로 가리킨다.

ref(매도 기준가)는 기록된 price 의 단위(원/달러×배율)가 매수·매도 레코드마다 달라
신뢰하기 어렵다 → 매도일 OHLCV 의 [low,high] 범위에 맞는 배율을 자동 복원해 unit-safe.
복원 실패 시 매도일 종가로 폴백.

순수 함수: trades 리스트 + fetch_daily 콜백만 받는다(라이브 봇/네트워크 비의존, 테스트 가능).
"""

# 단위 자동복원 후보 배율 (원=1, 달러×1000/×100 등 레코드 혼재 대응)
_SCALE_CANDIDATES = (1.0, 1e-3, 1e-2, 1e-1, 1e1, 1e2, 1e3)


def _normdate(x) -> str:
    s = str(x)
    if len(s) >= 10 and s[4:5] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _native_ref(rec_price: float, low: float, high: float, close: float) -> float:
    """기록 매도가를 매도일 [low,high] 범위에 맞는 배율로 복원. 실패 시 종가."""
    if low <= 0 or high <= 0:
        return float(close)
    lo, hi = low * 0.95, high * 1.05
    best = None
    for sc in _SCALE_CANDIDATES:
        p = rec_price * sc
        if lo <= p <= hi:
            # 범위 안 후보 중 [low,high] 중점에 가장 가까운 것
            mid = (low + high) / 2.0
            d = abs(p - mid)
            if best is None or d < best[1]:
                best = (p, d)
    return best[0] if best else float(close)


def _verdict(up: float, down: float, pct: float) -> str:
    """패턴 평균 up(놓친 상승)/down(막은 하락, 음수)로 매도 타이밍 판정."""
    drop = -down  # 막은 하락폭(양수)
    if up >= pct and up >= drop:
        return "조기매도(상승 놓침)"
    if drop >= pct and drop > up:
        return "보호 성공(하락 회피)"
    return "적정/혼조"


def analyze_sell_timing(trades, fetch_daily, *, horizons=(1, 3, 5, 10),
                        primary: int = 5, classify_pct: float = 0.02):
    """매도 사후분석 집계.

    trades: data/trades.json 리스트.
    fetch_daily(market, symbol, exchange) -> dict{dates:[YYYY-MM-DD], lows, highs, closes}
        또는 None. 심볼별 1회만 불리도록 호출측이 캐시할 것(여기선 자체 캐시도 함).
    반환: {"by_pattern": {pat: {...}}, "overall": {...}, "details": [...], "pending": n,
            "horizons": [...], "primary": p}
    """
    horizons = sorted(set(int(h) for h in horizons))
    cache: dict = {}

    def _series(market, symbol, exchange):
        key = (market, symbol)
        if key in cache:
            return cache[key]
        try:
            s = fetch_daily(market, symbol, exchange)
        except Exception:
            s = None
        cache[key] = s
        return s

    details = []
    pending = 0
    for t in trades:
        if t.get("type") != "sell":
            continue
        market = (t.get("market") or ("US" if t.get("ticker") else "KR")).upper()
        symbol = t.get("ticker") if market == "US" else t.get("code")
        symbol = symbol or t.get("code")
        if not symbol:
            continue
        sell_date = _normdate(t.get("date") or t.get("timestamp"))
        if "-" not in sell_date:
            continue
        s = _series(market, symbol, t.get("exchange", "NASD"))
        if not s or not s.get("dates"):
            continue
        dates, lows, highs, closes = s["dates"], s["lows"], s["highs"], s["closes"]
        # 매도일 인덱스 = sell_date 이하의 마지막 거래일
        idx = None
        for i, d in enumerate(dates):
            if d <= sell_date:
                idx = i
            else:
                break
        if idx is None or idx >= len(dates) - 1:
            pending += 1  # forward 데이터 없음(너무 최근) → 보류
            continue
        ref = _native_ref(float(t.get("price") or 0), lows[idx], highs[idx], closes[idx])
        if ref <= 0:
            continue
        row = {
            "code": symbol, "name": t.get("name", symbol), "market": market,
            "date": sell_date, "pattern": t.get("sell_pattern") or "other",
            "realized_rate": t.get("realized_rate"),
            # 당일 놓친 상승: 매도일 high 대비 매도 ref. ref 가 [low,high] 안이라 >= 0.
            "same_day_up": max(0.0, highs[idx] / ref - 1.0),
        }
        n = len(dates)
        for h in horizons:
            end = min(idx + h, n - 1)
            if end <= idx:
                continue
            seg_hi = max(highs[idx + 1:end + 1])
            seg_lo = min(lows[idx + 1:end + 1])
            row[f"up_{h}"] = seg_hi / ref - 1.0      # 놓친 상승
            row[f"down_{h}"] = seg_lo / ref - 1.0     # 막은 하락(음수)
            row[f"net_{h}"] = closes[end] / ref - 1.0  # 홀드 시 종가 기준
        details.append(row)

    # 패턴별 집계 (primary horizon 기준 분류 + 전 horizon 평균)
    by_pattern: dict = {}
    pu, pd_, pn = f"up_{primary}", f"down_{primary}", f"net_{primary}"
    for r in details:
        pat = r["pattern"]
        b = by_pattern.setdefault(pat, {"count": 0, "_up": [], "_down": [], "_net": [],
                                        "_sd": [], "too_early": 0, "protected": 0})
        b["count"] += 1
        b["_sd"].append(r.get("same_day_up", 0.0))
        if pu in r:
            b["_up"].append(r[pu]); b["_down"].append(r[pd_]); b["_net"].append(r.get(pn, 0.0))
            if r[pu] >= classify_pct and -r[pd_] < classify_pct:
                b["too_early"] += 1
            elif -r[pd_] >= classify_pct and r[pu] < classify_pct:
                b["protected"] += 1

    def _mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    out_patterns = {}
    for pat, b in by_pattern.items():
        up, down, net = _mean(b["_up"]), _mean(b["_down"]), _mean(b["_net"])
        out_patterns[pat] = {
            "count": b["count"],
            "avg_same_day_missed": round(_mean(b["_sd"]) * 100, 2),  # % 당일 장중 놓친 상승
            "avg_missed_upside": round(up * 100, 2),    # % 놓친 상승(forward)
            "avg_avoided_drop": round(-down * 100, 2),  # % 막은 하락(양수)
            "avg_net_if_held": round(net * 100, 2),     # % 홀드 시 종가
            "too_early": b["too_early"],
            "protected": b["protected"],
            "verdict": _verdict(up, down, classify_pct),
        }
    # 정렬: 조기매도(놓친 상승 큰 것) 우선 — 고쳐야 할 패턴이 위로
    out_patterns = dict(sorted(out_patterns.items(),
                               key=lambda kv: kv[1]["avg_missed_upside"], reverse=True))

    all_up = [r[pu] for r in details if pu in r]
    all_down = [r[pd_] for r in details if pd_ in r]
    all_sd = [r.get("same_day_up", 0.0) for r in details]
    overall = {
        "analyzed": len(details), "pending": pending,
        "avg_same_day_missed": round(_mean(all_sd) * 100, 2),
        "avg_missed_upside": round(_mean(all_up) * 100, 2),
        "avg_avoided_drop": round(-_mean(all_down) * 100, 2),
    }
    return {"by_pattern": out_patterns, "overall": overall, "details": details,
            "pending": pending, "horizons": horizons, "primary": primary}
