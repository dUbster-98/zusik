from __future__ import annotations
"""포트폴리오 아레나 — 전략 + AI 에이전트 가상 포트폴리오 경쟁.

경쟁 참가자:
  - 4인 Claude 애널리스트 (fundamental / sentiment / quant / generalist)
  - 로컬 전략 (adaptive / momentum_breakout / ma_cross / rsi / bollinger / macd_rsi)

실행 방식:
  1. 감시 종목 전체를 주기적으로 스캔 (tick마다)
  2. 로컬 전략은 매 스캔마다 신호 생성 → 가상 매매 즉시 실행
  3. Claude 애널리스트는 실전 분석 시 신호 기록
  4. 보유 가상 포지션은 mark-to-market으로 실시간 평가
  5. 자동 포지션 관리: 손절 -10%, 익절 +15%, 최대 보유 30일

저장: data/arena.json
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

ARENA_FILE = os.path.join("data", "arena.json")
VIRTUAL_CAPITAL = 1_000_000  # 가상 시작 자본 100만원

# 가상 포지션 관리 파라미터
VIRTUAL_STOP_LOSS = -0.10        # -10% 손절
VIRTUAL_TAKE_PROFIT = 0.15       # +15% 익절
VIRTUAL_MAX_HOLD_DAYS = 30       # 최대 보유 30일

# 로컬 전략 경쟁자 (Claude 없이도 독립 운영)
LOCAL_STRATEGIES = [
    "adaptive",
    "momentum_breakout",
    "ma_cross",
    "rsi",
    "bollinger",
    "macd_rsi",
]

CLAUDE_AGENTS = ["fundamental", "sentiment", "quant", "generalist"]


def _load() -> dict:
    if os.path.exists(ARENA_FILE):
        with open(ARENA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(ARENA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class PortfolioArena:
    """전략 + AI 에이전트 가상 포트폴리오 경쟁."""

    # 전체 경쟁자 = Claude 4인 + 로컬 전략
    @property
    def COMPETITORS(self) -> list[str]:
        return CLAUDE_AGENTS + LOCAL_STRATEGIES

    # 하위 호환 (기존 get_leader 호출부)
    AGENTS = CLAUDE_AGENTS

    def __init__(self):
        self._data = _load()
        self._migrate_if_needed()

    def _migrate_if_needed(self):
        """기존 4인만 있는 데이터에 로컬 전략 포트폴리오 추가."""
        if not self._data.get("portfolios"):
            self._init_portfolios()
            return
        added = False
        for name in self.COMPETITORS:
            if name not in self._data["portfolios"]:
                self._data["portfolios"][name] = self._blank_portfolio()
                added = True
        if added:
            _save(self._data)

    def _blank_portfolio(self) -> dict:
        return {
            "cash": VIRTUAL_CAPITAL,
            "holdings": {},  # {code: {"qty": N, "avg_price": P, "buy_date": iso, "high": max_price}}
            "total_trades": 0,
            "wins": 0,
            "realized_pnl": 0,
            "start_date": datetime.now().isoformat(),
        }

    def _init_portfolios(self):
        self._data["portfolios"] = {name: self._blank_portfolio() for name in self.COMPETITORS}
        self._data["week_start"] = datetime.now().isoformat()
        self._data["rankings_history"] = []
        _save(self._data)

    # ══════════════════════════════════════
    # 가상 매매
    # ══════════════════════════════════════

    def record_signal(self, competitor: str, code: str, signal: str,
                      price: float, invest_ratio: float = 0.15):
        """경쟁자의 신호를 가상 포트폴리오에 반영.

        기존 호환성: agent 이름(claude 애널리스트)도 지원.
        """
        if competitor not in self._data.get("portfolios", {}):
            return

        p = self._data["portfolios"][competitor]

        if signal in ("buy", "long_term_buy"):
            # 이미 보유 중이면 스킵 (분산 우선 — 한 종목에 몰빵 방지)
            if code in p["holdings"]:
                return
            # 현금의 invest_ratio만큼 배팅
            invest = p["cash"] * invest_ratio
            if invest <= 0 or price <= 0:
                return
            qty = int(invest / price)
            if qty <= 0:
                return

            cost = qty * price
            p["cash"] -= cost
            p["holdings"][code] = {
                "qty": qty,
                "avg_price": price,
                "buy_date": datetime.now().isoformat(),
                "high": price,
            }
            p["total_trades"] += 1

        elif signal == "sell" and code in p["holdings"]:
            self._close_position(p, code, price, reason="signal")

        _save(self._data)

    def _close_position(self, p: dict, code: str, price: float, reason: str = "signal"):
        h = p["holdings"].get(code)
        if not h or h["qty"] <= 0:
            return
        proceeds = h["qty"] * price
        pnl = proceeds - h["qty"] * h["avg_price"]
        p["cash"] += proceeds
        p["realized_pnl"] += pnl
        p["total_trades"] += 1
        if pnl > 0:
            p["wins"] += 1
        del p["holdings"][code]

    # ══════════════════════════════════════
    # 자동 포지션 관리 (mark-to-market + 손절/익절/최대보유)
    # ══════════════════════════════════════

    def manage_positions(self, current_prices: dict):
        """모든 가상 포지션을 현재가로 평가 + 자동 매도 체크.

        Args:
            current_prices: {code: price} 현재가 딕셔너리
        """
        if not current_prices:
            return
        changed = False
        now = datetime.now()
        for name, p in self._data.get("portfolios", {}).items():
            codes = list(p["holdings"].keys())
            for code in codes:
                h = p["holdings"][code]
                price = current_prices.get(code)
                if price is None or price <= 0:
                    continue
                # 고점 갱신
                if price > h.get("high", 0):
                    h["high"] = price
                    changed = True

                avg = h["avg_price"]
                profit_rate = (price - avg) / avg if avg > 0 else 0
                buy_date = datetime.fromisoformat(h["buy_date"])
                days_held = (now - buy_date).days

                # ── 자동 매도 조건 ──
                close_reason = None
                if profit_rate <= VIRTUAL_STOP_LOSS:
                    close_reason = f"손절 {profit_rate:+.1%}"
                elif profit_rate >= VIRTUAL_TAKE_PROFIT:
                    close_reason = f"익절 {profit_rate:+.1%}"
                elif days_held >= VIRTUAL_MAX_HOLD_DAYS:
                    close_reason = f"최대 보유 {days_held}일 초과"
                elif h.get("high", 0) > avg * 1.05 and price <= h["high"] * 0.93:
                    close_reason = "트레일링 (고점 대비 -7%+)"

                if close_reason:
                    self._close_position(p, code, price, reason=close_reason)
                    logger.info("아레나 %s 자동 매도: %s @ %s (%s)",
                                name, code, f"{price:.2f}", close_reason)
                    changed = True
        if changed:
            _save(self._data)

    def scan_and_trade(self, stocks: list[dict],
                       ohlcv_fetcher, price_fetcher,
                       strategies: dict = None):
        """감시 종목 전체를 돌며 각 로컬 전략이 가상 매매.

        Args:
            stocks: [{"code" or "ticker": ..., "name": ..., "market": "KR"/"US"}]
            ohlcv_fetcher: code → DataFrame 반환 callable
            price_fetcher: code → price 반환 callable (int or float)
            strategies: {이름: Strategy 인스턴스}. None이면 전략 경쟁자는 스킵.
        """
        if not strategies:
            return
        changed = False
        for stock in stocks:
            code = stock.get("code") or stock.get("ticker")
            if not code:
                continue
            try:
                df = ohlcv_fetcher(code)
                price = price_fetcher(code)
            except Exception:
                continue
            if df is None or len(df) < 30 or price is None or price <= 0:
                continue

            for strat_name, strat in strategies.items():
                if strat_name not in self._data.get("portfolios", {}):
                    continue
                try:
                    signal = strat.analyze(df)
                except Exception:
                    continue
                if signal not in ("buy", "long_term_buy", "sell"):
                    continue
                p = self._data["portfolios"][strat_name]
                before_trades = p["total_trades"]
                # record_signal은 _save를 호출하지만 대량 호출 시 비효율 → 메모리 내 변경 후 일괄 저장
                self._apply_signal_inmem(strat_name, code, signal, float(price), 0.15)
                if p["total_trades"] != before_trades:
                    changed = True
        if changed:
            _save(self._data)

    def _apply_signal_inmem(self, competitor: str, code: str, signal: str,
                             price: float, invest_ratio: float):
        """record_signal과 동일 로직, 저장 없이 메모리만 변경."""
        p = self._data["portfolios"].get(competitor)
        if not p:
            return
        if signal in ("buy", "long_term_buy"):
            if code in p["holdings"]:
                return
            invest = p["cash"] * invest_ratio
            if invest <= 0 or price <= 0:
                return
            qty = int(invest / price)
            if qty <= 0:
                return
            cost = qty * price
            p["cash"] -= cost
            p["holdings"][code] = {
                "qty": qty, "avg_price": price,
                "buy_date": datetime.now().isoformat(), "high": price,
            }
            p["total_trades"] += 1
        elif signal == "sell" and code in p["holdings"]:
            self._close_position(p, code, price, reason="signal")

    # ══════════════════════════════════════
    # 순위 + 평가
    # ══════════════════════════════════════

    def get_rankings(self, current_prices: dict = None) -> list[dict]:
        """전체 경쟁자 수익률 순위."""
        results = []
        for name in self.COMPETITORS:
            p = self._data.get("portfolios", {}).get(name)
            if not p:
                continue
            holdings_value = 0
            if current_prices:
                for code, h in p.get("holdings", {}).items():
                    price = current_prices.get(code, h["avg_price"])
                    holdings_value += h["qty"] * price
            total_value = p["cash"] + holdings_value
            return_pct = (total_value - VIRTUAL_CAPITAL) / VIRTUAL_CAPITAL * 100
            win_rate = (p["wins"] / p["total_trades"] * 100) if p["total_trades"] > 0 else 0
            results.append({
                "agent": name,
                "cash": p["cash"],
                "holdings_value": holdings_value,
                "total_value": total_value,
                "return_pct": return_pct,
                "realized_pnl": p["realized_pnl"],
                "total_trades": p["total_trades"],
                "win_rate": win_rate,
                "open_positions": len(p.get("holdings", {})),
            })

        traded = [r for r in results if r["total_trades"] > 0]
        idle = [r for r in results if r["total_trades"] == 0]
        traded.sort(key=lambda x: x["return_pct"], reverse=True)
        results = traded + idle
        for i, r in enumerate(results):
            r["rank"] = i + 1
        return results

    def get_leader(self, current_prices: dict = None) -> str:
        """실전 반영할 1등 (Claude 4인 중에서만, 기존 호환)."""
        rankings = self.get_rankings(current_prices)
        claude_ranked = [r for r in rankings if r["agent"] in CLAUDE_AGENTS]
        if claude_ranked:
            return claude_ranked[0]["agent"]
        return "quant"

    def get_overall_leader(self, current_prices: dict = None) -> dict | None:
        """전체 경쟁자 중 1등 (Claude + 로컬 통합)."""
        rankings = self.get_rankings(current_prices)
        return rankings[0] if rankings else None

    TIER_ROTATION = ["easy", "medium", "hard", "premium", "easy"]

    def weekly_evaluation(self, current_prices: dict = None) -> dict | None:
        """3일 평가 — Claude 애널리스트 중 꼴찌 리셋 + 티어 교체."""
        week_start = self._data.get("week_start", "")
        if week_start:
            start = datetime.fromisoformat(week_start)
            if (datetime.now() - start).days < 3:
                return None

        rankings = self.get_rankings(current_prices)
        if not rankings:
            return None

        claude_ranked = [r for r in rankings if r["agent"] in CLAUDE_AGENTS and r["total_trades"] > 0]
        if not claude_ranked:
            return None

        leader = claude_ranked[0]
        loser = claude_ranked[-1]

        old_tier = self._data.get("agent_tiers", {}).get(loser["agent"], "easy")
        tier_idx = self.TIER_ROTATION.index(old_tier) if old_tier in self.TIER_ROTATION else 0
        new_tier = self.TIER_ROTATION[tier_idx + 1]
        self._data.setdefault("agent_tiers", {})[loser["agent"]] = new_tier
        self._data["portfolios"][loser["agent"]] = self._blank_portfolio()
        logger.info("아레나: Claude 꼴찌 %s 티어 교체 %s→%s", loser["agent"], old_tier, new_tier)

        self._data["rankings_history"].append({
            "date": datetime.now().isoformat(),
            "rankings": rankings,
        })
        self._data["rankings_history"] = self._data["rankings_history"][-20:]
        self._data["week_start"] = datetime.now().isoformat()
        _save(self._data)
        return {"leader": leader, "loser": loser, "rankings": rankings}

    def get_report(self, current_prices: dict = None) -> str:
        """Discord용 아레나 리포트."""
        rankings = self.get_rankings(current_prices)
        if not rankings:
            return "아레나 데이터 없음"

        name_map = {
            "fundamental": "펀더멘털", "sentiment": "센티멘트",
            "quant": "퀀트(AI)", "generalist": "종합",
            "adaptive": "적응형", "momentum_breakout": "돌파",
            "ma_cross": "이평교차", "rsi": "〽RSI",
            "bollinger": "볼린저", "macd_rsi": "MACD+RSI",
        }

        lines = ["── 포트폴리오 아레나 ──", ""]
        for i, r in enumerate(rankings):
            prefix = f"#{i+1}"
            name = name_map.get(r["agent"], r["agent"])
            idle = " (불참)" if r["total_trades"] == 0 else ""
            lines.append(
                f"{prefix} {name}: {r['return_pct']:+.2f}% "
                f"({r['total_value']:,.0f}원){idle}"
            )
            if r["total_trades"] > 0:
                lines.append(
                    f"     {r['total_trades']}건·승률 {r['win_rate']:.0f}%·"
                    f"보유 {r['open_positions']}·실현 {r['realized_pnl']:+,.0f}"
                )

        lines.append("")
        claude_best = next((r for r in rankings if r["agent"] in CLAUDE_AGENTS), None)
        if claude_best:
            lines.append(f"실전 투입: {name_map.get(claude_best['agent'], claude_best['agent'])}")
        return "\n".join(lines)
