from __future__ import annotations
"""Claude 기반 자동 종목 선별 모듈.

매일 장 시작 전 Claude가 웹 검색으로:
  1. 시장 동향/섹터 분석
  2. 유망 종목 발굴 (모멘텀, 실적, 테마)
  3. 기존 종목 유지/교체 판단
  4. 위험 종목 제거

선별 결과를 data/selected_stocks.json에 저장.
"""

import json
import logging
import os
from datetime import datetime

import anthropic

logger = logging.getLogger(__name__)

SELECTED_FILE = os.path.join("data", "selected_stocks.json")


def _load_selected() -> dict:
    if os.path.exists(SELECTED_FILE):
        with open(SELECTED_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"kr": [], "us": [], "last_updated": ""}


def _save_selected(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(SELECTED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class StockScreener:
    """Claude AI 기반 자동 종목 선별.

    주기:
      - 평시: screen_interval_hours마다 재선별 (기본 2시간)
      - 위기: 급락/전쟁 감지 시 즉시 방어 종목으로 재선별
    """

    def __init__(self, api_key: str = "", model: str = "claude-sonnet-5", config: dict | None = None):
        self._api_key = api_key
        self._has_api = bool(api_key)
        if api_key:
            self.client = anthropic.Anthropic(api_key=api_key)
        else:
            self.client = None
        self.model = model

        screen_cfg = (config or {}).get("screening", {})
        self.kr_count: int = screen_cfg.get("kr_count", 5)
        self.us_count: int = screen_cfg.get("us_count", 5)
        self.style: str = screen_cfg.get("style", "balanced")
        self.sectors: list = screen_cfg.get("preferred_sectors", [])
        self.avoid: list = screen_cfg.get("avoid", [])
        self.min_market_cap_kr: str = screen_cfg.get("min_market_cap_kr", "1조원")
        self.min_market_cap_us: str = screen_cfg.get("min_market_cap_us", "10B")
        self.interval_hours: float = screen_cfg.get("screen_interval_hours", 2)
        self.crisis_interval_min: int = screen_cfg.get("crisis_interval_minutes", 30)

        self._selected = _load_selected()
        self._crisis_mode = False

    # ── 한국 종목 선별 ──

    def screen_kr_stocks(self, current_stocks: list[dict], performance_summary: str = "",
                         max_price_krw: int = 0) -> list[dict]:
        """한국 유망 종목 선별.

        Args:
            current_stocks: 현재 매매 중인 종목 리스트
            performance_summary: 보상 엔진 성과 요약
            max_price_krw: 주당 최대 가격 (0이면 제한 없음)

        Returns:
            [{"code": "005930", "name": "삼성전자", "reason": "선정 사유"}, ...]
        """
        datetime.now().strftime("%Y-%m-%d")
        current_list = ", ".join(f"{s.get('name', s['code'])}({s['code']})" for s in current_stocks) if current_stocks else "없음"

        price_rule = ""
        if max_price_krw > 0:
            price_rule = f"\n- 주당 가격 {max_price_krw:,}원 이하 종목만 선별 (소액 계좌, 이 조건 필수!)"

        prompt = self._build_screening_prompt(
            market="한국",
            current_list=current_list,
            count=self.kr_count,
            performance_summary=performance_summary,
            extra_rules=(
                f"- 종목코드는 반드시 6자리 숫자 (예: '005930')\n"
                f"- 시가총액 {self.min_market_cap_kr} 이상\n"
                f"- 거래소: KOSPI, KOSDAQ"
                f"{price_rule}"
            ),
            json_format=(
                '[\n'
                '  {"code": "005930", "name": "삼성전자", "reason": "선정 사유 1-2문장"},\n'
                '  ...\n'
                ']'
            ),
        )

        logger.info("Claude 한국 종목 선별 중... (목표 %d종목)", self.kr_count)
        result = self._call_claude_with_search(prompt)
        stocks = self._parse_stock_list(result, key="code")

        if stocks:
            logger.info("한국 종목 선별 완료: %d종목", len(stocks))
            for s in stocks:
                logger.info("  %s(%s): %s", s.get("name"), s.get("code"), s.get("reason", "")[:60])
        else:
            logger.warning("한국 종목 선별 실패, 기존 유지")
            stocks = current_stocks

        return stocks[:self.kr_count]

    # ── 미국 종목 선별 ──

    def screen_us_stocks(self, current_stocks: list[dict], performance_summary: str = "",
                         max_price_usd: float = 0) -> list[dict]:
        """미국 유망 종목 선별.

        Returns:
            [{"ticker": "AAPL", "name": "Apple", "exchange": "NASD", "reason": "..."}, ...]
        """
        current_list = ", ".join(f"{s.get('name', s['ticker'])}({s['ticker']})" for s in current_stocks) if current_stocks else "없음"

        price_rule = ""
        if max_price_usd > 0:
            price_rule = f"\n- 주당 가격 ${max_price_usd:.0f} 이하 종목만 선별 (소액 계좌, 이 조건 필수!)"

        prompt = self._build_screening_prompt(
            market="미국",
            current_list=current_list,
            count=self.us_count,
            performance_summary=performance_summary,
            extra_rules=(
                f"- ticker는 영문 심볼 (예: 'AAPL', 'NVDA')\n"
                f"- exchange는 NASD(나스닥), NYSE(뉴욕), AMEX 중 하나\n"
                f"- 시가총액 {self.min_market_cap_us} 이상"
                f"{price_rule}"
            ),
            json_format=(
                '[\n'
                '  {"ticker": "AAPL", "name": "Apple", "exchange": "NASD", "reason": "선정 사유"},\n'
                '  ...\n'
                ']'
            ),
        )

        logger.info("Claude 미국 종목 선별 중... (목표 %d종목)", self.us_count)
        result = self._call_claude_with_search(prompt)
        stocks = self._parse_stock_list(result, key="ticker")

        if stocks:
            logger.info("미국 종목 선별 완료: %d종목", len(stocks))
            for s in stocks:
                logger.info("  %s(%s/%s): %s", s.get("name"), s.get("ticker"), s.get("exchange", "NASD"), s.get("reason", "")[:60])
        else:
            logger.warning("미국 종목 선별 실패, 기존 유지")
            stocks = current_stocks

        return stocks[:self.us_count]

    # ── 전체 선별 (한국+미국) ──

    def screen_all(self, kr_current: list[dict], us_current: list[dict],
                   performance_summary: str = "",
                   max_price_krw: int = 0, max_price_usd: float = 0) -> dict:
        """한국+미국 종목 동시 선별.

        Returns:
            {"kr": [...], "us": [...], "last_updated": " "}
        """
        kr = self.screen_kr_stocks(kr_current, performance_summary, max_price_krw=max_price_krw)
        us = self.screen_us_stocks(us_current, performance_summary, max_price_usd=max_price_usd)

        self._selected = {
            "kr": kr,
            "us": us,
            "last_updated": datetime.now().isoformat(),
        }
        _save_selected(self._selected)

        return self._selected

    def get_selected(self) -> dict:
        """마지막 선별 결과."""
        return self._selected

    def needs_update(self) -> bool:
        """선별 주기가 지났으면 True."""
        last = self._selected.get("last_updated", "")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True

        interval = self.crisis_interval_min if self._crisis_mode else self.interval_hours * 60
        elapsed = (datetime.now() - last_dt).total_seconds() / 60
        return elapsed >= interval

    # ── 위기 모드 ──

    def enter_crisis_mode(self):
        """위기 모드 진입 — 선별 주기 단축 + 방어적 종목으로 전환."""
        if not self._crisis_mode:
            self._crisis_mode = True
            logger.warning("종목 선별: 위기 모드 진입 (주기 %d분)", self.crisis_interval_min)

    def exit_crisis_mode(self):
        if self._crisis_mode:
            self._crisis_mode = False
            logger.info("종목 선별: 위기 모드 해제 (주기 %.0f시간)", self.interval_hours)

    def is_crisis_mode(self) -> bool:
        return self._crisis_mode

    def screen_defensive(self, kr_current: list[dict], us_current: list[dict], crisis_reason: str = "") -> dict:
        """위기 시 방어 종목으로 긴급 재선별.

        전쟁/폭락 시: 고배당, 방산, 필수소비재, 금 등 방어주로 전환.
        """
        logger.warning("긴급 방어 종목 선별 (사유: %s)", crisis_reason)

        today = datetime.now().strftime("%Y-%m-%d")
        kr_list = ", ".join(f"{s.get('name', s.get('code', ''))}" for s in kr_current) if kr_current else "없음"
        us_list = ", ".join(f"{s.get('name', s.get('ticker', ''))}" for s in us_current) if us_current else "없음"

        prompt = f"""당신은 주식 시장 위기 관리 전문가입니다.
현재 심각한 시장 위기 상황입니다: {crisis_reason}

오늘 날짜: {today}

## 현재 보유 종목
한국: {kr_list}
미국: {us_list}

## 긴급 종목 교체 요청
폭락/전쟁/위기 상황에서 **자산을 방어**할 수 있는 종목으로 교체해주세요.

### 방어 종목 기준:
- 하락장에서 상대적으로 강한 방어주
- 고배당 대형 우량주 (배당수익률 3% 이상)
- 방산/국방 관련주 (전쟁 상황 시)
- 필수소비재 (경기방어)
- 유틸리티/통신 (안정적 현금흐름)
- 금/원자재 관련 ETF
- 달러 강세 수혜주
- 인버스 ETF는 제외 (위험)

### 현재 보유 중 유지할 종목도 판단해주세요
- 방어력 있는 종목은 유지
- 성장주/고변동성 종목은 제거

웹 검색으로 현재 시장 상황을 확인하고, 방어 종목을 선별해주세요.

응답은 반드시 아래 JSON 형식으로만:
{{
  "kr": [
    {{"code": "종목코드6자리", "name": "종목명", "reason": "방어 사유"}}
  ],
  "us": [
    {{"ticker": "심볼", "name": "종목명", "exchange": "NASD/NYSE", "reason": "방어 사유"}}
  ]
}}"""

        try:
            raw = self._call_claude_with_search(prompt)
            text = raw.strip()

            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                result = json.loads(text[start:end + 1])
                kr = result.get("kr", [])
                us = result.get("us", [])

                if kr or us:
                    self._selected = {
                        "kr": kr[:self.kr_count] if kr else kr_current,
                        "us": us[:self.us_count] if us else us_current,
                        "last_updated": datetime.now().isoformat(),
                        "mode": "defensive",
                        "crisis_reason": crisis_reason,
                    }
                    _save_selected(self._selected)

                    logger.warning("방어 종목 선별 완료:")
                    for s in self._selected["kr"]:
                        logger.warning("  KR %s: %s", s.get("name"), s.get("reason", "")[:50])
                    for s in self._selected["us"]:
                        logger.warning("  US %s: %s", s.get("name"), s.get("reason", "")[:50])

                    return self._selected

        except Exception:
            logger.exception("방어 종목 선별 실패")

        return {"kr": kr_current, "us": us_current, "last_updated": datetime.now().isoformat()}

    # ── 내부 ──

    def _build_screening_prompt(
        self, market: str, current_list: str, count: int,
        performance_summary: str, extra_rules: str, json_format: str,
    ) -> str:
        today = datetime.now().strftime("%Y-%m-%d")

        style_desc = {
            "aggressive": "공격적 — 높은 변동성, 성장주 위주, 테마/모멘텀 중시",
            "balanced": "균형 — 성장+가치 혼합, 적당한 변동성, 실적 기반",
            "conservative": "보수적 — 대형 우량주, 낮은 변동성, 배당+안정성 중시",
            "defensive": "디펜시브 — 메가캡 우량주만, 20일 변동성 3% 이하, 60일선 위 정배열, 데드크로스 회피",
        }.get(self.style, "균형")

        sectors_str = ", ".join(self.sectors) if self.sectors else "제한 없음"
        avoid_str = ", ".join(self.avoid) if self.avoid else "없음"

        return f"""당신은 {market} 주식 시장 전문 애널리스트입니다.
오늘({today}) 자동매매 봇이 거래할 최적의 종목 {count}개를 선별해주세요.

## 선별 기준
- 투자 스타일: {style_desc}
- 선호 섹터: {sectors_str}
- 회피 종목/섹터: {avoid_str}
{extra_rules}

## 현재 매매 중인 종목
{current_list}

## 과거 매매 성과
{performance_summary if performance_summary else "(데이터 없음)"}

## 선별 요청
다음을 웹 검색으로 조사하여 종목을 선별하세요:
1. 오늘 시장 전망 / 주요 이슈
2. 섹터별 모멘텀 (어떤 업종이 강세인지)
3. 실적 시즌이면 실적 서프라이즈 종목
4. 기관/외국인 수급 좋은 종목
5. 기술적으로 매수 타이밍인 종목 (지지선 부근, 돌파 직전 등)

## 중요 원칙
- **약세 추세 종목 제외**: 5일선 < 20일선 (데드크로스) + 60일선 아래는 절대 선정 금지.
  로컬 추세 필터에서 자동 차단되어 LLM이 추천해도 매수 불가.
- **변동성 5% 이상 종목 회피**: 20일 일중 변동성이 5% 이상이면 선정 금지 (RIOT/SMCI/NIO/GRAB 패턴).
- **메가캡 우선**: 시총 큰 종목일수록 변동성 낮고 손실 폭 작음.
- 현재 종목 중 여전히 유망한 것은 유지하세요 (불필요한 교체 자제)
- 추세가 꺾이거나 악재가 있는 종목은 제거하세요
- 각 종목의 선정 사유를 반드시 기재하세요
- 상장폐지 위험, 관리종목, 투자경고 종목은 절대 포함하지 마세요

반드시 아래 JSON 배열 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{json_format}"""

    def _call_claude_with_search(self, prompt: str) -> str:
        """Claude에게 웹 검색 + 종목 선별 요청 (API 또는 CLI)."""
        # CLI 우선 (Max 구독)
        from zusik.clients.claude_client import ClaudeClient
        cl = ClaudeClient(prefer_cli=True)
        if cl.is_cli:
            #: 종목 선별 tier="hard"(sonnet) → "cheap_web"(agy/codex 우선).
            # 사용자 Claude 쿼터 절감 — web_search 유지.
            return cl.message(prompt, use_web_search=True, tier="cheap_web")

        # API 폴백
        if self.client:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                tools=[{"type": "web_search_20250305"}],
                messages=[{"role": "user", "content": prompt}],
            )
            parts = []
            for block in response.content:
                if block.type == "text":
                    parts.append(block.text)
            return "\n".join(parts)

        return ""

    @staticmethod
    def _parse_stock_list(raw: str, key: str) -> list[dict]:
        """Claude 응답에서 JSON 배열 파싱."""
        text = raw.strip()

        # ```json ... ``` 블록 추출
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        # [ 로 시작하는 부분 찾기
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            return []

        try:
            items = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            logger.warning("종목 리스트 JSON 파싱 실패: %s", text[:200])
            return []

        # 유효성 검증
        valid = []
        for item in items:
            if isinstance(item, dict) and item.get(key):
                valid.append(item)

        return valid
