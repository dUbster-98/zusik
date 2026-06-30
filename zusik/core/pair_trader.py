from __future__ import annotations
"""페어 트레이딩 — 시장 방향 무관 수익 (variance 시장 알파).

원리:
  두 상관 높은 종목 (A, B) 가격 비율의 z-score가 평균에서 멀어지면 회귀에 베팅.
  - z-score = (현재 ratio - 60일 평균) / 60일 std
  - z >= +2: A가 B 대비 비싸짐 → B 매수 (저평가 진입)
  - z <= -2: B가 A 대비 비싸짐 → A 매수
  - |z| < 0.5: 청산 또는 회피

시드 10만원 환경 → long-only 변형:
  진입 시 두 종목 모두 매수 X, 한 쪽(저평가)만 매수.
  청산은 단순 z-score 회귀 + 시간 한도.

페어 후보 (도메인 지식):
  - T ↔ VZ          (미국 통신, 강한 cointegration)
  - 102110 ↔ 069500 (KOSPI 200 ETF — 운용보수 차 spread)
  - 360750 ↔ 133690 (S&P 500 vs Nasdaq 100, 미국 인덱스 페어)
  - KO ↔ PEP        (음료 디펜시브, 추가 시 활용)

향후 Vortex 활용:
  - N×N 종목 간 공분산 매트릭스 자동 계산 → 새 페어 발굴
  - 매일 모든 쌍 ADF/cointegration 검정
  - 현재는 정적 정의로 시작, 안정성 검증 후 동적 발굴 추가
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# 정적 페어 정의 — (code_a, code_b, 시장, 설명)
DEFAULT_PAIRS = [
    # 시드 10만원 호환 (1주 가격 ≤ ~6만원). 두 종목 모두 매수 가능해야 의미 있음.
    ("T", "VZ", "US", "미국 통신 (AT&T $20 vs Verizon $40)"),
    ("BAC", "F", "US", "미국 가치주 (BoA $40 vs Ford $11)"),
    ("INTC", "CSCO", "US", "기술주 정체기 (Intel $30 vs Cisco $48)"),
    ("102110", "069500", "KR", "KOSPI 200 ETF (TIGER 33K vs KODEX 33K)"),
    ("360750", "133690", "KR", "미국 인덱스 (S&P 500 15K vs Nasdaq 100 15K)"),
    ("316140", "055550", "KR", "한국 은행 (우리금융 15K vs 신한지주 45K)"),
    # 제외 (시드 부족):
    # KO ↔ PEP (PEP 21만원), BAC ↔ JPM (JPM 29만원)
]


class PairTrader:
    """페어 트레이딩 시그널 생성기.

    Args:
        pairs: [(code_a, code_b, market, desc), ...]
        lookback: z-score 산출 기간 (기본 60봉)
        z_entry: 진입 임계 (기본 2.0)
        z_exit: 청산 임계 (기본 0.5)
        max_hold_days: 진입 후 최대 보유일 (시간 한도, mean-reversion 실패 시)
    """

    def __init__(self, pairs: Optional[list] = None,
                 lookback: int = 60,
                 z_entry: float = 2.0,
                 z_exit: float = 0.5,
                 max_hold_days: int = 14):
        self.pairs = pairs if pairs is not None else DEFAULT_PAIRS
        self.lookback = lookback
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.max_hold_days = max_hold_days

    @staticmethod
    def compute_zscore(prices_a: np.ndarray, prices_b: np.ndarray) -> float:
        """두 시계열 가격 비율의 마지막 z-score."""
        if len(prices_a) != len(prices_b) or len(prices_a) < 10:
            return 0.0
        b_safe = np.where(prices_b == 0, 1e-9, prices_b)
        ratio = prices_a / b_safe
        mean = ratio.mean()
        std = ratio.std()
        if std == 0 or np.isnan(std):
            return 0.0
        return float((ratio[-1] - mean) / std)

    @staticmethod
    def compute_correlation(prices_a: np.ndarray, prices_b: np.ndarray) -> float:
        """두 시계열의 단순 상관계수 (페어 후보 검증)."""
        if len(prices_a) != len(prices_b) or len(prices_a) < 10:
            return 0.0
        a = pd.Series(prices_a).pct_change().dropna()
        b = pd.Series(prices_b).pct_change().dropna()
        if len(a) < 5 or a.std() == 0 or b.std() == 0:
            return 0.0
        return float(a.corr(b))

    def evaluate_pair(self, df_a: pd.DataFrame, df_b: pd.DataFrame) -> dict:
        """단일 페어 평가.

        Returns:
            {
              "z": float,            # 현재 z-score
              "correlation": float,  # 가격 변화 상관
              "signal": "buy_a"|"buy_b"|"exit"|"hold",
              "valid": bool,         # 페어가 통계적으로 유효한지
            }
        """
        result = {"z": 0.0, "correlation": 0.0, "signal": "hold",
                  "valid": False, "reason": ""}
        if df_a is None or df_b is None:
            result["reason"] = "OHLCV 없음"
            return result
        if len(df_a) < self.lookback or len(df_b) < self.lookback:
            result["reason"] = f"데이터 부족 (lookback {self.lookback})"
            return result

        # 두 시계열을 같은 길이로 자르기 (인덱스 매칭은 호출자가)
        n = min(len(df_a), len(df_b), self.lookback)
        a = df_a["close"].astype(float).iloc[-n:].values
        b = df_b["close"].astype(float).iloc[-n:].values

        corr = self.compute_correlation(a, b)
        z = self.compute_zscore(a, b)

        result["correlation"] = corr
        result["z"] = z

        # 페어 유효성: 상관 0.5 이상이어야 의미 있는 페어
        if abs(corr) < 0.5:
            result["reason"] = f"상관 {corr:.2f} < 0.5 (페어 무효)"
            return result
        result["valid"] = True

        # 시그널
        if z >= self.z_entry:
            # A가 B 대비 고평가 → B가 저평가 → B 매수
            result["signal"] = "buy_b"
            result["reason"] = f"z {z:+.2f} ≥ +{self.z_entry} → B 저평가 매수"
        elif z <= -self.z_entry:
            result["signal"] = "buy_a"
            result["reason"] = f"z {z:+.2f} ≤ -{self.z_entry} → A 저평가 매수"
        elif abs(z) < self.z_exit:
            result["signal"] = "exit"
            result["reason"] = f"z {z:+.2f} 회귀 완료 → 청산"
        else:
            result["signal"] = "hold"
            result["reason"] = f"z {z:+.2f} 중립"

        return result

    def discover_pairs(self, stocks_dfs: dict, min_corr: float = 0.7,
                       top_n: int = 15) -> list[tuple]:
        """N×N 상관계수 매트릭스에서 자동 페어 발굴.

        모든 종목 쌍의 가격 변화 상관계수 계산 → 0.7+ 페어 선별.
        시장이 변하면 정적 페어가 죽고 새 페어가 생기므로 일일 재발굴.

        Args:
            stocks_dfs: {code: ohlcv_df}
            min_corr: 최소 상관계수 (기본 0.7)
            top_n: 상위 N 페어 반환
        Returns:
            [(code_a, code_b, market, "auto: corr=0.85 z=+1.2"), ...]
        """
        codes = sorted(stocks_dfs.keys())
        prices_dict = {}
        for c in codes:
            df = stocks_dfs[c]
            if df is None or len(df) < 30:
                continue
            close = df["close"].astype(float).values
            if len(close) < 30:
                continue
            prices_dict[c] = close[-self.lookback:] if len(close) >= self.lookback else close

        candidates = []
        codes_valid = list(prices_dict.keys())
        for i, a in enumerate(codes_valid):
            pa = prices_dict[a]
            for b in codes_valid[i + 1:]:
                pb = prices_dict[b]
                # 길이 통일
                n = min(len(pa), len(pb))
                if n < 30:
                    continue
                corr = self.compute_correlation(pa[-n:], pb[-n:])
                if abs(corr) < min_corr:
                    continue
                z = self.compute_zscore(pa[-n:], pb[-n:])
                # 시장 추론 (코드가 숫자면 KR, 영문이면 US)
                market = "KR" if a.isdigit() and b.isdigit() else "US"
                desc = f"auto: corr={corr:.2f} z={z:+.2f}"
                candidates.append((a, b, market, desc, corr, z))

        # 상관계수 절대값 큰 순으로 정렬 (강한 cointegration 우선)
        candidates.sort(key=lambda x: abs(x[4]), reverse=True)
        result = [(a, b, m, d) for a, b, m, d, _, _ in candidates[:top_n]]
        if result:
            logger.info("페어 자동 발굴: %d개 (min_corr=%.2f)", len(result), min_corr)
        return result

    def scan(self, ohlcv_fetcher) -> list[dict]:
        """모든 페어 스캔.

        Args:
            ohlcv_fetcher: callable(code, market) → DataFrame
        Returns:
            [{"pair": (a,b,market), "result": dict}, ...]
            signal in (buy_a/buy_b/exit/hold)
        """
        out = []
        for code_a, code_b, market, desc in self.pairs:
            try:
                df_a = ohlcv_fetcher(code_a, market)
                df_b = ohlcv_fetcher(code_b, market)
                res = self.evaluate_pair(df_a, df_b)
                res["desc"] = desc
                out.append({
                    "code_a": code_a, "code_b": code_b,
                    "market": market, "desc": desc,
                    "result": res,
                })
                if res["signal"] in ("buy_a", "buy_b", "exit") and res["valid"]:
                    logger.info("페어 신호 [%s↔%s]: %s (corr %.2f, %s)",
                                code_a, code_b, res["signal"], res["correlation"],
                                res["reason"])
            except Exception as e:
                logger.debug("페어 평가 실패 %s↔%s: %s", code_a, code_b, e)
        return out
