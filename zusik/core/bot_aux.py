from __future__ import annotations

import logging
import time
from datetime import datetime

from zusik import paths


from zusik.strategies.adaptive import AdaptiveStrategy
from zusik.strategies.auto_hybrid import AutoHybridStrategy


logger = logging.getLogger(__name__)


class AuxMarketsMixin:
    """AuxMarketsMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def run_crypto(self):
        """암호화폐 매매 (KR/US 마감 시간 + 주말에 활성)."""
        if not self.crypto.enabled or not self.crypto_tickers:
            return

        # KR장, US장 둘 다 열려있으면 주식에 집중
        kr_open = self.client.is_market_open()
        us_open = self.client.is_us_market_open()
        if kr_open or us_open:
            return

        if not self._check_risks_before_trading():
            return

        logger.info("===== 암호화폐 [%s] =====", datetime.now().strftime("%H:%M"))
        for ticker in self.crypto_tickers:
            try:
                self._execute_crypto(ticker)
                time.sleep(0.5)
            except Exception as e:
                logger.exception("암호화폐 %s 오류", ticker)
                msg = self._format_error_alert("암호화폐", ticker, e)
                if self.discord and msg:
                    self.discord.notify_error(msg)

    def _execute_crypto(self, ticker: str):
        """암호화폐 단일 종목 분석+매매."""
        df = self.crypto.get_ohlcv(ticker, interval="minute60")
        if df is None or df.empty:
            return

        # 로컬 퀀트 체크 (비용 $0)
        local = self.cost.local_quick_check(df)
        if not local["action_needed"]:
            return

        price_data = self.crypto.get_current_price(ticker)
        price = price_data["price"]
        name = ticker.replace("KRW-", "")

        # 캐시 체크
        check = self.cost.should_analyze(ticker, price)
        if not check["should_call"]:
            return

        logger.info("─── %s $%s ───", name, f"{price:,.0f}")

        if self.use_claude:
            self.strategy.set_stock(ticker, name)
            self.strategy.set_context(portfolio_info="암호화폐 모드")

        signal = self.strategy.analyze(df)
        logger.info("%s %s원 → %s", name, f"{price:,.0f}", signal.upper())

        if signal in ("buy", "long_term_buy"):
            if getattr(self, "_buy_blocked_low_cash", False):
                logger.info("암호화폐 매수 차단 (현금 부족): %s", name)
                return
            krw = self.crypto.get_balance("KRW")
            # 오염된 설정이 과대 매수를 만들지 못하게 비중을 [0,1] 로 clamp (fail-safe).
            try:
                ratio = min(max(float(self.crypto_invest_ratio), 0.0), 1.0)
            except (TypeError, ValueError):
                ratio = 0.0
            invest = int(krw * ratio)
            if invest >= 5000:
                self.crypto.buy_market(ticker, invest)
                if self.discord:
                    self.discord.notify_trade("buy", name, ticker, 0, int(price), reason="암호화폐")
        elif signal == "sell":
            coin = ticker.split("-")[1]
            vol = self.crypto.get_balance(coin)
            if vol > 0:
                self.crypto.sell_market(ticker, vol)
                if self.discord:
                    self.discord.notify_trade("sell", name, ticker, 0, int(price), reason="암호화폐")

    def run_cross_signals(self):
        """US 장 마감 후 크로스시그널 → data/cross_signals_kr.json 저장 (익일 KR 매매 반영).

        美 종목 급등/급락을 연동 KR 종목 단위 편향으로 정규화해 저장한다. 소비는
        `_ai_signal_for`(매수 게이트·사이징)가 담당 — 더이상 로그로만 흘려보내지 않는다.
        """
        if not self.us_stocks:
            return
        # 약세(caution) 우선순위가 강세(buy)보다 높다 — 보수적 dedup.
        # 같은 KR 코드에 美 종목 둘이 엇갈리면(예: 035420 = MSFT+AMZN) 강세가 약세를
        # 덮어 위험 신호를 잃지 않도록. 같은 등급이면 boost 큰 쪽.
        _rank = {"caution": 2, "buy": 1}
        codes: dict = {}
        for stock in self.us_stocks:
            try:
                ticker = stock["ticker"]
                exchange = stock.get("exchange", "NASD")
                info = self.client.get_us_current_price(ticker, exchange)
                change = info["change_rate"] / 100 if abs(info["change_rate"]) > 1 else info["change_rate"]
                # persist=False — 익일 매매가 읽는 cross_signals_kr.json만 쓰고,
                # smart_signals.json의 (아무도 안 읽는) cross_signals 히스토리 전체 재기록은 생략.
                cross = self.signals.check_cross_signal(ticker, change, persist=False)
                for s in cross:
                    code = s.get("kr_code")
                    if not code:
                        continue
                    bias = "buy" if s.get("signal") == "buy" else "caution"
                    boost = float(s.get("confidence_boost", 0) or 0)
                    prev = codes.get(code)
                    if (prev is None or _rank.get(bias, 0) > _rank.get(prev["bias"], 0)
                            or (_rank.get(bias, 0) == _rank.get(prev["bias"], 0)
                                and boost > prev.get("boost", 0))):
                        codes[code] = {"bias": bias, "boost": boost, "reason": s.get("reason", "")}
                    if bias == "buy":
                        logger.info("크로스시그널: %s %+.1f%% → KR %s 매수 참고",
                                    ticker, change * 100, code)
            except Exception:
                pass
        if not codes:
            return
        try:
            paths.write_json_atomic(paths.data_path("cross_signals_kr.json"),
                                    {"ts": time.time(),
                                     "date": datetime.now().strftime("%Y-%m-%d"),
                                     "codes": codes})
            logger.info("크로스시그널 %d종목 저장 → 익일 KR 매수 게이트/사이징 반영", len(codes))
        except Exception:
            logger.debug("크로스시그널 저장 실패", exc_info=True)

    def _run_arena_cycle(self):
        """아레나 스캔 + 가상 포지션 관리 (30분마다 비동기).

        모든 가격은 KRW로 환산하여 사용 (가상자본 1,000,000 KRW 기준).
        US 종목은 KIS 실시간 환율 적용.
        """
        now = datetime.now()
        #: 무의미 24/7 가동 방지. 장 마감 중엔 가격이 정체돼 가상매매가
        # 의미 없고 API만 낭비 → KR/US 둘 다 닫혔으면 스킵. config로 토글/주기 조절.
        arena_cfg = self.config.get("arena", {}) or {}
        if not arena_cfg.get("enabled", False):   # 기본 OFF (의사결정 미사용 — 낭비 차단)
            return
        try:
            if not (self.client.is_market_open() or self.client.is_us_market_open()):
                return
        except Exception:
            pass
        interval = int(arena_cfg.get("interval_minutes", 60)) * 60
        last = self._last_arena_scan
        if last and (now - last).total_seconds() < interval:
            return
        self._last_arena_scan = now

        import threading
        def _scan():
            try:
                universe = (self.kr_stocks or []) + (self.us_stocks or [])
                if not universe:
                    return

                fx = self.client.get_usd_krw_rate()

                def _fetch_df(code):
                    if code and code.isdigit():
                        return self.client.get_ohlcv(code, period=self.period)
                    us_info = next((s for s in (self.us_stocks or []) if s.get("ticker") == code), None)
                    exch = us_info.get("exchange", "NASD") if us_info else "NASD"
                    return self.client.get_us_ohlcv(code, exch, period=self.period)

                def _fetch_price_krw(code):
                    """가격을 KRW로 환산하여 반환 (US는 FX 적용)."""
                    try:
                        if code and code.isdigit():
                            return float(self.client.get_current_price(code)["price"])
                        us_info = next((s for s in (self.us_stocks or []) if s.get("ticker") == code), None)
                        exch = us_info.get("exchange", "NASD") if us_info else "NASD"
                        usd_price = self.client.get_us_current_price(code, exch)["price"]
                        return float(usd_price) * fx
                    except Exception:
                        return None

                normalized = []
                for s in universe:
                    code = s.get("code") or s.get("ticker")
                    if code:
                        normalized.append({"code": code, "name": s.get("name", code)})
                self.arena.scan_and_trade(
                    normalized, _fetch_df, _fetch_price_krw,
                    strategies=self._arena_strategies,
                )

                prices = {}
                for s in normalized:
                    p = _fetch_price_krw(s["code"])
                    if p is not None:
                        prices[s["code"]] = p
                self.arena.manage_positions(prices)
                # 의미 있는 산출 — 지금 어떤 전략이 실제로 이기고 있는지 표면화.
                leader = self.arena.get_overall_leader(prices)
                if leader and leader.get("total_trades", 0) > 0:
                    logger.info("아레나 사이클 완료 — 종목 %d개. 현재 1등: %s "
                                "(%+.1f%%, 승률 %.0f%%, %d거래)",
                                len(normalized), leader["agent"], leader["return_pct"],
                                leader["win_rate"], leader["total_trades"])
                    self._arena_leader = leader  # 운영 가시성/후속 활용용 캐시
                else:
                    logger.info("아레나 사이클 완료 — 종목 %d개 (아직 거래 없음)", len(normalized))
            except Exception:
                logger.exception("아레나 사이클 오류")

        threading.Thread(target=_scan, daemon=True).start()

    def _discover_pairs_async(self):
        """주 1회 페어 자동 발굴 — 종목 풀 OHLCV에서 강한 cointegration 페어 찾기.

        DEFAULT_PAIRS (도메인 지식)에 자동 발굴 페어 추가. 시장 변하면 정적
        페어가 죽고 새 페어가 생기므로 재발굴 필요.
        """
        if self._pair_trader is None:
            return
        import threading

        def _discover():
            try:
                from zusik.core.pair_trader import DEFAULT_PAIRS
                from concurrent.futures import ThreadPoolExecutor, as_completed
                # 종목 풀 OHLCV 병렬 fetch (자동 스크리닝과 같은 풀 활용)
                from zusik.analysis.auto_screener import KR_CANDIDATE_POOL, US_CANDIDATE_POOL

                def _fetch_kr(code, name):
                    try:
                        return code, self.client.get_ohlcv(code, period=self.period)
                    except Exception:
                        return code, None

                def _fetch_us(t, n, e):
                    try:
                        return t, self.client.get_us_ohlcv(t, e, period=self.period)
                    except Exception:
                        return t, None

                # KR/US 분리 발굴 (시장 간 페어는 의미 약함)
                stocks_kr = {}
                stocks_us = {}
                # 부담 줄이려 풀 일부만 (매수 가능 우량주 30종 제한)
                with ThreadPoolExecutor(max_workers=8) as ex:
                    futures = [ex.submit(_fetch_kr, c, n) for c, n in KR_CANDIDATE_POOL[:30]]
                    for f in as_completed(futures, timeout=60):
                        c, df = f.result()
                        if df is not None:
                            stocks_kr[c] = df
                with ThreadPoolExecutor(max_workers=8) as ex:
                    futures = [ex.submit(_fetch_us, t, n, e) for t, n, e in US_CANDIDATE_POOL[:30]]
                    for f in as_completed(futures, timeout=60):
                        t, df = f.result()
                        if df is not None:
                            stocks_us[t] = df

                discovered_kr = self._pair_trader.discover_pairs(
                    stocks_kr, min_corr=0.7, top_n=5,
                )
                discovered_us = self._pair_trader.discover_pairs(
                    stocks_us, min_corr=0.7, top_n=5,
                )
                # 시장 정보 보정 (discover_pairs는 코드만 보고 추론)
                discovered_kr = [(a, b, "KR", d) for a, b, _, d in discovered_kr]
                discovered_us = [(a, b, "US", d) for a, b, _, d in discovered_us]

                # 정적 페어 + 자동 페어 합치기 (중복 제거)
                seen = set()
                merged = []
                for pair in DEFAULT_PAIRS + discovered_kr + discovered_us:
                    key = tuple(sorted([pair[0], pair[1]]))
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(pair)
                self._pair_trader.pairs = merged
                logger.info("페어 자동 발굴: 정적 %d + 자동 %d = 총 %d 페어",
                            len(DEFAULT_PAIRS), len(discovered_kr) + len(discovered_us),
                            len(merged))
            except Exception:
                logger.exception("페어 자동 발굴 오류")

        threading.Thread(target=_discover, daemon=True).start()

    def _run_pair_trading_cycle(self):
        """페어 트레이딩 — 시장 방향 무관 수익. 30분 주기.

        z-score >= ±2 진입 신호 발견 시 저평가 종목 매수. dedup으로 일일 1회만.
        주 1회 자동 페어 발굴 — N×N 상관계수로 강한 페어 자동 갱신.
        """
        if self._pair_trader is None:
            return
        now = datetime.now()
        last = self._last_pair_scan
        if last and (now - last).total_seconds() < 1800:
            return
        self._last_pair_scan = now

        # 주 1회 페어 자동 발굴 — 정적 페어 + 자동 페어 합쳐 사용
        last_disc = self._last_pair_discovery
        if last_disc is None or (now - last_disc).days >= 7:
            self._last_pair_discovery = now
            self._discover_pairs_async()

        import threading

        def _scan():
            try:
                def _fetch(code: str, market: str):
                    if market == "KR":
                        return self.client.get_ohlcv(code, period=self.period)
                    us_info = next((s for s in (self.us_stocks or []) if s.get("ticker") == code), None)
                    exch = us_info.get("exchange", "NASD") if us_info else "NASD"
                    return self.client.get_us_ohlcv(code, exch, period=self.period)

                signals = self._pair_trader.scan(_fetch)
                for s in signals:
                    res = s.get("result", {})
                    if not res.get("valid"):
                        continue
                    sig = res.get("signal", "hold")
                    if sig not in ("buy_a", "buy_b"):
                        continue
                    target_code = s["code_a"] if sig == "buy_a" else s["code_b"]
                    pair_key = f"{s['code_a']}_{s['code_b']}_{now.strftime('%Y-%m-%d')}"
                    if pair_key in self._pair_signals_today:
                        continue
                    self._pair_signals_today.add(pair_key)

                    # 이미 보유 중이면 스킵
                    if self.positions.has_position(target_code):
                        logger.debug("페어 매수 스킵 (이미 보유): %s", target_code)
                        continue

                    # 매수 시도 (저평가 → 매수). 신뢰도 +0.65 (z-score 통계적 의미)
                    name = self._get_name(target_code) or target_code
                    logger.info("페어 매수 신호: %s↔%s, %s 저평가 (z=%.2f, corr=%.2f, %s)",
                                s["code_a"], s["code_b"], target_code,
                                res["z"], res["correlation"], s["desc"])
                    if self.discord:
                        try:
                            self.discord.notify_info(
                                f"페어 트레이딩 신호\n"
                                f"  {s['desc']}\n"
                                f"  {s['code_a']} vs {s['code_b']}\n"
                                f"  z-score: {res['z']:+.2f} (|z|≥2 진입)\n"
                                f"  매수 후보: **{target_code}** (저평가)"
                            )
                        except Exception:
                            pass
                    # 매수 실행 — 1주 가격이 가용 현금 초과면 스킵
                    df = _fetch(target_code, s["market"])
                    if df is None or df.empty:
                        continue
                    price = float(df["close"].iloc[-1])
                    if s["market"] == "KR":
                        cash = self.client.get_balance().get("cash", 0)
                        if price > cash:
                            logger.info("페어 매수 스킵 %s: 1주 %s원 > 현금 %s원",
                                        target_code, f"{int(price):,}", f"{cash:,}")
                            continue
                        self._handle_buy(target_code, name, int(price), df=df)
                    else:
                        us_info = next((u for u in (self.us_stocks or []) if u.get("ticker") == target_code), None)
                        exch = us_info.get("exchange", "NASD") if us_info else "NASD"
                        try:
                            cash_usd = self.client.get_us_balance().get("cash_usd", 0.0)
                            if price > cash_usd:
                                logger.info("페어 매수 스킵 %s: 1주 $%.2f > 달러 $%.2f",
                                            target_code, price, cash_usd)
                                continue
                        except Exception:
                            pass
                        self._handle_us_buy(target_code, name, price, exch, df=df)
            except Exception:
                logger.exception("페어 트레이딩 사이클 오류")

        threading.Thread(target=_scan, daemon=True).start()

    def _run_global_backtest(self):
        """종목 풀 전체 OHLCV로 다종목 글로벌 백테스트 (30분 주기, 비동기).

        12 전략 × N 종목 시뮬으로 통계적으로 의미 있는 1등 전략 발굴.
        결과는 AdaptiveStrategy._global_best에 캐시되어 모든 종목 분석에 일괄 적용.
        """
        now = datetime.now()
        last = self._last_global_backtest
        if last and (now - last).total_seconds() < 900:  # 15분 cooldown (기존 30분에서 단축)
            return
        self._last_global_backtest = now

        # AutoHybridStrategy의 내부 adaptive 또는 직접 AdaptiveStrategy 인스턴스 찾기
        adaptive = None
        if isinstance(self.strategy, AdaptiveStrategy):
            adaptive = self.strategy
        elif isinstance(self.strategy, AutoHybridStrategy):
            #: auto_hybrid는 `_adaptive`로 저장하는데 여기선 `adaptive`를
            # 찾아 항상 None→early return → 글로벌 백테스트(250봉 모델선택)가 한 번도 안 돌고
            # 종목별 30봉 fallback(-1.000 노이즈)만 쓰였다. 실제 속성명으로 수정.
            adaptive = getattr(self.strategy, "_adaptive", None) or getattr(self.strategy, "adaptive", None)
        if adaptive is None:
            return

        import threading

        def _backtest():
            try:
                universe = (self.kr_stocks or []) + (self.us_stocks or [])
                if not universe:
                    return

                # 종목별 OHLCV 병렬 fetch
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _fetch(stock):
                    code = stock.get("code") or stock.get("ticker")
                    try:
                        if code and code.isdigit():
                            #: get_ohlcv(~30봉) → get_daily_long(250봉). 모델선택
                            # 백테스트가 1년 데이터를 봐서 통계적으로 의미 있는 전략을 고른다.
                            df = self.client.get_daily_long(code, days=250)
                        else:
                            exch = stock.get("exchange", "NASD")
                            #: US도 100봉→250봉(get_us_daily_long, BYMD 청크). KR과 동일하게
                            # 모델선택 백테스트가 1년 데이터로 통계적 의미를 갖는다.
                            df = self.client.get_us_daily_long(code, exch, days=250)
                        if df is not None and not df.empty:
                            return code, df
                    except Exception as e:
                        logger.debug("글로벌 백테스트 OHLCV 실패 %s: %s", code, e)
                    return code, None

                stocks_dfs = {}
                with ThreadPoolExecutor(max_workers=8) as ex:
                    futures = {ex.submit(_fetch, s): s for s in universe}
                    for fut in as_completed(futures, timeout=60):
                        try:
                            code, df = fut.result()
                            if df is not None:
                                stocks_dfs[code] = df
                        except Exception:
                            pass

                logger.info("글로벌 백테스트 시작: 종목 %d개 OHLCV 수집 완료", len(stocks_dfs))

                # Vortex 가속 활용: 다종목 RSI 동시 계산 (사전 캐시)
                if self._accel is not None and self._accel.is_available() and stocks_dfs:
                    try:
                        import numpy as _np
                        codes = list(stocks_dfs.keys())
                        # 동일 길이로 맞춤 (최단봉 기준 자르기)
                        min_len = min(len(stocks_dfs[c]) for c in codes)
                        if min_len >= 30:
                            mat = _np.stack(
                                [stocks_dfs[c]["close"].iloc[-min_len:].astype(_np.float32).values
                                 for c in codes]
                            )
                            rsi_mat = self._accel.compute_rsi_batch(mat, period=14)
                            logger.info("Vortex RSI 가속: %d종 × %d봉 (%s)",
                                        len(codes), min_len, self._accel.device_info)
                            # 결과를 글로벌 캐시 (단기 시그널이 재사용 가능)
                            self._rsi_cache = {c: rsi_mat[i] for i, c in enumerate(codes)}
                    except Exception as e:
                        logger.debug("RSI 가속 실패: %s", e)

                adaptive.select_best_strategy_from_pool(stocks_dfs)
            except Exception:
                logger.exception("글로벌 백테스트 오류")

        threading.Thread(target=_backtest, daemon=True).start()

