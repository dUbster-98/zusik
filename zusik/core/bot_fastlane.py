from __future__ import annotations

import logging
from datetime import datetime


logger = logging.getLogger(__name__)


class FastLaneMixin:
    """FastLaneMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def _prepare_open_buys(self):
        """장전(08:30~09:00): KR 후보 OHLCV 예열 + 로컬 매수 우선순위 산출 (하루 1회).

        목적: 개장(09:00) 즉시 '가장 좋은 후보부터' 매수하도록 준비. analysis_max_workers=1
        이라 run_kr이 후보를 순차 처리 → 후보 정렬 순서가 곧 매수 우선순위가 된다.
        Claude 미사용(로컬 momentum + 장전 동시호가 갭). 주문은 내지 않음(점수/예열만).
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_open_prep_date", "") == today:
            return
        self._open_prep_date = today
        try:
            from zusik.analysis.indicators import momentum_score
            scored: list[tuple[str, str, float]] = []
            for s in self.kr_stocks:
                code = s.get("code")
                if not code or self._is_inverse(code):
                    continue
                try:
                    df = self.client.get_ohlcv(code, period=self.period)  # 캐시 예열
                except Exception:
                    continue
                if df is None or len(df) < 20:
                    continue
                try:
                    mom = float(momentum_score(df))  # 0~1
                except Exception:
                    mom = 0.0
                gap = 0.0
                try:
                    cur = float(self.client.get_current_price(code).get("price", 0) or 0)
                    prev = float(df["close"].iloc[-1])
                    if cur > 0 and prev > 0:
                        gap = (cur - prev) / prev
                except Exception:
                    pass
                # 모멘텀 위주 + 장전 갭 완만 보정. 과도 갭상승(+) 추격은 제한(+0.03 cap),
                # 장전 급락(-)은 소폭 감점(반등 노림은 별도 로직이 처리).
                score = mom + max(-0.05, min(0.03, gap))
                scored.append((code, s.get("name", code), score))
            scored.sort(key=lambda x: x[2], reverse=True)
            # code → 순위(낮을수록 우선)
            self._open_priority = {c: i for i, (c, _n, _s) in enumerate(scored)}
            top = ", ".join(f"{n}" for _c, n, _s in scored[:5])
            logger.info("장전 분석 완료: KR %d종 매수 우선순위 산출 — 상위: %s", len(scored), top)
            if self.discord:
                try:
                    self.discord.notify_info(f"장전 분석 완료 — 개장 매수 우선순위 상위: {top}")
                except Exception:
                    pass
        except Exception:
            logger.debug("장전 매수 준비 실패", exc_info=True)

    def _in_opening_window(self, market: str) -> bool:
        """개장 직후 변동성 구간인지 — 실제 개장시각 + open_guard.delay_minutes 이내.

        시초 갭/스파이크 추격을 피하려 신규 매수를 잠시 보류. 실제 개장시각 기준이라
        장중 재시작에도 오작동 없음. is_market_open 으로 세션 게이트.
        """
        og = (self.config.get("open_guard", {}) or {})
        if not og.get("enabled", True):
            return False
        delay = float(og.get("delay_minutes", 5))
        if delay <= 0:
            return False
        try:
            is_open = self.client.is_market_open() if market == "KR" else self.client.is_us_market_open()
        except Exception:
            is_open = False
        if not is_open:
            return False
        h, m = self._OPEN_HOUR.get(market, (9, 0))
        now_dt = datetime.now()
        open_dt = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        elapsed_min = (now_dt - open_dt).total_seconds() / 60.0
        return 0 <= elapsed_min < delay

    def _on_market_open(self):
        """KR 개장(09:00) 직후 1회: 준비된 우선순위로 즉시 매수 사이클 실행.

        스케줄 run_once(5분 주기)를 기다리지 않고 개장 ~1분 내 매수를 시작한다.
        run_kr 자체에 동시 실행 가드가 있어 스케줄 사이클과 겹쳐도 안전.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_open_kicked_date", "") == today:
            return
        self._open_kicked_date = today
        logger.info("KR 개장 — 준비된 우선순위로 즉시 매수 사이클 실행")
        import threading

        def _kick():
            try:
                self.run_kr()
            except Exception:
                logger.debug("개장 즉시 매수 사이클 실패", exc_info=True)
        threading.Thread(target=_kick, daemon=True).start()

    def _fast_entry_scan(self):
        """빠른 로컬 진입 서브루프 (~2분) — 추가 Claude 호출 없이 급등 돌파·과매도 반등을 포착해 즉시 진입.

        5분 run_once 사이에 급등(돌파)이 시작된 종목을 놓쳐 수익을 못 잡던 문제를 보완한다.
        '추가 Claude 호출 없이'의 의미: 스캔 유니버스는 장전에 LLM 선별+RS 게이트를 이미 거쳤고, 매 진입도
        _handle_buy의 장전 센티멘트 게이트(_pre_market_buy_gate)를 통과한다. 전략적 AI 판단은 상류(장전)에
        반영돼 있고, 여기선 돌파 순간의 실시간 Claude 호출만 생략한다(타이밍 포착은 로컬 신호).
        신호는 전부 로컬: breakout_signal+volume_surge+momentum_score / check_oversold_bounce.
        진입은 _handle_buy(fast_entry=True)로 라우팅 → 현금·churn·추세·RSI추격 차단 등 모든 안전 게이트 통과,
        개장가드만 우회(확인된 신호는 '보류 대신 진입'). 매수 후엔 40초 빠른 익절(_fast_exit_scan)이 보호.
        """
        fe = (self.config.get("fast_entry", {}) or {})
        if not fe.get("enabled", True):
            return
        if getattr(self, "_buy_blocked_low_cash", False):
            return  # 현금 없으면 스캔 스킵 (불필요한 quote 호출 방지)
        if not self._fast_entry_lock.acquire(blocking=False):
            return
        import threading

        def _run():
            try:
                if self.client.is_market_open():
                    self._fast_entry_market("KR", fe)
                if self.client.is_us_market_open():
                    self._fast_entry_market("US", fe)
            except Exception:
                logger.debug("fast_entry_scan 오류", exc_info=True)
            finally:
                self._fast_entry_lock.release()

        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception:
            self._fast_entry_lock.release()
            logger.debug("fast_entry_scan 스레드 시작 실패", exc_info=True)

    # ── 실시간 진입 트리거 (이벤트 드리븐) ──
    # WS 체결 틱에서 감시종목이 기준가 대비 +N% 급등하면 "빠른진입 스캔을 지금 돌려라"는
    # 신호만 켠다. 실제 매수는 _fast_entry_scan 의 모든 안전 게이트를 그대로 거친다(WS 우회 없음).
    # 폴링(2분)을 기다리지 않고 급등 순간 수 초 내 반응. 기본 OFF(config.realtime.entry_enabled).

    def _make_entry_tick_cb(self, threshold: float):
        """감시종목 체결 틱 콜백 생성 — 기준가 대비 +threshold% 급등이면 진입 벨을 울린다(무API)."""
        def _cb(msg):
            try:
                code = msg.get("code")
                price = msg.get("price", 0) or 0
                if not code or price <= 0:
                    return
                ref = self._rt_entry_ref.get(code)
                if not ref or ref <= 0:
                    self._rt_entry_ref[code] = price  # 첫 틱 = 기준가
                    return
                if (price - ref) / ref * 100.0 >= threshold:
                    self._rt_entry_ref[code] = price  # 재기준(반복 트리거 방지)
                    self._realtime_entry_triggered = True
            except Exception:
                pass
        return _cb

    def _realtime_entry_setup(self):
        """감시종목(kr_stocks+us_stocks)을 WS 구독해 급등 틱 → 빠른진입 트리거. opt-in."""
        rt = self.config.get("realtime", {}) or {}
        if not (rt.get("enabled", True) and rt.get("entry_enabled", False)):
            return
        try:
            if self._ws_manager is None:
                import os
                from zusik.clients.kis_websocket import KISWebSocketManager
                self._ws_manager = KISWebSocketManager(
                    app_key=os.environ.get("KIS_APP_KEY", ""),
                    app_secret=os.environ.get("KIS_APP_SECRET", ""),
                    is_virtual=bool(getattr(self.client, "is_virtual", False)),
                )
                if not self._ws_manager.start():
                    self._ws_manager = None
                    logger.info("실시간 진입: WS 비활성 — 폴링 진입만")
                    return
            thr = float(rt.get("entry_threshold_pct", 1.5))
            cb = self._make_entry_tick_cb(thr)
            n = 0
            for s in (self.kr_stocks or []):
                code = s.get("code")
                if code and code not in self._ws_subscribed and code not in self._ws_entry_subscribed:
                    self._ws_manager.subscribe(code, cb, market="KR")
                    self._ws_entry_subscribed.add(code)
                    n += 1
            for s in (self.us_stocks or []):
                code = s.get("ticker") or s.get("code")
                if code and code not in self._ws_subscribed and code not in self._ws_entry_subscribed:
                    self._ws_manager.subscribe(code, cb, market="US",
                                               exchange=s.get("exchange", "NASD"))
                    self._ws_entry_subscribed.add(code)
                    n += 1
            logger.info("실시간 진입 트리거 구독 %d종목 (임계 +%.1f%%)", n, thr)
        except Exception:
            logger.debug("실시간 진입 구독 실패", exc_info=True)

    def _drain_realtime_entry(self):
        """급등 틱 감지 시 빠른진입 스캔을 즉시 1회 실행(드레인). 미감지면 무동작."""
        if not getattr(self, "_realtime_entry_triggered", False):
            return
        self._realtime_entry_triggered = False
        try:
            self._fast_entry_scan()
        except Exception:
            logger.debug("실시간 진입 드레인 예외", exc_info=True)

    def _fast_entry_market(self, market: str, fe: dict):
        """시장별 빠른 로컬 진입. 워치리스트 비보유 종목에서 급등돌파/과매도반등 신호만 포착.

        급등: N일 고점 돌파 + 거래량 폭증 + 모멘텀(local). 급락: RSI 극단 과매도 반등(local).
        실시간 Claude 호출만 생략(장전 선별/게이트는 이미 반영). _handle_buy의 RSI>78 추격 차단이 꼭지 진입을 막는다.
        """
        import time as _t
        from zusik.analysis.indicators import breakout_signal, volume_surge, momentum_score
        mom_min = float(fe.get("momentum_min", 0.6))
        max_new = int(fe.get("max_new_per_scan", 1))
        retry_throttle = float(fe.get("retry_throttle_sec", 600))  # 같은 종목 재시도 최소 간격
        if market == "KR":
            watch = list(self.kr_stocks or [])
            bal = self.client.get_balance()
            held = {h.get("code") for h in (bal.get("holdings", []) or [])}
        else:
            watch = list(self.us_stocks or [])
            bal = self.client.get_us_balance()
            held = {h.get("ticker") for h in (bal.get("holdings", []) or [])}
        now = _t.time()
        new_count = 0
        for s in watch:
            if new_count >= max_new:
                break
            code = s.get("code") if market == "KR" else s.get("ticker")
            if not code or code in held or self._is_inverse(code):
                continue
            if now - self._fast_entry_last.get(code, 0.0) < retry_throttle:
                continue
            name = s.get("name", code)
            exch = s.get("exchange", "NASD")
            try:
                if market == "KR":
                    df = self.client.get_ohlcv(code, period=getattr(self, "period", "D"))
                    price = int(self.client.get_current_price(code).get("price", 0) or 0)
                else:
                    df = self.client.get_us_ohlcv(code, exch)
                    price = float(self.client.get_us_current_price(code, exch).get("price", 0) or 0)
            except Exception:
                continue
            if df is None or len(df) < 60 or price <= 0:
                continue
            try:
                surge = bool(breakout_signal(df).get("is_breakout")
                             and volume_surge(df).get("is_surge")
                             and momentum_score(df) >= mom_min)
                bounce = None if surge else self.signals.check_oversold_bounce(df, code, name)
            except Exception:
                continue
            if not (surge or bounce):
                continue
            self._fast_entry_last[code] = now
            reason = "급등 돌파(거래량+모멘텀)" if surge else "과매도 반등"
            logger.info("빠른 진입 신호: %s (%s) — 로컬 신호 즉시 진입 검토", name, reason)
            try:
                if market == "KR":
                    self._handle_buy(code, name, price, df=df, fast_entry=True)
                else:
                    self._handle_us_buy(code, name, price, exch, df=df, fast_entry=True)
                new_count += 1
            except Exception:
                logger.debug("fast_entry 매수 실패 %s", name, exc_info=True)

    def _fast_exit_scan(self):
        """빠른 로컬 익절 서브루프 (~40초) — 보유종목의 시간민감 '익절·수익보호'만, Claude 없이.

        5분 run_once 사이에 종목이 급등→되돌림하면 익절을 놓치던 문제(수익 놓침)를 보완한다.
        급등 익절(check_surge)·트레일링 스톱·본전 보호만 본다 — 전부 '수익 보호' 트리거라
        손실 컷은 하지 않는다(hold-floor 철학 유지: 트레일링/본전보호는 순익 구간에서만 발동).
        WS 실시간 익절(extreme tier)과 상보 — WS 미구독 일반 변동성 종목까지 40초 안전망으로 커버.
        보유 가격은 잔고 응답의 current_price 재사용 → 추가 quote 호출 없음(시장당 잔고 1콜).
        """
        if not self._fast_scan_lock.acquire(blocking=False):
            return  # 직전 스캔이 아직 진행 중 → 스킵 (중복 방지)
        import threading

        def _run():
            try:
                if self.client.is_market_open():
                    self._fast_exit_market("KR")
                if self.client.is_us_market_open():
                    self._fast_exit_market("US")
            except Exception:
                logger.debug("fast_exit_scan 오류", exc_info=True)
            finally:
                self._fast_scan_lock.release()

        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception:
            self._fast_scan_lock.release()
            logger.debug("fast_exit_scan 스레드 시작 실패", exc_info=True)

    def _fast_exit_market(self, market: str):
        """시장별 보유종목 빠른 익절 체크. KR/US 공통 로직."""
        import time as _t
        if market == "KR":
            bal = self.client.get_balance()
            keyf = "code"
        else:
            bal = self.client.get_us_balance()
            keyf = "ticker"
        now = _t.time()
        for h in (bal.get("holdings", []) or []):
            try:
                code = h.get(keyf) or h.get("code")
                qty = h.get("qty", 0) or 0
                price = h.get("current_price", 0) or 0
                if not code or qty <= 0 or price <= 0:
                    continue
                # 인버스 헷지는 급등 익절 대상 아님 — 레짐 청산(_should_force_exit_inverse)이 담당
                if self._is_inverse(code):
                    continue
                # per-code 60s 스로틀 — run_once/WS와 동시 발동 시 중복 매도 방지
                if now - self._fast_exit_last.get(code, 0.0) < 60:
                    continue
                name = h.get("name", code)
                exch = h.get("exchange", "NASD")
                sold = False

                # 1) 급등 익절 (절반익절/라이딩 트림 sell_ratio 그대로 반영)
                surge = self.positions.check_surge(code, int(price))
                if surge:
                    if market == "KR":
                        self._handle_sell(code, name, force_reason=f"{surge['reason']} (fast)",
                                          sell_ratio=surge.get("sell_ratio", 1.0))
                    else:
                        self._us_force_sell_reason = f"{surge['reason']} (fast)"
                        try:
                            self._handle_us_sell(code, name, exch, sell_ratio=surge.get("sell_ratio", 1.0))
                        finally:
                            self._us_force_sell_reason = ""
                    sold = True
                else:
                    # 2) 트레일링/본전 보호 (둘 다 순익 구간에서만 발동 — 손실 컷 아님)
                    trailing = self.positions.update_trailing_stop(code, int(price))
                    if trailing:
                        act = trailing.get("action")
                        if act == "stop_triggered":
                            if market == "KR":
                                self._handle_sell(code, name, force_reason="트레일링 스톱 (fast)")
                            else:
                                self._us_force_sell_reason = "트레일링 스톱 (fast)"
                                try:
                                    self._handle_us_sell(code, name, exch)
                                finally:
                                    self._us_force_sell_reason = ""
                            sold = True
                        elif act == "breakeven_protect" and not self._core_hold_through(code):
                            peak = trailing.get("peak_profit", 0) * 100
                            if market == "KR":
                                self._handle_sell(code, name,
                                                  force_reason=f"본전 보호 (최고 +{peak:.1f}%, fast)")
                            else:
                                self._us_force_sell_reason = f"본전 보호 (최고 +{peak:.1f}%, fast)"
                                try:
                                    self._handle_us_sell(code, name, exch)
                                finally:
                                    self._us_force_sell_reason = ""
                            sold = True

                if sold:
                    self._fast_exit_last[code] = now
                    logger.info("fast-exit 익절/보호: %s (%s)", name, market)
            except Exception:
                logger.debug("fast_exit %s 오류", h.get(keyf, "?"), exc_info=True)

