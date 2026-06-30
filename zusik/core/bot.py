from __future__ import annotations
"""TradingBot — zusik 메인 트레이딩 봇.

처음 보는 사람을 위한 흐름:

1) main.py가 .env 로드 후 TradingBot(config) 인스턴스 생성, schedule 등록
2) 매 1분 tick() 호출 — Discord 명령, 종목 재선별, 백테스트 등
3) 매 2분 run_once() 호출 — KR/US 장 중일 때 종목별 _execute_stock 실행
4) _execute_stock 흐름:
   - OHLCV fetch (KIS API)
   - 보유 종목 안전망 체크 (crash/surge/trailing/breakeven/RSI 익절/빠른 손절)
   - cost_optimizer가 LLM 호출 필요 여부 판단
   - strategy.analyze(df) → buy/sell/hold
   - hysteresis 가드 (신호 진동 차단)
   - buy → _churn_guard (추세/일중/MC 게이트) → _handle_buy
   - sell → _should_defer_sell → _handle_sell

핵심 안전망 (LLM 호출 없이 즉시 작동):
  - _is_weak_trend          : 5/20/60일선 데드크로스 차단
  - _churn_guard            : 24h 재진입 + 일일 한도 + 일중 변동 + 추세
  - _apply_hysteresis       : BUY ↔ SELL 진동 차단
  - _mc_buy_gate            : Monte Carlo P(profit>0) ≥ 55%
  - check_oversold_bounce   : RSI ≤ 20 + 거래량 → 매수
  - check_overbought_exit   : RSI ≥ 70 + 수익 → 익절
  - check_quick_loss_exit   : RSI 급락 + 데드크로스 → 손절

자세한 모듈 흐름은 README.md 참조. 설계 결정은 CLAUDE.md.
"""

import json
import logging
import os
import time
from datetime import datetime

import schedule
import yaml

from zusik.clients.kis_client import KISClient
from zusik.clients.discord_notifier import DiscordNotifier
from zusik.clients.discord_bot import send_bot_message
from zusik.storage.portfolio_tracker import PortfolioTracker
from zusik.core.risk_manager import RiskManager
from zusik.core.reward_engine import RewardEngine
from zusik.analysis.stock_screener import StockScreener
from zusik.core.position_manager import PositionManager
from zusik.core.cost_optimizer import CostOptimizer
from zusik.clients.crypto_client import CryptoClient
from zusik.analysis.smart_signals import SmartSignals
from zusik.core.performance_trainer import PerformanceTrainer
from zusik.core.event_learner import EventLearner
from zusik.core.resilience import OrderGuard, NetworkMonitor
from zusik.clients.discord_commander import DiscordCommander
from zusik.core.portfolio_arena import PortfolioArena
from zusik.strategies.base import Strategy
from zusik.strategies.ma_cross import MACrossStrategy
from zusik.strategies.rsi import RSIStrategy
from zusik.strategies.bollinger import BollingerBandStrategy
from zusik.strategies.volatility_breakout import VolatilityBreakoutStrategy
from zusik.strategies.dual_momentum import DualMomentumStrategy
from zusik.strategies.macd_rsi import MACDRSIStrategy
from zusik.strategies.adaptive import AdaptiveStrategy
from zusik.strategies.claude_strategy import ClaudeStrategy
from zusik.strategies.auto_hybrid import AutoHybridStrategy
from zusik.strategies.momentum_breakout import MomentumBreakoutStrategy

logger = logging.getLogger(__name__)

from zusik.core.bot_kr import KRTradingMixin
from zusik.core.bot_us import USTradingMixin
from zusik.core.bot_inverse import InverseHedgeMixin
from zusik.core.bot_fastlane import FastLaneMixin
from zusik.core.bot_reporting import ReportingMixin
from zusik.core.bot_risk import RiskExitMixin
from zusik.core.bot_sizing import SizingModeMixin
from zusik.core.bot_selection import SelectionMixin
from zusik.core.bot_aux import AuxMarketsMixin
from zusik.core.bot_helpers import CoreHelpersMixin


class _BotNotifierFallback:
    """Webhook 없을 때 Discord Bot으로 알림 폴백."""

    def notify_trade(self, side="", stock_name="", stock_code="", qty=0, price=0,
                     reason="", is_long_term=False, long_term_reason="",
                     realized_pnl=0, realized_rate=0, **kw):
        emoji = {"buy": "매수", "long_term_buy": "장기", "sell": "매도"}.get(side, side)
        # 1) 헤더 — 짧으니 한 메시지
        header = f"**{emoji}** `{stock_name}({stock_code})` {qty}주 @ {price:,}"
        if realized_pnl:
            header += f" | 손익 {realized_pnl:+,}원({realized_rate:+.1f}%)"
        send_bot_message(header)

        # 2) 판단 근거 — 4명 합산은 4~12k chars라 사전 자르지 않고 전체 표시
        # Discord 메시지 한도 2000 → 1800 chars씩 잘라 코드블록을 매 chunk마다 닫기
        if reason:
            self._send_long_block("판단 근거", reason)

        # 3) 장기투자 사유 (이전엔 누락됐음)
        if is_long_term and long_term_reason:
            self._send_long_block("장기투자 사유", long_term_reason)

    def _send_long_block(self, title: str, body: str, chunk_size: int = 1800):
        """긴 텍스트를 Discord 메시지 한도 안에 코드블록으로 분할 송신."""
        if not body:
            return
        send_bot_message(f"**{title}**")
        s = str(body)
        cursor = 0
        while cursor < len(s):
            end = cursor + chunk_size
            # 줄/어휘 경계 우선 분할 (코드블록 안에서 자연스럽게)
            if end < len(s):
                nl = s.rfind("\n", cursor, end)
                if nl > cursor:
                    end = nl
                else:
                    sp = s.rfind(" ", cursor, end)
                    if sp > cursor:
                        end = sp
            piece = s[cursor:end]
            send_bot_message(f"```\n{piece}\n```")
            cursor = end + 1 if end < len(s) and s[end] in ("\n", " ") else end

    def notify_error(self, message):
        send_bot_message(f"{message}")

    def notify_emergency_hold(self, reason=""):
        send_bot_message(f"**긴급 홀딩**: {reason}")

    def notify_emergency_release(self):
        send_bot_message("긴급 홀딩 해제")

    def notify_strategy_switch(self, old="", new="", reason=""):
        send_bot_message(f"전략 전환: {old} → {new}\n{reason}")

    def notify_daily_target_reached(self, pnl=0, rate=0.0, target_rate=0.0):
        send_bot_message(
            f"일일 목표 {pnl:+,}원 ({rate:+.2f}% / 목표 {target_rate:.2f}%) 도달 — "
            f"추가 수익을 위해 계속 동작합니다"
        )

    def notify_pattern_report(self, date="", stats=None, total_pnl=0, market=""):
        stats = stats or {}
        if not stats:
            return
        tag = f"{market} " if market else ""
        lines = [f"**{tag}일일 매도 패턴 리포트 — {date}**  (합계 **{total_pnl:+,}원**)"]
        for pat, s in sorted(stats.items(), key=lambda x: -x[1]["pnl_sum"])[:8]:
            lines.append(f"• `{pat}`  {s['count']}건 · 승률 {s['win_rate']:.0f}% · "
                         f"총 {s['pnl_sum']:+,}원 · 건당 {int(s['avg_pnl']):+,}원")
        send_bot_message("\n".join(lines))

    def notify_effective_pnl(self, date="", summary=None):
        summary = summary or {}
        if not summary:
            return
        lines = [f"**실효 수익 분해 — {date}**"]
        lines.append(f"실현 누적: **{summary['realized_total']:+,}원**")
        lines.append(f"미실현 (평가차익): **{summary['unrealized_krw']:+,}원**")
        lines.append(f"실효 순수익 = 실현 + 미실현: **{summary['effective_total']:+,}원**")
        lines.append("━━━━━━━━━━━━")
        lines.append(f"현재 총자산: {summary['total_equity_now']:,}원")
        lines.append(f"누적 입금: {summary['total_deposits']:,}원")
        lines.append(f"명목 증가 (자산 − 입금): {summary['apparent_gain']:+,}원")
        lines.append(f"환율·집계 효과: {summary['fx_and_other_effect']:+,}원")
        send_bot_message("\n".join(lines))

    def notify_monthly_report(self, stats=None):
        stats = stats or {}
        if not stats or stats.get("days", 0) == 0:
            return
        from zusik.reporting.monthly_text import format_monthly_report
        send_bot_message(format_monthly_report(stats))

    def notify_stock_rotation(self, changes="", kr_list="", us_list=""):
        send_bot_message(f"종목 교체:\n{changes}")

    def notify_forced_stop_loss(self, name="", code="", rate=0, qty=0):
        send_bot_message(f"강제 손절: {name}({code}) {rate:+.1f}% {qty}주")

    def notify_stock_danger(self, name="", code="", danger_level="", reasons=None, action=""):
        r = ", ".join(reasons) if isinstance(reasons, list) else (reasons or "")
        send_bot_message(f"위험 종목 [{danger_level}]: {name}({code}) — {r} / 조치: {action}")

    def _send(self, **kw):
        # content(평문)는 Bot 채널로 전달 — watchdog_alert·명령 결과·헬스체크(--notify) 알림 경로.
        # (이전엔 전체 no-op이라 webhook 없는 봇토큰 구성에서 이 알림들이 조용히 사라졌다.)
        # embed 는 Bot 에서 미지원이라 무시.
        content = kw.get("content")
        if content:
            send_bot_message(str(content))

    def __getattr__(self, name):
        # `discord_notifier.DiscordNotifier`에는 있지만 stub에 명시되지 않은
        # `notify_*` 메서드는 자동으로 no-op 처리해 `AttributeError`가
        # `_check_risks_before_trading`을 중단시키지 않도록 방어.
        # 주요 알림은 위에서 명시적으로 구현됨 (send_bot_message 호출).
        if name.startswith("notify_"):
            return lambda *a, **kw: None
        raise AttributeError(name)


STRATEGY_MAP: dict[str, type[Strategy]] = {
    "ma_cross": MACrossStrategy,
    "rsi": RSIStrategy,
    "bollinger": BollingerBandStrategy,
    "volatility_breakout": VolatilityBreakoutStrategy,
    "dual_momentum": DualMomentumStrategy,
    "macd_rsi": MACDRSIStrategy,
    "adaptive": AdaptiveStrategy,
    "claude": ClaudeStrategy,
    "auto_hybrid": AutoHybridStrategy,
    "momentum_breakout": MomentumBreakoutStrategy,
}


def _build_strategy(name: str, config: dict) -> Strategy:
    """전략 이름으로 전략 객체 생성."""
    strategy_cfg = dict(config.get("strategy", {}))
    strategy_cfg.pop("name", None)

    strategy_cls = STRATEGY_MAP.get(name)
    if strategy_cls is None:
        raise ValueError(f"알 수 없는 전략: {name}")

    if name in ("claude", "auto_hybrid"):
        api_key = strategy_cfg.get("api_key") or os.getenv("ANTHROPIC_API_KEY", "")
        strategy_cfg["api_key"] = api_key
    else:
        # claude 전용 파라미터 제거
        for k in ("api_key", "prefer_cli", "use_web_search", "min_confidence", "model"):
            strategy_cfg.pop(k, None)

    # 전략 클래스가 받지 않는 파라미터 안전하게 제거
    import inspect
    valid_params = inspect.signature(strategy_cls.__init__).parameters
    filtered = {k: v for k, v in strategy_cfg.items() if k in valid_params}
    return strategy_cls(**filtered)


class TradingBot(
    KRTradingMixin, USTradingMixin, InverseHedgeMixin, FastLaneMixin, ReportingMixin, RiskExitMixin, SizingModeMixin, SelectionMixin, AuxMarketsMixin, CoreHelpersMixin,
):
    """한국 주식 자동매매 봇.

    3중 방어 시스템:
      1. 실현손실 -10% → 전략 자동 교체
      2. 급락/전쟁 감지 → 긴급 홀딩 (매매 전면 중단)
      3. 상장폐지/관리종목 감지 → 즉시 매도, 종목별 -15% 강제 손절
    """

    def __init__(self, client: KISClient, config: dict, discord: DiscordNotifier | None = None):
        self.client = client
        self.config = config
        # Discord: webhook 없으면 Bot 기반 알림으로 자동 폴백
        self.discord = discord or _BotNotifierFallback()
        self.tracker = PortfolioTracker()
        self.risk = RiskManager(config)
        self.reward = RewardEngine(config)
        self.positions = PositionManager(config)
        self.cost = CostOptimizer(config)
        self.signals = SmartSignals(config)
        self.trainer = PerformanceTrainer(config)
        self.order_guard = OrderGuard()
        self.network = NetworkMonitor()

        # 암호화폐 (Upbit)
        upbit_access = os.getenv("UPBIT_ACCESS_KEY", "")
        upbit_secret = os.getenv("UPBIT_SECRET_KEY", "")
        self.crypto = CryptoClient(upbit_access, upbit_secret)
        self.crypto_tickers: list[str] = config.get("crypto_tickers", [])
        self.crypto_invest_ratio: float = config.get("crypto_invest_ratio", 0.15)

        # 자동 종목 선별
        self.auto_screen: bool = config.get("screening", {}).get("enabled", True)
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.screener = StockScreener(api_key=api_key, config=config)

        # 파생ETF 권한 — KIS 선택확인서 미등록 계좌면 인버스/선물/레버리지/커버드콜 차단
        self.derivative_etf_enabled: bool = config.get("broker", {}).get(
            "derivative_etf_enabled", True
        )

        # 미국 매매 토글 — 미국 주식을 안 하는 사용자는 config us_enabled: false 로 끈다(기본 true).
        # 끄면 _default_us 와 선별 US 가 비워져 us_stocks 가 항상 [] → 미국 매매·알림·잔고조회가
        # 일괄 차단되고 run_us 도 조기 반환한다.
        self.us_enabled: bool = bool(config.get("us_enabled", True))

        # 종목 리스트 — config 기본 풀에서 파생ETF 제거 (권한 없을 때)
        self._default_kr: list[dict] = self._filter_derivatives(
            config.get("stocks", []), market="KR"
        )
        self._default_us: list[dict] = self._filter_derivatives(
            config.get("us_stocks", []), market="US"
        ) if self.us_enabled else []
        if not self.us_enabled:
            logger.info("미국 매매 비활성 (config us_enabled=false) — KR/crypto 만 운용")
        self._load_stocks()

        self.invest_ratio: float = config.get("invest_ratio", 0.1)
        self.min_amount: int = config.get("min_amount", 50000)
        self.min_amount_usd: float = config.get("min_amount_usd", 50)
        self.interval: int = config.get("run_interval_minutes", 5)
        self.period: str = config.get("candle_period", "D")
        self.long_term_ratio: float = config.get("long_term_ratio", 0.2)
        cooldown_cfg = config.get("cooldown", {})
        self.daily_target_min_confidence: float = cooldown_cfg.get(
            "daily_target_min_confidence", 0.80
        )
        self.daily_target_invest_ratio: float = cooldown_cfg.get(
            "daily_target_invest_ratio", 0.50
        )
        consensus_cfg = config.get("consensus", {})
        self.consensus_unanimous_multiplier: float = consensus_cfg.get(
            "unanimous_multiplier", 1.20
        )
        self.consensus_majority_multiplier: float = consensus_cfg.get(
            "majority_multiplier", 1.12
        )
        self.consensus_split_multiplier: float = consensus_cfg.get(
            "split_multiplier", 0.60
        )
        self.consensus_mixed_multiplier: float = consensus_cfg.get(
            "mixed_multiplier", 0.85
        )
        # defensive 모드 토글 (drawdown 기반 강제 활성 + 매수 conf 70% 차단)
        # false면 drawdown 가드를 끄고 적극 회복 시도. 시장 crisis/war defensive는 유지.
        self.defensive_mode_enabled: bool = bool(
            config.get("risk", {}).get("defensive_mode_enabled", True)
        )

        # 전략 (event_learner보다 먼저 초기화)
        strategy_name = config.get("strategy", {}).get("name", "claude")
        self.strategy: Strategy = _build_strategy(strategy_name, config)
        self.use_claude = isinstance(self.strategy, (ClaudeStrategy, AutoHybridStrategy))
        self.use_adaptive = isinstance(self.strategy, AdaptiveStrategy)

        self._cached_portfolio_info = ""

        # event_learner (use_claude 이후에 초기화)
        _analyst = getattr(self.strategy, "analyst", None)
        self.event_learner = EventLearner(
            claude_client=_analyst._client if _analyst is not None else None
        )
        self.commander = DiscordCommander(self)
        self.arena = PortfolioArena()

        # 아레나 로컬 전략 경쟁자 (Claude 없이 독립 운영)
        from zusik.strategies.ma_cross import MACrossStrategy
        from zusik.strategies.rsi import RSIStrategy
        from zusik.strategies.bollinger import BollingerBandStrategy
        from zusik.strategies.macd_rsi import MACDRSIStrategy
        self._arena_strategies = {
            "adaptive": AdaptiveStrategy(),
            "momentum_breakout": MomentumBreakoutStrategy(),
            "ma_cross": MACrossStrategy(),
            "rsi": RSIStrategy(),
            "bollinger": BollingerBandStrategy(),
            "macd_rsi": MACDRSIStrategy(),
        }
        self._last_arena_scan: datetime | None = None
        self._last_global_backtest: datetime | None = None  # 5/1: 다종목 글로벌 백테스트

        self._active_mode: str = config.get("_active_mode", "yolo")
        self._prev_cash: int = 0  # 적립금 감지용

        logger.info("전략: %s | 모드: %s | %d분 | KR %d + US %d | 자동선별 %s",
                     self.strategy.name, self._active_mode.upper(), self.interval,
                     len(self.kr_stocks), len(self.us_stocks),
                     "ON" if self.auto_screen else "OFF")

        self._pre_market_notified: str = ""
        self._post_market_notified: str = ""
        self._us_pre_notified: str = ""
        self._us_post_notified: str = ""
        self._daily_loss_halted: str = ""
        self._daily_target_reached: str = ""
        self._daily_target_cooldown: bool = False
        self._merge_logged_kr: set[str] = set()
        self._merge_logged_us: set[str] = set()
        # 인버스 운용 상태
        self._market_condition: str = "peace"
        self._bear_cache: tuple[float, float] = (0.0, 0.0)  # (epoch, score)
        # 이벤트 로테이션: 장전 리포트에서 감지한 활성 수혜 섹터 (선별 부스트에 사용)
        self._active_event_sectors: set = self._load_active_event_sectors()
        # 재진입 차단 (crash_instant/slow_bleed 매도 후 일정시간 재매수 금지) + 일일 churn 방지
        self._reentry_block: dict[str, tuple[float, str]] = {}  # code → (until_epoch, reason)
        self._daily_sell_count: dict[str, int] = {}  # code → 오늘 매도 횟수
        self._load_reentry_block()
        # 칼날 재진입 차단: 익절 후 매도가 대비 -5%↓ 재매수 금지 48h
        self._knife_block: dict[str, tuple[float, float]] = {}  # code → (until_epoch, sell_price)
        self._load_knife_block()
        # 일중 변동성 체크용 (매수 직전 crash_instant 직전 위험 회피)
        self._last_intraday_change: dict[str, float] = {}  # code → 직전 측정 일중 변동률
        self._tick_exempt_logged: dict[str, float] = {}  # code → 틱 급락 면제 로그 스로틀(ts)
        # 신호 진동 가드 (4-analyst 응답 노이즈로 BUY ↔ SELL 뒤집힘 차단)
        self._signal_history: dict[str, tuple[float, str, float]] = {}  # code → (epoch, signal, conf)
        # WebSocket 실시간 (extreme tier 보유 종목용 — 5/1 추가)
        self._ws_manager = None  # KISWebSocketManager (lazy 초기화)
        self._ws_subscribed: set[str] = set()
        # 실시간 진입 트리거(event-driven entry) 상태
        self._ws_entry_subscribed: set[str] = set()
        self._rt_entry_ref: dict[str, float] = {}          # code → 진입 트리거 기준가
        self._realtime_entry_triggered = False             # 급등 틱 감지 → 다음 드레인서 빠른진입 스캔
        # 빠른 로컬 익절 서브루프 (~40초) — 5분 run_once 사이 급등→되돌림 익절 놓침 보완.
        # WS(extreme tier)와 상보: WS 미구독 일반종목까지 40초 안전망으로 익절/트레일링/본전 보호.
        import threading as _threading
        self._fast_scan_lock = _threading.Lock()
        self._fast_entry_lock = _threading.Lock()          # 빠른 로컬 진입 스캔 동시실행 가드
        self._fast_entry_last: dict[str, float] = {}       # code → 마지막 fast-entry 시도 ts (재시도 스로틀)
        self._fast_exit_last: dict[str, float] = {}        # code → 마지막 fast-exit 매도 ts (중복매도 스로틀)
        self._tick_surge_throttle: dict[str, float] = {}   # code → 마지막 WS 틱 익절 ts (틱 폭주 방지)
        # Vortex/FPGA 가속 제거 — 종목선택·MC·RSI는 numpy(bot_money_helpers)로 일원화.
        self._accel = None
        self._rsi_cache: dict = {}  # code → RSI series(np.ndarray) 캐시
        self._last_mc_stats: dict | None = None  # 직전 MC 결과 (LLM 컨텍스트/Kelly 재사용)
        # 페어 트레이딩 (변동성 시장에서도 수익 — 시장 방향 무관)
        try:
            from zusik.core.pair_trader import PairTrader
            self._pair_trader = PairTrader()
            logger.info("페어 트레이더: %d 페어 활성", len(self._pair_trader.pairs))
        except Exception as e:
            logger.debug("페어 트레이더 초기화 실패: %s", e)
            self._pair_trader = None
        self._last_pair_scan: datetime | None = None
        self._pair_signals_today: set[str] = set()  # 일일 dedup
        self._last_pair_discovery: datetime | None = None  # 자동 페어 발굴

        self._name_cache: dict[str, str] = {}
        for s in self.kr_stocks:
            if s.get("name"):
                self._name_cache[s["code"]] = s["name"]
        for s in self.us_stocks:
            if s.get("name"):
                self._name_cache[s["ticker"]] = s["name"]


    _LAST_COMMIT_FILE = os.path.join("data", "last_commit_hash.txt")


    # ── 재진입 차단 / 일일 churn 가드 ──
    _REENTRY_BLOCK_FILE = os.path.join("data", "reentry_block.json")
    _DAILY_TARGET_FILE = os.path.join("data", "last_daily_target.txt")  # 일일목표 알림 1일1회 가드(재시작 유지)
    DAILY_SELL_LIMIT = 3  # 같은 종목 일일 매도 ≥ 3회 → 추가 매수 차단
    # 세션 churn 차단: 익절 매도 후 12h 차단 = "현 세션 차단 / 다음 세션 허용".
    # KR/US 세션 어느 시점에 팔아도 +12h는 마감 이후~다음 개장 이전에 만료(주말은 비거래로
    # 자연 처리). 데이터: 같은세션 평평 재매수 -214k 차단, 세션넘김 재진입(중립~+)은 허용.
    # +2% 돌파 override: 매도가 ×1.02 위로 재돌파면 즉시 허용(추세 지속 재진입 +208k 살림).
    _SESSION_BLOCK_HOURS = 12.0
    _REENTRY_BREAKOUT = 1.02


    # ── 칼날 재진입 차단 ──
    # 익절(과매수/분할/본전) 매도 후 48h 내 매도가 대비 -5%↓ 재매수 금지.
    # 실측: HPE RSI94 +44% 익절(+955k) → 다음날 -12% 떨어진 칼날 재매수 → 트레일링
    # -187k (블로우오프 후 mean reversion 추격). 반례 BB는 매도가 -0.2~-1.8%에서 추세 지속
    # 재진입 → +171k — 임계 -5%는 BB(허용)와 HPE(차단)를 정확히 가른다.
    # 인버스 설계 메모와 동일 교훈: pullback 추격이 손실원, 추세 지속 추격이 수익원.
    _KNIFE_BLOCK_FILE = os.path.join("data", "knife_block.json")
    _KNIFE_HOURS = 48.0
    _KNIFE_RETRACE = 0.95  # 매도가 × 0.95 미만 재매수 차단


    # ── 신호 진동 가드 (hysteresis) ──
    SIGNAL_HYSTERESIS_HOURS = 6.0
    SIGNAL_HYSTERESIS_MIN_CONF = 0.70


    def _reset_daily(self):
        # 자정 지나 날짜가 바뀌었을 때만 1회 리셋 (이전 로직은 _pre_market_notified가
        # 비어있는 시간대에 매 사이클 리셋되어 _daily_target_reached dedup가 깨졌음)
        today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_last_daily_reset", "") != today:
            self._pre_market_notified = ""
            self._post_market_notified = ""
            self._daily_loss_halted = ""
            self._daily_target_reached = ""
            self._daily_target_cooldown = False
            self._merge_logged_kr.clear()
            self._merge_logged_us.clear()
            self._daily_sell_count.clear()
            self._pair_signals_today.clear()
            self._last_daily_reset = today

    # ══════════════════════════════════════
    # 리스크 체크 (매 실행마다)
    # ══════════════════════════════════════


    def _switch_strategy(self, new_name: str):
        """전략 교체 실행."""
        old_name = self.strategy.name
        try:
            self.strategy = _build_strategy(new_name, self.config)
            self.use_claude = isinstance(self.strategy, (ClaudeStrategy, AutoHybridStrategy))
            self.use_adaptive = isinstance(self.strategy, AdaptiveStrategy)

            logger.warning("전략 교체: %s → %s", old_name, new_name)
            if self.discord:
                realized = self.tracker.get_realized_pnl_total()
                self.discord.notify_strategy_switch(
                    old_name, new_name,
                    f"누적 실현손실 {realized['total_realized_pnl']:+,}원",
                )
        except Exception:
            logger.exception("전략 교체 실패: %s", new_name)

    # ══════════════════════════════════════
    # 위기 감지 + 긴급 홀딩 해제 판단
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 완전손실 방어: 종목별 강제 매도
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 장기투자 한도
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 장 시작 전 / 마감 후
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 매매 로직
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 인버스 ETF 운용 (하락장 헷지)
    # ══════════════════════════════════════


    _RS_DROP_THRESHOLD = -0.05  # 지수 대비 -5%p 이상 약하면 후보 제외


    _ACTIVE_EVENT_FILE = os.path.join("data", "active_event_sectors.json")


    _DERIVATIVE_NAME_KEYWORDS = (
        "인버스", "선물", "레버리지", "커버드콜",
        "inverse", "leverage", "futures", "covered call",
        " 2x", " 3x",
    )


    # 시장 지수 프록시 ETF — bearish_regime_score 산출용
    _INDEX_PROXIES_KR = [("069500", "KODEX 200")]
    _INDEX_PROXIES_US = [("QQQ", "NASD"), ("SPY", "AMEX")]

    # 시장을 끌고 내려갈 수 있는 美 메가캡 — '메가캡발 시장 급락'(fast_fall_guard) 감지용
    _MEGACAP_LEADERS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN"]

    # 시장 추종 인덱스 ETF (강제 노출 + 회전 대상)
    #: bull regime 시 KOSPI/SPY 베타 노출 확보용
    # KR 할당 측정은 KOSPI 추종만, 회전 매도 제외(보호)는 KR 거래소의 모든 인덱스 ETF
    _INDEX_ETF_KR_KOSPI = {"069500": "KODEX 200", "122630": "KODEX 레버리지"}  # KOSPI 추종 (KR 베타)
    _INDEX_ETF_KR_US_HEDGE = {"360750": "TIGER 미국S&P500"}  # KR 거래 + 미국 추종 (US 베타로 카운트)
    _INDEX_ETF_KR = {**_INDEX_ETF_KR_KOSPI, **_INDEX_ETF_KR_US_HEDGE}  # 회전 보호 대상 통합
    _INDEX_ETF_US = {"SPY": ("AMEX", "SPDR S&P 500"), "QQQ": ("NASD", "Invesco QQQ")}


    # ══════════════════════════════════════
    # 상승장 인덱스 추종 (bull regime + 강제 노출)
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 미국 주식 매매
    # ══════════════════════════════════════


    # ══════════════════════════════════════
    # 실행 — 듀얼마켓 (한국 + 미국)
    # ══════════════════════════════════════


    _OPEN_HOUR = {"KR": (9, 0), "US": (22, 30)}  # KST 개장 시각 (봇 세션 가정과 일치)


    def run_once(self):
        """3시장 모두 체크 (별도 스레드에서 실행 — Discord 안 막음)."""
        import threading
        def _run():
            try:
                # 인덱스 회전: 사이클당 1회 평가 (bull regime + 노출 부족 시 자동 회전)
                try:
                    self._maybe_rotate_to_index()
                except Exception:
                    logger.debug("인덱스 회전 호출 실패", exc_info=True)
                self.run_kr()
                self.run_us()
                self.run_crypto()
            except Exception as e:
                logger.exception("run_once 오류")
                msg = self._format_error_alert("run_once", "-", e)
                if self.discord and msg:
                    self.discord.notify_error(msg)
        threading.Thread(target=_run, daemon=True).start()


    def tick(self):
        """매 1분: 명령 처리 + 알림 + 종목 재선별 + 커밋 감지."""
        self._reset_daily()
        self._write_status_snapshot()   # data/status.json 갱신 (웹/CLI 단일 상태 소스)
        self._check_llm_health()        # LLM 다운/복구 전이 시 1회 통보
        self._refresh_learned_params()  # 캘리브레이션 갱신 시 재시작 없이 청산 파라미터 런타임 반영

        # Discord 명령 처리
        self.commander.process_pending()

        # Git 커밋 감지 → Discord 알림
        self._check_new_commits()

        # 수동 매매(MTS/HTS) 감지 → tracker 동기화
        self._reconcile_external_trades()

        # 아레나 스캔 (30분마다) — 로컬 전략 경쟁자가 감시 종목 전체 돌며 가상매매
        self._run_arena_cycle()

        # 글로벌 다종목 백테스트 (30분마다) — 통계적으로 의미 있는 전략 자동 선택
        self._run_global_backtest()

        # 페어 트레이딩 (30분마다) — 시장 방향 무관 수익
        self._run_pair_trading_cycle()

        # 자동 스크리닝 (일 1회) — 후보 풀 100+ 종목 MC 평가 + watch list 갱신
        self._run_auto_screening()

        # 매시간 자산 동기화 — equity_curve가 실제 한투 잔고와 차이날 때 자동 보정
        self._hourly_equity_sync()

        # 미체결 지정가 정정 (키움 ch8 amend 패턴) — 60초 초과 미체결 시 시장가 전환.
        # 현재 봇은 시장가 위주라 보통 no-op. 추후 지정가 진입을 켰을 때 효과.
        self._amend_stale_limit_orders()

        # 주기적 종목 재선별 — KR/US 장 중 하나라도 열렸을 때만
        # 장 닫힌 시간에 돌면 Claude 호출 비용 낭비 + 종목 교체 로그만 반복됨.
        # 장 시작 전 재선별은 `pre_market_alert`가 별도로 `_refresh_stocks(force=True)` 담당.
        import threading
        kr_open = False
        us_open = False
        try:
            kr_open = self.client.is_market_open()
            us_open = self.client.is_us_market_open()
        except Exception:
            pass
        if (kr_open or us_open) and not getattr(self, "_refreshing", False):
            self._refreshing = True
            def _bg_refresh():
                try:
                    self._refresh_stocks()
                finally:
                    self._refreshing = False
            threading.Thread(target=_bg_refresh, daemon=True).start()

        # 한국 시장
        kr_phase = self.client.market_phase()
        if kr_phase == "pre_market":
            self._update_portfolio_info()  # 장 시작 전 포트폴리오 갱신
            self.pre_market_alert()
            self._prepare_open_buys()      # 장전 분석 → 개장 매수 우선순위 준비 (하루 1회)
        elif kr_phase == "open":
            self._on_market_open()         # 개장 즉시 매수 사이클 (5분 스케줄 대기 X, 하루 1회)
            # 핵심주 코어 패스를 매 분(tick) 재시도 — run_kr(느림)·시세실패에 막히지 않게.
            # 자체적으로 market_open/busy 가드. 목표 도달하면 no-op.
            self._core_entry_pass_kr()
        elif kr_phase == "post_market":
            self._update_portfolio_info()  # 장 마감 후 갱신
            self.post_market_report()
        elif kr_phase == "open" and not hasattr(self, "_kr_open_updated"):
            self._update_portfolio_info()  # 장 시작 직후 1회
            self._kr_open_updated = True
            # 리부팅 등으로 장전 리포트를 놓친 경우 복구
            self.pre_market_alert()

        # 미국 시장
        us_phase = self.client.us_market_phase()
        today = datetime.now().strftime("%Y-%m-%d")

        if us_phase == "pre_market" and self._us_pre_notified != today and self.us_stocks:
            logger.info("──── US 장 시작 전 알림 ────")
            self._update_portfolio_info()
            # 매일 US 장전 강제 종목 재선별
            import threading as _th
            _th.Thread(target=lambda: self._refresh_stocks(force=True), daemon=True).start()
            self._send_pre_market_report("US")
            self._prepare_open_buys_us()   # US 장전 분석 → 개장 매수 우선순위 (하루 1회)
            self._us_pre_notified = today
        # 리부팅 등으로 US 장전 리포트를 놓친 경우 복구
        elif us_phase == "open" and self._us_pre_notified != today and self.us_stocks:
            logger.info("──── US 장전 리포트 복구 (리부팅) ────")
            self._update_portfolio_info()
            self._send_pre_market_report("US")
            self._prepare_open_buys_us()   # 리부팅 복구 시에도 우선순위 산출
            self._us_pre_notified = today

        # US 개장 즉시 매수 사이클 (5분 스케줄 대기 X, 하루 1회) — KR과 동일
        if us_phase == "open" and self.us_stocks:
            self._on_us_market_open()

        if us_phase == "post_market" and self._us_post_notified != today and self.us_stocks:
            logger.info("──── US 장 마감 ────")
            self._update_portfolio_info()
            self.run_cross_signals()
            # 미국장 EOD 매도 패턴 리포트 — '오늘 + 미국장' 매도만 (한국장 리포트와 분리)
            self._send_eod_pattern_report(market="US")
            self._us_post_notified = today

    def status(self):
        from zusik.analysis.bot_money_helpers import compute_total_equity, compute_pnl_vs_deposit
        balance = self.client.get_balance()
        realized_total = self.tracker.get_realized_pnl_total()
        long_term = self.tracker.get_long_term_holdings()
        risk_status = self.risk.get_status()
        cash = balance["cash"]
        total_eval = balance["total_eval"]

        # 진짜 총자산: KR + US 합산 (compute_total_equity가 한투 inquire-present-balance
        # tot_asst_amt를 우선해 매수 직후 KIS cash 갱신 지연 케이스를 보정).
        try:
            us_bal_for_eq = self.client.get_us_balance() if self.us_stocks else {}
            fx_for_eq = self.client.get_usd_krw_rate() if self.us_stocks else 0.0
            eq = compute_total_equity(balance, us_bal_for_eq, fx_for_eq)
            total_equity = eq["total"]
            deposits = (self.tracker.get_total_deposits()
                        if hasattr(self.tracker, "get_total_deposits") else 0)
            pnl_vs = compute_pnl_vs_deposit(total_equity, deposits)
        except Exception:
            total_equity = cash + total_eval
            deposits, pnl_vs = 0, {"pnl": 0, "pnl_pct": 0.0}

        logger.info("── 자산 현황 ──")
        logger.info("  총 자산 (KR+US): %s원", f"{total_equity:,}")
        logger.info("  KR: 예수금 %s원 + 평가 %s원", f"{cash:,}", f"{total_eval:,}")
        if deposits > 0:
            logger.info("  누적 입금: %s원 → 입금대비 %s원 (%+.2f%%)",
                        f"{deposits:,}", f"{pnl_vs['pnl']:+,}", pnl_vs["pnl_pct"])
        logger.info("  누적 실현손익 (확정): %s원", f"{realized_total['total_realized_pnl']:+,}")
        logger.info("  미실현 평가손익 (KR 미확정): %s원", f"{balance['total_profit']:+,}")
        logger.info("── 리스크 현황 ──")
        logger.info("  전략: %s", self.strategy.name)
        logger.info("  긴급 홀딩: %s", "ON — " + risk_status["emergency_reason"] if risk_status["emergency_hold"] else "OFF")
        logger.info("  전략 교체 이력: %d회", len(risk_status["strategy_switches"]))

        if long_term:
            self.tracker.get_long_term_total_cost()
            logger.info("── 장기투자 (%.0f%% 한도) ──", self.long_term_ratio * 100)
            for lt in long_term:
                logger.info("  %s %d주 | 사유: %s", lt["name"], lt["qty"], lt.get("reason", "-")[:60])

        # 미국 주식 잔고
        if self.us_stocks:
            try:
                us_bal = self.client.get_us_balance()
                logger.info("── US 자산 ──")
                logger.info("  예수금: $%.2f", us_bal["cash_usd"])
                for h in us_bal.get("holdings", []):
                    logger.info("  %s(%s): %d주 | $%.2f → $%.2f | %+.1f%%",
                                h["name"], h["ticker"], h["qty"],
                                h["avg_price"], h["current_price"], h["profit_rate"])
            except Exception:
                logger.warning("US 잔고 조회 실패")

        # 통합 상태 스냅샷 — 한 화면 요약 출력 + data/status.json 기록(웹/CLI 공용 소스)
        try:
            from zusik.reporting.status_snapshot import render_status_text
            print(render_status_text(self._write_status_snapshot()))
        except Exception:
            logger.debug("상태 요약 출력 실패", exc_info=True)


    def start(self):
        # Discord 봇 시작
        from zusik.clients.discord_bot import start_discord_bot
        start_discord_bot(self)

        logger.info(
            "봇 시작 — %d분 간격 | KR %d종목 | US %d종목 | 암호화폐 %d종목",
            self.interval, len(self.kr_stocks), len(self.us_stocks), len(self.crypto_tickers),
        )
        logger.info("  KR: 09:00~15:20 | US: 22:30~06:00 | 암호화폐: 그 외 전체 (24/7)")
        logger.info("  크로스시그널: US→KR | 적립 타이밍 알림 | 배당 캡처")
        logger.info("  웹 명령: http://localhost:7777")
        if self.risk.is_emergency_hold():
            logger.warning("긴급 홀딩 모드 (사유: %s)", self.risk.get_emergency_reason())

        # 초기 실행도 스레드 (Claude 호출이 메인 루프 블록하지 않게)
        import threading

        def _tick_async():
            threading.Thread(target=self.tick, daemon=True).start()

        _tick_async()
        self.run_once()

        schedule.every(1).minutes.do(_tick_async)
        schedule.every(self.interval).minutes.do(self.run_once)
        # 빠른 익절 서브루프 (40초) — 5분 run_once 사이 급등 익절 놓침 보완.
        # 보유종목 익절/트레일링/본전 보호만, Claude 없이. 자기중복은 _fast_scan_lock으로 방지.
        schedule.every(40).seconds.do(self._fast_exit_scan)
        # 빠른 진입 서브루프 (기본 2분) — 5분 run_once 사이 급등 돌파/과매도 반등 진입 놓침 보완.
        # 로컬 신호만(Claude 없이), _handle_buy의 모든 안전 게이트 통과. 자기중복은 _fast_entry_lock으로 방지.
        _fe_cfg = (self.config.get("fast_entry", {}) or {})
        if _fe_cfg.get("enabled", True):
            schedule.every(max(30, int(_fe_cfg.get("interval_seconds", 120)))).seconds.do(self._fast_entry_scan)
        # 실시간 진입 트리거(이벤트 드리븐) — WS 급등 틱이 울린 벨을 3초마다 드레인해 즉시 진입스캔.
        _rt_cfg = (self.config.get("realtime", {}) or {})
        if _rt_cfg.get("enabled", True) and _rt_cfg.get("entry_enabled", False):
            self._realtime_entry_setup()
            schedule.every(3).seconds.do(self._drain_realtime_entry)

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("봇 종료 (Ctrl+C)")


_LEARNED_PARAMS_FILE = os.path.join("data", "learned_params.json")
# 다년 일봉 캘리브레이션(calibrate_from_history.py)이 학습해 기록하는 청산 파라미터.
# 학습 → 재시작 시 자동 대응 루프의 적용 지점. 안전을 위해 화이트리스트 키만 오버레이.
_LEARNED_KEYS = ("profit_ladder", "breakeven_giveback_cap",
                 "breakeven_arm_pct", "breakeven_min_floor")


def _apply_learned_params(config: dict) -> dict:
    """data/learned_params.json의 캘리브레이션 결과를 position 설정에 최종 오버레이.

    화이트리스트 키만 반영(안전). 파일 없으면 무동작. 재실행 시 자동 갱신.
    """
    if not os.path.exists(_LEARNED_PARAMS_FILE):
        return config
    try:
        with open(_LEARNED_PARAMS_FILE, encoding="utf-8") as f:
            learned = json.load(f)
    except Exception as e:
        logger.warning("학습 파라미터 로드 실패: %s — 무시", e)
        return config
    pos = config.setdefault("position", {})
    applied = [k for k in _LEARNED_KEYS if k in learned]
    for k in applied:
        pos[k] = learned[k]
    if applied:
        logger.info("학습 파라미터 적용(%s): %s",
                    learned.get("calibrated_at", "?"), applied)
    return config


def _deep_merge(base: dict, over: dict) -> dict:
    """over 를 base 에 재귀 병합 (dict 는 깊게, 그 외는 덮어쓰기). base 를 갱신해 반환."""
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _apply_local_overrides(config: dict, path: str) -> dict:
    """config.local.yaml(사용자 로컬 오버라이드)을 최종 병합. configtool.py 가 관리.

    config.yaml(주석 보존 기본값)은 건드리지 않고, 사용자 설정은 별도 파일로 분리해
    깊은 병합한다. 사용자 명시 설정이므로 우선순위 최상(학습값 위). 파일 없으면 무동작.
    """
    base_dir = os.path.dirname(os.path.abspath(path)) or "."
    local_path = os.path.join(base_dir, "config.local.yaml")
    if not os.path.exists(local_path):
        return config
    try:
        with open(local_path, encoding="utf-8") as f:
            local = yaml.safe_load(f) or {}
        if isinstance(local, dict) and local:
            _deep_merge(config, local)
            logger.info("로컬 오버라이드 적용(config.local.yaml): %s", sorted(local.keys()))
    except Exception as e:
        logger.warning("config.local.yaml 로드 실패: %s — 무시", e)
    return config


def load_config(path: str = "config.yaml") -> dict:
    from zusik.core.trading_mode import apply_mode
    from zusik.core.performance_trainer import PerformanceTrainer
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config = apply_mode(config)
    # 메리트/디메리트 누적 반영
    trainer = PerformanceTrainer(config)
    config = trainer.apply_adjustments(config)
    # 다년 일봉 학습 캘리브레이션 오버레이 (있을 때만)
    config = _apply_learned_params(config)
    # 사용자 로컬 오버라이드 최종 병합 (configtool.py 가 관리, 명시 설정이 최우선)
    config = _apply_local_overrides(config, path)
    return config
