from __future__ import annotations
"""Claude 기억 시스템.

매 분석마다 Claude에게 주입되는 단기/장기 기억:

1. 매매 일지 — 과거 분석→결과 기록 ("내가 매수 추천한 삼성전자가 +5% 됐다")
2. 시장 메모 — Claude가 직접 남긴 시장 관찰 노트
3. 실수 노트 — 틀린 판단과 교훈 ("RSI 30에서 매수했는데 더 빠졌다, 왜?")
4. 종목 인사이트 — 종목별 누적 관찰

저장: data/claude_memory.json
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

MEMORY_FILE = os.path.join("data", "claude_memory.json")
MAX_TRADE_LOG = 30       # 최근 30건 매매 기억
MAX_MARKET_MEMO = 10     # 시장 메모 10개
MAX_MISTAKES = 15        # 실수 노트 15개
MAX_INSIGHT_PER_STOCK = 5  # 종목당 인사이트 5개


def _load() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"trade_log": [], "market_memos": [], "mistakes": [], "stock_insights": {}}


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class ClaudeMemory:
    """Claude의 기억 저장소."""

    def __init__(self):
        self._mem = _load()

    # ══════════════════════════════════════
    # 1. 매매 일지 — "내가 추천한 결과가 어땠는지"
    # ══════════════════════════════════════

    def record_trade(self, code: str, name: str, side: str,
                     reasoning: str, analyst_signals: dict | None = None):
        """매수/매도 시점의 분석 기록."""
        entry = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "code": code,
            "name": name,
            "side": side,
            "reasoning": reasoning[:200],
            "analyst_signals": analyst_signals or {},
            "outcome": None,  # 매도 시 채워짐
        }
        self._mem["trade_log"].append(entry)
        self._mem["trade_log"] = self._mem["trade_log"][-MAX_TRADE_LOG:]
        _save(self._mem)

    def record_outcome(self, code: str, realized_rate: float, lesson: str = ""):
        """매도 후 결과 기록 — 가장 최근 해당 종목 매수 기록에 결과 추가."""
        for entry in reversed(self._mem["trade_log"]):
            if entry["code"] == code and entry["outcome"] is None:
                entry["outcome"] = {
                    "realized_rate": realized_rate,
                    "result": "수익" if realized_rate > 0 else "손실",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                }
                break

        # 손실이면 자동으로 실수 노트 추가
        if realized_rate < -2:
            self.add_mistake(code, f"{code} 매매에서 {realized_rate:+.1f}% 손실. {lesson}")

        _save(self._mem)

    # ══════════════════════════════════════
    # 2. 시장 메모 — Claude가 남기는 관찰 노트
    # ══════════════════════════════════════

    # ══════════════════════════════════════
    # 3. 실수 노트 — 틀린 판단에서 배운 교훈
    # ══════════════════════════════════════

    def add_mistake(self, code: str, lesson: str):
        """손실 매매에서 배운 교훈."""
        self._mem["mistakes"].append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "code": code,
            "lesson": lesson[:300],
        })
        self._mem["mistakes"] = self._mem["mistakes"][-MAX_MISTAKES:]
        _save(self._mem)

    # ══════════════════════════════════════
    # 4. 종목 인사이트 — 종목별 누적 관찰
    # ══════════════════════════════════════

    # ══════════════════════════════════════
    # 프롬프트용 기억 텍스트 생성
    # ══════════════════════════════════════

    def build_memory_prompt(self, stock_code: str = "") -> str:
        """Claude 프롬프트에 주입할 기억 텍스트.

        Args:
            stock_code: 현재 분석 중인 종목 (해당 종목 기억 우선 표시)

        Returns:
            프롬프트에 삽입할 기억 텍스트 (빈 문자열이면 기억 없음)
        """
        sections = []

        # 최근 매매 결과 (성적표)
        recent_trades = [t for t in self._mem["trade_log"] if t.get("outcome")]
        if recent_trades:
            lines = []
            wins = sum(1 for t in recent_trades if t["outcome"]["realized_rate"] > 0)
            total = len(recent_trades)
            lines.append(f"최근 {total}건 매매: {wins}승 {total - wins}패")
            for t in recent_trades[-5:]:
                o = t["outcome"]
                lines.append(
                    f"  {t['date']} {t['name']} {t['side']} → {o['result']} {o['realized_rate']:+.1f}%"
                    f" (근거: {t['reasoning'][:50]})"
                )
            sections.append("## 내 최근 매매 결과\n" + "\n".join(lines))

        # 현재 종목 관련 기억
        if stock_code and stock_code in self._mem.get("stock_insights", {}):
            si = self._mem["stock_insights"][stock_code]
            notes = "\n".join(f"  {n['date']}: {n['note']}" for n in si["notes"])
            sections.append(f"## {si['name']} 관련 과거 관찰\n{notes}")

        # 실수에서 배운 교훈
        mistakes = self._mem.get("mistakes", [])
        if mistakes:
            lines = [f"  {m['date']}: {m['lesson']}" for m in mistakes[-5:]]
            sections.append("## 과거 실수에서 배운 교훈 (같은 실수 반복 금지)\n" + "\n".join(lines))

        # 시장 메모
        memos = self._mem.get("market_memos", [])
        if memos:
            lines = [f"  {m['date']}: {m['memo']}" for m in memos[-3:]]
            sections.append("## 시장 관찰 메모\n" + "\n".join(lines))

        if not sections:
            return ""

        return "\n\n".join(sections)
