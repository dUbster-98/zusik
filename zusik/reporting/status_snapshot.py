from __future__ import annotations
"""봇 상태 단일 스냅샷 — 흩어진 상태(자산·보유·토글·시장·WS·무결성·결정)를 한 곳으로.

build_status_snapshot(bot) -> dict 를 data/status.json 으로 매 사이클 내보내면,
CLI(--status)와 Rust 웹(/api/status)이 같은 한 소스를 쓴다. effective 기준이라 T+2 팬텀 없음.
모든 섹션 defensive(getattr·try/except) — 어떤 상태에서도 예외 없이 dict 반환.
"""

import os


def _tail_lines(path: str, n: int) -> list:
    try:
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except Exception:
        return []


def build_status_snapshot(bot, generated_at: str = "") -> dict:
    snap: dict = {"generated_at": generated_at}
    cfg = getattr(bot, "config", {}) or {}

    # 1) 자산/손익 + 종목별 (effective 기준 — results 요약 재사용)
    latest = None
    try:
        from zusik.storage.portfolio_tracker import EQUITY_CURVE_FILE, _load_json
        curve = _load_json(EQUITY_CURVE_FILE)
        if isinstance(curve, list) and curve:
            latest = max(curve, key=lambda c: c.get("date", ""), default=None)
    except Exception:
        pass
    try:
        from zusik.reporting.results_html import build_results_summary
        rs = build_results_summary(bot.tracker)
        snap["equity"] = {k: rs.get(k) for k in (
            "effective_equity", "deposits", "realized_total", "unrealized",
            "effective_total", "return_pct", "max_drawdown", "win_rate", "sells")}
        snap["by_stock"] = rs.get("by_stock", [])[:20]
    except Exception:
        snap["equity"] = {}
        snap["by_stock"] = []

    # 2) 보유 현황 (positions.json — API 호출 없음)
    try:
        pos = getattr(bot.positions, "_positions", {}) or {}
        snap["holdings"] = [
            {"code": c, "qty": p.get("qty"), "avg_price": p.get("avg_price"),
             "peak_profit_rate": p.get("peak_profit_rate")}
            for c, p in pos.items() if isinstance(p, dict) and (p.get("qty") or 0) > 0]
    except Exception:
        snap["holdings"] = []

    # 3) 시장 개장 (시간/캘린더 기반 — 네트워크 없음)
    snap["market"] = {}
    for key, fn in (("kr_open", "is_market_open"), ("us_open", "is_us_market_open")):
        try:
            snap["market"][key] = bool(getattr(bot.client, fn)())
        except Exception:
            snap["market"][key] = None

    # 3b) LLM 가용성 (message()가 집계 — /헬스·웹 공용 소스)
    try:
        from zusik.clients.claude_client import get_llm_health
        h = get_llm_health()
        snap["llm"] = {"status": h.get("status", "ok"),
                       "consecutive_fail": h.get("consecutive_fail", 0),
                       "last_reason": h.get("last_reason", "")}
    except Exception:
        snap["llm"] = {}

    # 4) 켜진 토글 (지금 뭐가 ON 인가 — 한눈에)
    ap = cfg.get("ai_providers", {}) or {}
    rt = cfg.get("realtime", {}) or {}
    ar = cfg.get("ai_routing", {}) or {}
    rk = cfg.get("risk", {}) or {}
    snap["toggles"] = {
        "realtime": rt.get("enabled", True),
        "realtime_entry": rt.get("entry_enabled", False),
        "local_llm": bool(ap.get("local_enabled", False)),
        "ambiguous_routing": ar.get("ambiguous_sell_enabled", True),
        "fast_entry": (cfg.get("fast_entry", {}) or {}).get("enabled", True),
        "fast_fall_guard": (rk.get("fast_fall_guard", {}) or {}).get("enabled", True),
        "inverse": (cfg.get("inverse", {}) or {}).get("enabled", False),
        "ai_signals": (cfg.get("ai_signals", {}) or {}).get("enabled", True),
        "integrity_halt": rk.get("halt_buys_on_integrity_violation", False),
        "defensive_mode_cfg": rk.get("defensive_mode_enabled", False),
    }

    # 5) 런타임 상태 플래그
    wsm = getattr(bot, "_ws_manager", None)
    snap["state"] = {
        "mode": getattr(bot, "_active_mode", None),
        "market_condition": getattr(bot, "_market_condition", None),
        "defensive": getattr(bot, "_defensive_mode", None),
        "fast_fall_active": getattr(bot, "_fast_fall_active", False),
        "daily_target_reached": getattr(bot, "_daily_target_reached", None),
        "daily_loss_halted": getattr(bot, "_daily_loss_halted", None),
        "ws_active": bool(getattr(wsm, "is_active", False)) if wsm else False,
    }

    # 손실측 자가학습 — 현재 보정된 hold floor 노출(반영 확인용). 기본값과 다르면 학습 작동 중.
    try:
        if rk.get("loss_learning_enabled", True):
            snap["state"]["loss_hold_floor"] = round(float(bot._learned_hold_floor()), 4)
            snap["state"]["loss_hold_floor_default"] = round(
                float(rk.get("pullback_hold_floor", -0.09)), 4)
    except Exception:
        pass

    # 6) 무결성 (순수 검사 — 부작용 없음)
    try:
        from zusik.core.resilience import verify_pnl_invariants
        issues = verify_pnl_invariants(
            trades=getattr(bot.tracker, "_trades", []) or [],
            deposits=bot.tracker.get_total_deposits(),
            latest_snapshot=latest,
            positions=getattr(bot.positions, "_positions", {}) or {})
        snap["state"]["integrity_ok"] = (len(issues) == 0)
        snap["state"]["integrity_issues"] = issues[:3]
    except Exception:
        snap["state"]["integrity_ok"] = None

    # 7) 최근 매매 사유 (decisions.log tail)
    snap["recent_decisions"] = _tail_lines(os.path.join("logs", "decisions.log"), 8)
    return snap


def _onoff(v) -> str:
    if v is True:
        return "ON"
    if v is False:
        return "off"
    return "?"


def render_status_text(snap: dict) -> str:
    s = snap or {}
    eq = s.get("equity", {}) or {}
    mk = s.get("market", {}) or {}
    st = s.get("state", {}) or {}
    tg = s.get("toggles", {}) or {}
    L = []
    L.append("=" * 60)
    L.append(f" zusik 상태  ({s.get('generated_at', '')})")
    L.append("=" * 60)
    if eq:
        L.append(f" 실효자산 {eq.get('effective_equity', 0):>14,}원   "
                 f"수익률 {eq.get('return_pct', 0):+.2f}%  MaxDD {eq.get('max_drawdown', 0):+.2f}%")
        L.append(f" 실현 {eq.get('realized_total', 0):>+12,}  미실현 {eq.get('unrealized', 0):>+12,}  "
                 f"승률 {eq.get('win_rate', 0)}% ({eq.get('sells', 0)}매도)")
    L.append(f" 시장: KR {'개장' if mk.get('kr_open') else '마감'} · US {'개장' if mk.get('us_open') else '마감'}"
             f"   국면 {st.get('market_condition', '?')}  모드 {st.get('mode', '?')}")
    flags = []
    if st.get("defensive"):
        flags.append("DEFENSIVE")
    if st.get("fast_fall_active"):
        flags.append("급락가드")
    if st.get("daily_target_reached"):
        flags.append("일일목표달성")
    if st.get("daily_loss_halted"):
        flags.append("일일손실중단")
    if st.get("ws_active"):
        flags.append("WS활성")
    ig = st.get("integrity_ok")
    flags.append("무결성 OK" if ig else ("무결성 경고" if ig is False else "무결성 ?"))
    L.append(" 상태: " + (" · ".join(flags) if flags else "정상"))
    lf = st.get("loss_hold_floor")
    df = st.get("loss_hold_floor_default")
    if lf is not None and df is not None and abs(lf - df) >= 0.005:
        L.append(f" 손실학습: hold floor {lf * 100:+.1f}% (기본 {df * 100:+.1f}%) "
                 f"— 손실 컷 사후데이터로 자동 보정 중")
    L.append(" 토글: " + "  ".join(f"{k}={_onoff(v)}" for k, v in tg.items()))
    hold = s.get("holdings", []) or []
    if hold:
        L.append(f" 보유 {len(hold)}종목: " + ", ".join(
            f"{h.get('code')}×{h.get('qty')}" for h in hold[:10]))
    by = s.get("by_stock", []) or []
    if by:
        L.append(" 종목별 손익(누적 상위):")
        for x in by[:5]:
            L.append(f"   {str(x.get('name') or x.get('code', '')):<18} {x.get('pnl', 0):>+12,}원 "
                     f"({x.get('count', 0)}매도)")
    rd = s.get("recent_decisions", []) or []
    if rd:
        L.append(" 최근 결정:")
        for ln in rd[-5:]:
            L.append(f"   {ln[:90]}")
    L.append("=" * 60)
    return "\n".join(L)
