from __future__ import annotations
"""이벤트-수혜 학습 모듈.

Claude가 직접:
  1. 새 이벤트/키워드/수혜종목 추가
  2. 안 맞는 매핑 제거
  3. 매핑별 성과 평가 (수익/손실 추적)
  4. 주기적으로 전체 맵 리뷰 → 최적화 제안

저장: data/event_map.json (학습된 맵)
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

EVENT_MAP_FILE = os.path.join("data", "event_map.json")


def _load_map() -> dict:
    if os.path.exists(EVENT_MAP_FILE):
        with open(EVENT_MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"events": {}, "performance": {}, "last_review": ""}


def _save_map(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(EVENT_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class EventLearner:
    """Claude가 이벤트-수혜 매핑을 학습/평가."""

    def __init__(self, claude_client=None):
        self._client = claude_client  # ClaudeClient (CLI/API)
        self._data = _load_map()

    def record_event_trade(self, event_type: str, sector: str, stock_code: str,
                           realized_pnl: int, realized_rate: float):
        """이벤트 기반 매매 결과 기록."""
        perf = self._data.setdefault("performance", {})
        key = f"{event_type}:{sector}"

        if key not in perf:
            perf[key] = {"trades": 0, "wins": 0, "total_pnl": 0, "stocks": {}}

        p = perf[key]
        p["trades"] += 1
        p["total_pnl"] += realized_pnl
        if realized_pnl > 0:
            p["wins"] += 1

        # 종목별 세부 성과
        p["stocks"].setdefault(stock_code, {"trades": 0, "wins": 0, "pnl": 0})
        p["stocks"][stock_code]["trades"] += 1
        p["stocks"][stock_code]["pnl"] += realized_pnl
        if realized_pnl > 0:
            p["stocks"][stock_code]["wins"] += 1

        _save_map(self._data)

        win_rate = p["wins"] / p["trades"] * 100 if p["trades"] > 0 else 0
        logger.info("이벤트 성과: %s | %d건 승률 %.0f%% | 누적 %s원",
                     key, p["trades"], win_rate, f"{p['total_pnl']:+,}")

