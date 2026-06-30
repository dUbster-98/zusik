from __future__ import annotations
"""스마트 시그널 모듈 — 크로스마켓/적립금/배당/인버스/선제매수.

1. 크로스마켓 시그널: 나스닥 야간 급등 → 다음날 한국 반도체 매수
2. 적립금 타이밍: 시장 급락 감지 → "지금 적립하세요" Discord 알림
3. 배당 캡처: 배당락일 직전 매수 → 배당 수령 → 매도
4. 시간외 매매: US 프리마켓/애프터마켓 트리거
5. 인버스 헷지: 하락장 감지 → 인버스 ETF 매수 → 하락에서 수익
6. 공포 역투자: 공포지수 극단 → 우량주 선제 매수 (역사적 반등 패턴)
7. 전쟁 수혜 자동 편입: 전쟁 감지 시 방산/정유/금/해운 자동 진입
8. 과매도 반등 사냥: RSI 극단 + 거래량 폭증 = 바닥, 선제 매수
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

SIGNALS_FILE = os.path.join("data", "smart_signals.json")


def _load() -> dict:
    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"cross_signals": [], "deposit_alerts": [], "dividend_targets": []}


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class SmartSignals:
    """추가 수익 시그널."""

    # 나스닥 → 한국 연동 종목
    CROSS_MAP = {
        # 나스닥 종목: 한국 연동 종목들
        "NVDA": ["000660", "005930"],       # NVIDIA → SK하이닉스, 삼성전자
        "AAPL": ["005380", "034730"],       # Apple → 현대차(부품), SK(배터리)
        "TSLA": ["373220", "006400"],       # Tesla → LG에솔, 삼성SDI
        "MSFT": ["035420", "035720"],       # Microsoft → NAVER, 카카오
        "AMZN": ["035420"],                 # Amazon → NAVER(클라우드)
    }

    def __init__(self, config: dict):
        self._data = _load()
        self.deposit_amount = config.get("monthly_deposit", {}).get("amount", 100000)
        self.deposit_enabled = config.get("monthly_deposit", {}).get("enabled", True)

    # ══════════════════════════════════════
    # 1. 크로스마켓 시그널
    # ══════════════════════════════════════

    def check_cross_signal(self, us_ticker: str, us_change: float,
                           persist: bool = True) -> list[dict]:
        """미국 종목 급등/급락 → 한국 연동 종목 시그널.

        Args:
            us_ticker: "NVDA" 등
            us_change: 당일 변동률 (0.05 = +5%)
            persist: False면 smart_signals.json 히스토리 기록을 생략한다. 익일 매매가 읽는
                건 bot_aux가 따로 쓰는 cross_signals_kr.json이라, 호출마다 전체 파일을
                재기록하는 비용(과 아무도 안 읽는 히스토리)을 피하기 위함.

        Returns:
            [{"kr_code": "000660", "signal": "buy", "reason": "NVDA +5% → SK하이닉스 연동 기대"}]
        """
        kr_codes = self.CROSS_MAP.get(us_ticker, [])
        if not kr_codes:
            return []

        signals = []
        if us_change >= 0.03:  # +3% 이상 급등
            for code in kr_codes:
                signals.append({
                    "kr_code": code,
                    "signal": "buy",
                    "reason": f"{us_ticker} {us_change:+.1%} 급등 → 한국 연동주 상승 기대",
                    "confidence_boost": min(0.15, us_change * 2),
                })
        elif us_change <= -0.03:  # -3% 이상 급락
            for code in kr_codes:
                signals.append({
                    "kr_code": code,
                    "signal": "caution",
                    "reason": f"{us_ticker} {us_change:+.1%} 급락 → 한국 연동주 하락 주의",
                    "confidence_boost": 0,
                })

        if signals and persist:
            self._data["cross_signals"].append({
                "date": datetime.now().isoformat(),
                "us_ticker": us_ticker,
                "us_change": us_change,
                "kr_signals": signals,
            })
            self._data["cross_signals"] = self._data["cross_signals"][-20:]
            _save(self._data)
            logger.info("크로스시그널: %s %+.1f%% → KR %d종목", us_ticker, us_change * 100, len(signals))

        return signals


    # ══════════════════════════════════════
    # 2. 적립금 타이밍
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 3. 배당 캡처
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 4. 시간외 매매 트리거
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 5. 인버스 헷지 — 하락장에서 수익
    # ══════════════════════════════════════

    # 한국 인버스 ETF — KIS 2년 백테스트 기반 선별 (inverse_backtest.py):
    # 레버리지(-2X)는 변동성 decay로 buy&hold -97%, 타이밍 매매해도 -43% (strictly worse).
    # → 기본 유니버스(default=True)는 -1X·지수매칭만. index별 1개: KOSPI=114800/KOSDAQ=251340/NASDAQ=409820.
    # -2X·중복은 crisis_only(default=False) — 명시적 위기 국면에서만.
    KR_INVERSE_ETF = {
        "114800": {"name": "KODEX 인버스",          "leverage": -1, "index": "kospi",  "default": True},
        "251340": {"name": "KODEX 코스닥150선물인버스", "leverage": -1, "index": "kosdaq", "default": True},
        "409820": {"name": "KODEX 미국나스닥100선물인버스(H)", "leverage": -1, "index": "nasdaq", "default": True},
        "252670": {"name": "KODEX 200선물인버스2X", "leverage": -2, "index": "kospi",  "default": False},
        "252710": {"name": "TIGER 200선물인버스2X",  "leverage": -2, "index": "kospi",  "default": False},
        "278240": {"name": "TIGER 코스닥150 인버스", "leverage": -1, "index": "kosdaq", "default": False},
    }
    # 미국 인버스 ETF — 동일 원칙: -1X(SH)만 기본, -3X(SQQQ/SPXU)는 decay 심해 crisis_only.
    US_INVERSE_ETF = {
        "SH":   {"name": "ProShares Short S&P500",       "leverage": -1, "index": "sp500",  "exchange": "NYSE", "default": True},
        "SQQQ": {"name": "ProShares UltraPro Short QQQ", "leverage": -3, "index": "nasdaq", "exchange": "NASD", "default": False},
        "SPXU": {"name": "ProShares UltraPro Short S&P500", "leverage": -3, "index": "sp500", "exchange": "NYSE", "default": False},
    }

    BEAR_THRESHOLDS = {
        "mild":   -0.02,   # -2% → 1X 인버스
        "strong": -0.04,   # -4% → 2~3X 인버스
        "crash":  -0.06,   # -6% → 현금 보유 (인버스도 위험)
    }


    # ══════════════════════════════════════
    # 6. 공포 역투자 — 극단적 공포에서 선제 매수
    # ══════════════════════════════════════

    # 공포 구간에서 매수할 우량주 (폭락 후 반등 확률 높은 대형주)
    FEAR_BUY_KR = [
        {"code": "005930", "name": "삼성전자"},
        {"code": "000660", "name": "SK하이닉스"},
        {"code": "035420", "name": "NAVER"},
    ]
    FEAR_BUY_US = [
        {"ticker": "AAPL", "name": "Apple", "exchange": "NASD"},
        {"ticker": "MSFT", "name": "Microsoft", "exchange": "NASD"},
        {"ticker": "NVDA", "name": "NVIDIA", "exchange": "NASD"},
    ]


    # ══════════════════════════════════════
    # 7. 이벤트별 수혜 종목 자동 편입
    # ══════════════════════════════════════

    # 이벤트 유형별 키워드 → 수혜 섹터 매핑
    EVENT_MAP = {
        "war": {
            "keywords": ["전쟁", "미사일", "폭격", "공습", "교전", "침공", "war", "missile", "bombing", "attack", "invasion"],
            "sectors": ["defense", "gold", "energy"],
        },
        "middle_east": {
            "keywords": ["이란", "iran", "중동", "호르무즈", "사우디", "이스라엘", "israel"],
            "sectors": ["defense", "gold", "energy", "shipping"],
        },
        "pandemic": {
            "keywords": ["팬데믹", "pandemic", "바이러스", "virus", "봉쇄", "lockdown", "격리", "확진", "변이"],
            "sectors": ["pharma", "biotech", "remote_work", "delivery"],
        },
        "inflation": {
            "keywords": ["인플레", "inflation", "물가", "CPI 상승", "금리 인상", "rate hike", "긴축"],
            "sectors": ["energy", "gold", "commodity", "bank"],
        },
        "rate_cut": {
            "keywords": ["금리 인하", "rate cut", "완화", "비둘기", "dovish", "양적완화", "QE"],
            "sectors": ["growth_tech", "reits", "construction"],
        },
        "recession": {
            "keywords": ["경기침체", "recession", "불황", "GDP 역성장", "실업률 상승", "디폴트"],
            "sectors": ["essential", "utility", "gold", "bond_etf"],
        },
        "tech_boom": {
            "keywords": ["AI 혁명", "AI boom", "반도체 슈퍼사이클", "데이터센터", "GPU 수요", "클라우드 폭발"],
            "sectors": ["ai_semi", "cloud", "growth_tech"],
        },
        "climate": {
            "keywords": ["태풍", "홍수", "가뭄", "폭염", "재해", "기후", "climate", "hurricane", "flood"],
            "sectors": ["construction", "insurance", "energy"],
        },
        "election": {
            "keywords": ["대선", "총선", "선거", "election", "정권교체", "대통령"],
            "sectors": ["construction", "defense", "policy"],
        },
        "usd_strong": {
            "keywords": ["달러 강세", "원화 약세", "환율 급등", "1500원", "달러 인덱스"],
            "sectors": ["export", "energy"],
        },
        "usd_weak": {
            "keywords": ["달러 약세", "원화 강세", "환율 하락", "달러 하락"],
            "sectors": ["import", "travel"],
        },
        "supply_chain": {
            "keywords": ["공급망", "supply chain", "반도체 부족", "물류 대란", "항만 폐쇄", "수에즈"],
            "sectors": ["shipping", "commodity", "logistics"],
        },
    }

    # 섹터별 수혜 종목 DB
    SECTOR_STOCKS_KR = {
        "defense":      [{"code": "012450", "name": "한화에어로스페이스"}, {"code": "079550", "name": "LIG넥스원"}, {"code": "064350", "name": "현대로템"}],
        "gold":         [{"code": "132030", "name": "KODEX 골드선물(H)"}],
        "energy":       [{"code": "010950", "name": "S-Oil"}, {"code": "096770", "name": "SK이노베이션"}],
        "shipping":     [{"code": "011200", "name": "HMM"}],
        "pharma":       [{"code": "207940", "name": "삼성바이오로직스"}, {"code": "068270", "name": "셀트리온"}],
        "biotech":      [{"code": "207940", "name": "삼성바이오로직스"}],
        "remote_work":  [{"code": "035420", "name": "NAVER"}, {"code": "035720", "name": "카카오"}],
        "delivery":     [{"code": "035420", "name": "NAVER"}],
        "bank":         [{"code": "105560", "name": "KB금융"}, {"code": "055550", "name": "신한지주"}],
        "commodity":    [{"code": "285130", "name": "KODEX 철강"}],
        "growth_tech":  [{"code": "005930", "name": "삼성전자"}, {"code": "000660", "name": "SK하이닉스"}, {"code": "035420", "name": "NAVER"}],
        "ai_semi":      [{"code": "000660", "name": "SK하이닉스"}, {"code": "005930", "name": "삼성전자"}],
        "cloud":        [{"code": "035420", "name": "NAVER"}, {"code": "035720", "name": "카카오"}],
        "construction": [{"code": "000720", "name": "현대건설"}, {"code": "047040", "name": "대우건설"}],
        "essential":    [{"code": "051900", "name": "LG생활건강"}, {"code": "004370", "name": "농심"}],
        "utility":      [{"code": "015760", "name": "한국전력"}],
        "bond_etf":     [{"code": "148070", "name": "KOSEF 국고채10년"}],
        "reits":        [{"code": "329200", "name": "리츠"}],
        "export":       [{"code": "005930", "name": "삼성전자"}, {"code": "005380", "name": "현대차"}],
        "import":       [{"code": "004370", "name": "농심"}],
        "travel":       [{"code": "039130", "name": "하나투어"}],
        "insurance":    [{"code": "000810", "name": "삼성화재"}],
        "logistics":    [{"code": "011200", "name": "HMM"}],
        "policy":       [{"code": "000720", "name": "현대건설"}],
    }
    SECTOR_STOCKS_US = {
        "defense":      [{"ticker": "LMT", "name": "Lockheed Martin", "exchange": "NYSE"}, {"ticker": "RTX", "name": "RTX Corp", "exchange": "NYSE"}],
        "gold":         [{"ticker": "GLD", "name": "SPDR Gold", "exchange": "NYSE"}],
        "energy":       [{"ticker": "XOM", "name": "ExxonMobil", "exchange": "NYSE"}, {"ticker": "CVX", "name": "Chevron", "exchange": "NYSE"}],
        "shipping":     [{"ticker": "ZIM", "name": "ZIM Shipping", "exchange": "NYSE"}],
        "pharma":       [{"ticker": "PFE", "name": "Pfizer", "exchange": "NYSE"}, {"ticker": "JNJ", "name": "J&J", "exchange": "NYSE"}],
        "biotech":      [{"ticker": "MRNA", "name": "Moderna", "exchange": "NASD"}],
        "remote_work":  [{"ticker": "ZM", "name": "Zoom", "exchange": "NASD"}],
        "bank":         [{"ticker": "JPM", "name": "JPMorgan", "exchange": "NYSE"}],
        "growth_tech":  [{"ticker": "AAPL", "name": "Apple", "exchange": "NASD"}, {"ticker": "MSFT", "name": "Microsoft", "exchange": "NASD"}],
        "ai_semi":      [{"ticker": "NVDA", "name": "NVIDIA", "exchange": "NASD"}, {"ticker": "AMD", "name": "AMD", "exchange": "NASD"}],
        "cloud":        [{"ticker": "AMZN", "name": "Amazon", "exchange": "NASD"}, {"ticker": "MSFT", "name": "Microsoft", "exchange": "NASD"}],
        "essential":    [{"ticker": "PG", "name": "P&G", "exchange": "NYSE"}, {"ticker": "KO", "name": "Coca-Cola", "exchange": "NYSE"}],
        "utility":      [{"ticker": "NEE", "name": "NextEra Energy", "exchange": "NYSE"}],
        "bond_etf":     [{"ticker": "TLT", "name": "iShares 20Y Treasury", "exchange": "NASD"}],
        "reits":        [{"ticker": "O", "name": "Realty Income", "exchange": "NYSE"}],
        "commodity":    [{"ticker": "DBC", "name": "Commodity Index", "exchange": "NYSE"}],
        "export":       [{"ticker": "CAT", "name": "Caterpillar", "exchange": "NYSE"}],
        "construction": [{"ticker": "DHI", "name": "D.R. Horton", "exchange": "NYSE"}],
    }

    # ── 종목 → 섹터 태깅 (SECTOR_STOCKS 역인덱스 + 이름 키워드 폴백) ──
    # 이벤트 로테이션: 활성 이벤트 섹터에 속한 종목을 선별에서 부스트하는 데 사용.
    SECTOR_NAME_KEYWORDS = {
        "defense":      ["방산", "항공우주", "디펜스", "방위", "defense", "aerospace"],
        "ai_semi":      ["반도체", "디램", "dram", "hbm", "파운드리", "semi", "gpu"],
        "growth_tech":  ["it", "소프트", "인터넷", "플랫폼", "tech", "software"],
        "bank":         ["금융", "은행", "증권", "지주", "보험", "bank", "financial", "capital"],
        "pharma":       ["바이오", "제약", "헬스", "pharma", "bio", "health", "therapeutics"],
        "biotech":      ["바이오", "biotech", "genomics"],
        "energy":       ["에너지", "정유", "석유", "가스", "energy", "oil", "petroleum"],
        "construction": ["건설", "건축", "엔지니어링", "construction", "homes"],
        "export":       ["자동차", "조선", "기계", "auto", "motor", "industrial"],
        "commodity":    ["철강", "금속", "소재", "steel", "metal", "materials"],
        "shipping":     ["해운", "물류", "shipping", "logistics", "freight"],
        "essential":    ["식품", "생활", "소비재", "유통", "food", "consumer", "staples"],
        "utility":      ["전력", "가스공사", "유틸", "utility", "electric"],
        "reits":        ["리츠", "reit", "부동산", "realty", "estate"],
    }

    @classmethod
    def _sector_index(cls) -> dict:
        """SECTOR_STOCKS_{KR,US} 를 역인덱싱 → {code/ticker: set(sectors)} (1회 캐시)."""
        if getattr(cls, "_SECTOR_IDX", None) is None:
            idx: dict = {}
            for sec, lst in cls.SECTOR_STOCKS_KR.items():
                for s in lst:
                    idx.setdefault(s["code"], set()).add(sec)
            for sec, lst in cls.SECTOR_STOCKS_US.items():
                for s in lst:
                    idx.setdefault(s["ticker"], set()).add(sec)
            cls._SECTOR_IDX = idx
        return cls._SECTOR_IDX

    @classmethod
    def sectors_of(cls, code: str, name: str = "") -> set:
        """종목의 섹터 집합 — 큐레이션 역인덱스 우선, 없으면 이름 키워드 폴백."""
        secs = set(cls._sector_index().get(code, set()))
        n = (name or "").lower()
        if n:
            for sec, kws in cls.SECTOR_NAME_KEYWORDS.items():
                if any(k in n for k in kws):
                    secs.add(sec)
        return secs

    def check_event_beneficiary(self, news_text: str = "", market_condition: str = "peace") -> dict | None:
        """뉴스에서 이벤트 감지 → 수혜 섹터/종목 자동 편입.

        전쟁, 팬데믹, 인플레, 금리인하, 경기침체, AI붐, 기후재해,
        선거, 환율, 공급망 등 12가지 이벤트 유형 커버.
        """
        if not news_text and market_condition == "peace":
            return None

        text_lower = news_text.lower()
        detected_events = []
        all_sectors = set()
        all_matched = []

        for event_type, event_cfg in self.EVENT_MAP.items():
            matched = [k for k in event_cfg["keywords"] if k in text_lower]
            if matched:
                detected_events.append(event_type)
                all_sectors.update(event_cfg["sectors"])
                all_matched.extend(matched)

        # 위기 상황이면 기본 방어 섹터 추가
        if market_condition in ("crisis", "war") and not detected_events:
            detected_events.append("recession")
            all_sectors.update(["essential", "gold", "utility", "bond_etf"])

        if not detected_events:
            return None

        # 종목 수집 (중복 제거)
        kr_stocks = []
        us_stocks = []
        seen_kr = set()
        seen_us = set()
        for sector in all_sectors:
            for s in self.SECTOR_STOCKS_KR.get(sector, []):
                if s["code"] not in seen_kr:
                    kr_stocks.append(s)
                    seen_kr.add(s["code"])
            for s in self.SECTOR_STOCKS_US.get(sector, []):
                if s["ticker"] not in seen_us:
                    us_stocks.append(s)
                    seen_us.add(s["ticker"])

        event_names = {
            "war": "전쟁", "middle_east": "중동 위기", "pandemic": "팬데믹",
            "inflation": "인플레이션", "rate_cut": "금리 인하", "recession": "경기침체",
            "tech_boom": "기술 붐", "climate": "기후 재해", "election": "선거",
            "usd_strong": "달러 강세", "usd_weak": "달러 약세", "supply_chain": "공급망 위기",
        }
        event_labels = [event_names.get(e, e) for e in detected_events]

        result = {
            "action": "event_buy",
            "events": detected_events,
            "event_labels": event_labels,
            "sectors": list(all_sectors),
            "matched_keywords": list(set(all_matched))[:10],
            "kr_stocks": kr_stocks[:8],
            "us_stocks": us_stocks[:6],
            "reason": f"이벤트 감지: {', '.join(event_labels)} → 수혜 {len(all_sectors)}섹터",
        }
        logger.info("이벤트 수혜: %s (키워드: %s)", result["reason"], ", ".join(all_matched[:5]))
        return result

    # ══════════════════════════════════════
    # 8. 과매도 반등 사냥
    # ══════════════════════════════════════

    @staticmethod
    def check_quick_loss_exit(df, profit_rate: float = 0.0) -> dict | None:
        """빠른 손절 트리거: 매수 후 단기 약세 신호 누적 시 즉시 컷.

        조건 (3개 이상 + 손실 -4% 이상):
          1. RSI < 35 + 추가 하락 중 (RSI 어제 대비 -3 이상 감소)
          2. 데드크로스 시작 (5MA가 막 20MA 아래로 내려감)
          3. 거래량 평균 1.5배+ 음봉 (매도세 가속)
          4. 볼린저 하단 이탈

        slow_bleed보다 빠름 (5일 누적 안 기다림). 완화: -2% 임계가
        BB -2.1% 단기 충격(매수 당일 RSI 56→34 + 거래량 43배)에 즉시 전량 손절 발동,
        반등 가능성 없이 -2,880원 확정. 임계 -2→-4%, 신호 2→3개, 전량→절반매도로 완화.
        """
        import pandas as pd
        if df is None or len(df) < 21:
            return None
        if profit_rate > -0.04:  # 손실 -4% 미만이면 트리거 안 함: -2 → -4)
            return None

        close = df["close"]
        volume = df["volume"]
        curr = df.iloc[-1]
        signals = []

        # 1. RSI 추가 하락
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi_series = 100 - (100 / (1 + rs))
        rsi_curr = rsi_series.iloc[-1]
        rsi_prev = rsi_series.iloc[-2] if len(rsi_series) >= 2 else None
        #: 극단 과매도(RSI<25)에서는 빠른손절 금지 — 캐피츌레이션 바닥 투매 방지.
        # NetApp을 RSI 13 / -4.7%에 전량컷해 -99k 확정한 사례가 대표적(바닥에서 던짐).
        # 진짜 붕괴는 -15% 하드스톱/트레일링이 잡고, 여기선 바닥 반등 여지를 남긴다.
        if pd.notna(rsi_curr) and rsi_curr < 25:
            return None
        if pd.notna(rsi_curr) and rsi_curr < 35 and rsi_prev is not None:
            if pd.notna(rsi_prev) and (rsi_prev - rsi_curr) >= 3:
                signals.append(f"RSI 급락 ({rsi_prev:.0f}→{rsi_curr:.0f}, 추세 약화)")

        # 2. 데드크로스 시작
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        if len(ma5) >= 2 and len(ma20) >= 2:
            crossed_down = (ma5.iloc[-2] >= ma20.iloc[-2]) and (ma5.iloc[-1] < ma20.iloc[-1])
            if crossed_down:
                signals.append("데드크로스 발생 (5MA < 20MA)")

        # 3. 거래량 급증 음봉
        vol_avg = volume.iloc[-21:-1].mean()
        is_bear_candle = curr["close"] < curr["open"]
        if (vol_avg > 0 and curr["volume"] > vol_avg * 1.5 and is_bear_candle):
            signals.append(f"거래량 {curr['volume'] / vol_avg:.1f}배 음봉 (매도세 가속)")

        # 4. 볼린저 하단 이탈
        bb_ma = close.rolling(20).mean().iloc[-1]
        bb_std = close.rolling(20).std().iloc[-1]
        if pd.notna(bb_ma) and pd.notna(bb_std):
            bb_lower = bb_ma - 2 * bb_std
            if curr["close"] <= bb_lower:
                signals.append("볼린저 하단 이탈")

        if len(signals) < 3:  #: 2개 → 3개로 강화
            return None

        return {
            "action": "quick_loss_exit",
            "signals": signals,
            "sell_ratio": 0.5,  #: 전량(1.0) → 절반(0.5)으로 완화. 추세 회복 여지 남김
            "reason": (f"빠른 손절 (수익 {profit_rate * 100:+.1f}%, "
                       f"{', '.join(signals)})"),
        }

    @staticmethod
    def check_overbought_exit(df, profit_rate: float = 0.0,
                              rsi_min: float = 80, profit_min: float = 0.03) -> dict | None:
        """RSI 과매수 + 모멘텀 둔화 = 단기 익절 신호 (보유 종목 빠른 부분 매도).

        조건 (2개 이상):
          1. RSI ≥ rsi_min (default 80)
          2. 볼린저 상단 터치 또는 이탈
          3. 거래량 평균 이하 (매수세 약화)

        수익 profit_min+ 일 때만 트리거 (default 3%, 작은 익절 방지).
        adaptive 상태에 따라 rsi_min/profit_min 동적 전달.

        Returns:
            {"action": "rsi_exit", "signals": [...], "sell_ratio": 0.3}
        """
        import pandas as pd
        if df is None or len(df) < 21:
            return None
        if profit_rate < profit_min:
            return None

        close = df["close"]
        volume = df["volume"]
        curr = df.iloc[-1]
        signals = []

        # 1. RSI ≥ rsi_min (adaptive)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = (100 - (100 / (1 + rs))).iloc[-1]
        if pd.notna(rsi) and rsi >= rsi_min:
            signals.append(f"RSI {rsi:.0f} 과매수")
        else:
            return None  # rsi_min 미만이면 즉시 종료

        # 2. 볼린저 상단 터치/이탈
        bb_ma = close.rolling(20).mean().iloc[-1]
        bb_std = close.rolling(20).std().iloc[-1]
        if pd.notna(bb_ma) and pd.notna(bb_std):
            bb_upper = bb_ma + 2 * bb_std
            if curr["close"] >= bb_upper:
                signals.append("볼린저 상단 터치")

        # 3. 거래량 둔화 (매수세 약화)
        vol_avg = volume.iloc[-21:-1].mean()
        if vol_avg > 0 and curr["volume"] < vol_avg * 0.8:
            signals.append(f"거래량 둔화 ({curr['volume'] / vol_avg:.1f}배)")

        if len(signals) < 2:
            return None

        return {
            "action": "rsi_exit",
            "signals": signals,
            "sell_ratio": 0.3,  # 부분 익절 30%만 (50→30, 나머지 70%는 trailing으로 추세 추적)
            "reason": (f"RSI 과매수 익절 (수익 +{profit_rate * 100:.1f}%, "
                       f"{', '.join(signals)})"),
        }

    @staticmethod
    def check_oversold_bounce(df, code: str = "", name: str = "") -> dict | None:
        """RSI 극단 + 거래량 폭증 = 바닥 신호, 선제 매수.

        조건 (3개 이상 충족 시 발동):
          1. RSI ≤ 20 (극단적 과매도)
          2. 거래량 평균 3배 이상 (투매 → 바닥 형성)
          3. 하단 볼린저밴드 이탈
          4. 장대 음봉 후 양봉 출현 (해머/도지)

        Returns:
            {"action": "bounce_buy", "signals": [...], "confidence": ...}
        """
        import pandas as pd

        if df is None or len(df) < 21:
            return None

        close = df["close"]
        volume = df["volume"]
        curr = df.iloc[-1]
        prev = df.iloc[-2]

        signals = []

        # 1. RSI ≤ 20
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = (100 - (100 / (1 + rs))).iloc[-1]
        if pd.notna(rsi) and rsi <= 20:
            signals.append(f"RSI {rsi:.0f} 극단적 과매도")
        elif pd.notna(rsi) and rsi <= 30:
            signals.append(f"RSI {rsi:.0f} 과매도")

        # 2. 거래량 3배 이상
        vol_avg = volume.iloc[-21:-1].mean()
        if vol_avg > 0 and curr["volume"] > vol_avg * 3:
            signals.append(f"거래량 {curr['volume'] / vol_avg:.1f}배 (투매 후 바닥)")

        # 3. 볼린저 하단 이탈
        bb_ma = close.rolling(20).mean().iloc[-1]
        bb_std = close.rolling(20).std().iloc[-1]
        if pd.notna(bb_ma) and pd.notna(bb_std):
            bb_lower = bb_ma - 2 * bb_std
            if curr["close"] <= bb_lower:
                signals.append("볼린저 하단 이탈")

        # 4. 장대 음봉 후 양봉 (반전 캔들)
        prev_body = prev["close"] - prev["open"]
        curr_body = curr["close"] - curr["open"]
        if prev_body < 0 and curr_body > 0:
            # 전일 음봉 + 오늘 양봉
            if abs(prev_body) > abs(curr["close"] - curr["open"]) * 0.5:
                signals.append("장대 음봉 후 양봉 (반전 캔들)")

        if len(signals) < 2:
            return None

        confidence = min(0.9, 0.3 + len(signals) * 0.15)

        result = {
            "action": "bounce_buy",
            "code": code,
            "name": name,
            "signals": signals,
            "signal_count": len(signals),
            "confidence": confidence,
            "reason": f"과매도 반등 신호 {len(signals)}개: {', '.join(signals)}",
        }
        logger.info("반등 사냥: %s %s", name, result["reason"])
        return result
