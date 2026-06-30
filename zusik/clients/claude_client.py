from __future__ import annotations
"""멀티 AI 클라이언트 — 용도별 모델 분배.

용도별 모델:
  장전 종목 선정 / 장후 평가  → Claude Opus (최고급, 하루 5건)
  중요 매매 판단              → Claude Sonnet (고급, 하루 10건)
  실시간 퀀트/센티멘트        → agy / Codex / Haiku (저가, 하루 100+건)

티어:
  "premium"  → Claude Opus (종목 선정, 일일 리포트)
  "hard"     → Claude Sonnet (매수/매도 최종 판단)
  "medium"   → Codex / Haiku (펀더멘털 분석)
  "easy"     → agy / Codex (퀀트, 센티멘트)
"""

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime

logger = logging.getLogger(__name__)

# LLM CLI 하위 프로세스에 넘기지 않을 시크릿 env 접두사.
# 분석/웹검색 CLI 는 거래소·메신저 키가 전혀 필요 없다. 외부 텍스트(뉴스/종목명) 프롬프트
# 인젝션이 agent CLI 의 셸/파일 도구를 깨워 `env` 를 덤프해도 계좌 탈취용 시크릿이
# 환경에 없도록 제거한다(fail-safe). ANTHROPIC_API_KEY 는 claude CLI 인증에 쓰일 수 있어
# 보존한다. 로컬 도구 접근 자체는 _run_claude 의 --disallowedTools 로 별도 차단한다.
_SECRET_ENV_PREFIXES = ("KIS_", "UPBIT_", "DISCORD_", "TELEGRAM_", "SLACK_")

# claude CLI 가 bypassPermissions 로 무인 실행되더라도 로컬 파일/셸 도구는 차단.
# (.env·data/kis_token.json 읽기, 소스/설정 변조, 임의 명령 실행 경로를 원천 제거.)
# 웹검색은 별도 --allowedTools(WebSearch,WebFetch)로만 허용된다.
_CLAUDE_DISALLOWED_TOOLS = "Bash,Read,Write,Edit,MultiEdit,NotebookEdit"


_child_env_cache: dict = {}


def _child_env() -> dict:
    """CLI 하위 프로세스용 env — 거래소/메신저 시크릿을 제거한 사본.

    필터 결과는 런타임 중 불변이므로 os.environ 객체별로 1회만 계산해 캐시한다(LLM 호출마다
    전체 env 재필터링 회피). 테스트가 os.environ을 다른 dict로 패치하면 id가 달라져 자동 재계산.
    """
    key = id(os.environ)
    cached = _child_env_cache.get(key)
    if cached is None:
        cached = {k: v for k, v in os.environ.items()
                  if not k.startswith(_SECRET_ENV_PREFIXES)}
        _child_env_cache.clear()   # os.environ 교체 시 엔트리 누적 방지 (현재 env만 유지)
        _child_env_cache[key] = cached
    return cached


def _cli_available(cmd):
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _read_merged_cfg() -> dict:
    """config.yaml + config.local.yaml 를 깊은 병합해 읽는다 (ai_providers 등 설정용).

    configtool.py / setup.sh 마법사는 사용자 설정을 config.local.yaml 에 쓰므로, 여기서도
    병합본을 봐야 disable_* 와 플랜 한도가 실제로 적용된다. load_config()는 mode/학습 등
    부수효과가 커서 단순 설정 읽기엔 부적합 → 가벼운 dict 병합만 한다(순환 import 없음)."""
    import yaml
    from zusik.paths import config_path

    def _merge(base: dict, over: dict) -> dict:
        for k, v in (over or {}).items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _merge(base[k], v)
            else:
                base[k] = v
        return base

    cfg: dict = {}
    for name in ("config.yaml", "config.local.yaml"):
        try:
            with open(config_path(name), encoding="utf-8") as f:
                _merge(cfg, yaml.safe_load(f) or {})
        except Exception:
            pass
    return cfg


# ── claude 일시정지 (사용자 쿼터 보호) ──
# config ai_providers.claude_pause_until('YYYY-MM-DD HH:MM', KST) 전까지 claude 호출 0.
# 값은 정적이라 1회 파싱 캐시하고 now 만 매번 비교 → 지정 시각이 지나면 자동 재개(재시작 불요).
_claude_pause_cache = "UNSET"


def _claude_pause_until():
    global _claude_pause_cache
    if _claude_pause_cache == "UNSET":
        _claude_pause_cache = None
        try:
            cfg = _read_merged_cfg()
            v = str((cfg.get("ai_providers") or {}).get("claude_pause_until") or "").strip()
            if v:
                _claude_pause_cache = datetime.strptime(v, "%Y-%m-%d %H:%M")
        except Exception:
            _claude_pause_cache = None
    return _claude_pause_cache


def _claude_paused() -> bool:
    """지정 시각 전이면 True → claude_* 호출 차단(codex/agy/로컬은 무관). 경과 시 자동 False."""
    until = _claude_pause_until()
    return bool(until and datetime.now() < until)


_cfg_limits_cache: dict | None = None


def _config_daily_limits() -> dict:
    """config(.yaml+.local) 의 api_cost.daily_limits 오버라이드 (1회 캐시).

    setup.sh AI 요금제 마법사 / configtool 이 쓴 플랜별 한도가 provider 라우팅(_check_limit)에도
    먹게 한다. 비어 있으면 모듈 DAILY_LIMITS 기본값을 그대로 쓴다. 정적 값이라 캐시(재시작 반영)."""
    global _cfg_limits_cache
    if _cfg_limits_cache is None:
        try:
            raw = (_read_merged_cfg().get("api_cost") or {}).get("daily_limits") or {}
            _cfg_limits_cache = {k: int(v) for k, v in raw.items()}
        except Exception:
            _cfg_limits_cache = {}
    return _cfg_limits_cache


def _effective_limit(provider: str, default: int = 999) -> int:
    """provider 의 유효 일일 한도 = config 오버라이드 우선, 없으면 모듈 DAILY_LIMITS.

    DAILY_LIMITS 는 매번 새로 참조(patch.dict 테스트/런타임 변경 honoring)."""
    from zusik.core.cost_optimizer import DAILY_LIMITS
    cfg = _config_daily_limits()
    if provider in cfg:
        return cfg[provider]
    return DAILY_LIMITS.get(provider, default)


def _check_limit(provider: str) -> bool:
    """호출 전 한도 체크.

    fail-closed 수정: 이전엔 예외 시 무조건 True(fail-open)라
    api_costs.json 동시 쓰기 파손 → JSONDecodeError → 한도 0인 Claude가
    하루 ~7콜 누수됐다. 수정:
      1. 한도 ≤ 0 → 파일 I/O 없이 즉시 False (비활성 provider는 어떤 상태에서도 차단)
      2. 예외 시 claude_* 는 False(fail-closed, 쿼터 보호 우선),
         codex/agy는 True(fail-open 유지 — 파일 파손이 봇 전체를 멈추면 안 됨)
    """
    # 사용자 쿼터 보호: 지정 시각 전까지 claude 호출 0 (codex/agy/로컬로 폴백)
    if provider.startswith("claude") and _claude_paused():
        return False
    if _effective_limit(provider) <= 0:
        return False
    try:
        if os.path.exists("data/api_costs.json"):
            with open("data/api_costs.json") as f:
                costs = json.load(f)
            today = datetime.now().strftime("%Y-%m-%d")
            daily = costs.get("daily", {}).get(today, {})
            if daily.get(provider, 0) >= _effective_limit(provider):
                return False
            if daily.get("total", 0) >= _effective_limit("total"):
                return False
    except Exception:
        if provider.startswith("claude"):
            return False
    return True


def _record_call(provider: str):
    """호출 카운트 기록."""
    if provider == "local":
        return  # 로컬 LLM 은 비용/쿼터가 없어 api_costs(total 캡)에 집계하지 않는다.
    try:
        costs = {}
        if os.path.exists("data/api_costs.json"):
            with open("data/api_costs.json") as f:
                costs = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        costs.setdefault("daily", {}).setdefault(today, {})
        costs["daily"][today][provider] = costs["daily"][today].get(provider, 0) + 1
        costs["daily"][today]["total"] = costs["daily"][today].get("total", 0) + 1
        os.makedirs("data", exist_ok=True)
        # 쓰기 (tmp + os.replace): 비원자 쓰기 중 동시 읽기가
        # 파손 JSON을 읽어 _check_limit 예외 → fail-open 한도 누수의 근본 원인이었음
        tmp = "data/api_costs.json.tmp"
        with open(tmp, "w") as f:
            json.dump(costs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, "data/api_costs.json")
    except Exception:
        pass


# ── 라운드로빈 부하 분산 ──
# easy/medium 티어에서 같은 사이클 내 여러 애널리스트가 동시에 호출될 때
# 첫 번째 후보(agy 또는 codex)로 몰리는 걸 방지.
# 모듈 전역 카운터로 매 호출마다 시작 인덱스를 회전.
_TIER_ROTATION_IDX = {"easy": 0, "medium": 0, "hard": 0, "balanced": 0}


def _next_rotation(tier: str) -> int:
    cur = _TIER_ROTATION_IDX.get(tier, 0)
    _TIER_ROTATION_IDX[tier] = cur + 1
    return cur


# ── LLM 가용성 추적 ──
# message()가 단일 관문이라 여기서 실패/복구를 집계해 data/llm_health.json 에 영속화.
# 봇(tick의 _check_llm_health)·외부 워치독·status 스냅샷이 공용으로 읽는다.
# 연속 실패 ≥ 임계면 down. 로컬 전략은 계속 돌지만 "AI 분석이 왜 안 오지"를 유저가 알 수 있게.
_LLM_FAIL_THRESHOLD = 3
_llm_health_cache: dict | None = None


def _llm_health_path() -> str:
    from zusik.paths import data_path
    return data_path("llm_health.json")


def _default_llm_health() -> dict:
    return {"status": "ok", "consecutive_fail": 0, "since": "",
            "last_reason": "", "last_update": ""}


def _load_llm_health() -> dict:
    global _llm_health_cache
    if _llm_health_cache is not None:
        return _llm_health_cache
    try:
        with open(_llm_health_path(), encoding="utf-8") as f:
            data = json.load(f)
        base = _default_llm_health()
        base.update({k: data.get(k, base[k]) for k in base})
        _llm_health_cache = base
    except Exception:
        _llm_health_cache = _default_llm_health()
    return _llm_health_cache


def get_llm_health() -> dict:
    """현재 LLM 가용성 스냅샷 (status/consecutive_fail/since/last_reason)."""
    return dict(_load_llm_health())


def _short_reason(result: str) -> str:
    """격하 응답에서 짧은 사유 추출 (시크릿/장문 차단)."""
    r = (result or "").strip()
    try:
        obj = json.loads(r)
        r = str(obj.get("reasoning", "") or obj.get("error", "") or r)
    except Exception:
        pass
    return r.replace("\n", " ")[:80]


def _record_llm_health(degraded: bool, result: str) -> None:
    """message() 결과 1건을 가용성 상태에 반영 + 파일 영속화. 무예외."""
    try:
        h = _load_llm_health()
        now = datetime.now().isoformat()
        if degraded:
            h["consecutive_fail"] = int(h.get("consecutive_fail", 0)) + 1
            if h["consecutive_fail"] >= _LLM_FAIL_THRESHOLD and h.get("status") != "down":
                h["status"] = "down"
                h["since"] = now
                h["last_reason"] = _short_reason(result)
        else:
            if h.get("status") == "down":
                h["status"] = "ok"
                h["since"] = now
                h["last_reason"] = ""
            h["consecutive_fail"] = 0
        h["last_update"] = now
        from zusik.paths import write_json_atomic
        write_json_atomic(_llm_health_path(), h)
    except Exception:
        pass


class ClaudeClient:
    """용도별 AI 모델 분배."""

    def __init__(self, api_key: str = "", prefer_cli: bool = True, provider: str = "auto",
                 disable_codex: bool | None = None, disable_agy: bool | None = None):
        # config 1회 로드 — ai_providers.{disable_codex, disable_agy, local_*} 공용.
        # config.yaml + config.local.yaml 병합본을 읽어 setup.sh 마법사/ configtool 설정이 먹게 한다.
        _cfg = _read_merged_cfg()
        ap = _cfg.get("ai_providers", {}) if isinstance(_cfg, dict) else {}
        if disable_codex is None:
            disable_codex = bool(ap.get("disable_codex", False))
        if disable_agy is None:
            disable_agy = bool(ap.get("disable_agy", False))

        self._has_claude = _cli_available("claude")
        self._has_codex = _cli_available("codex") and not disable_codex
        # agy(Antigravity) — 구글 Gemini 계열 provider. `agy -p` 비대화형 모드 사용.
        self._has_agy = _cli_available("agy") and not disable_agy
        if disable_codex:
            logger.info("codex CLI 비활성화 (config.ai_providers.disable_codex=true)")
        if disable_agy:
            logger.info("agy(Antigravity) CLI 비활성화 (config.ai_providers.disable_agy=true)")

        # 로컬 LLM(Ollama) — 기본 OFF. 켜면 저렴 티어가 로컬 우선(쿼터/비용 0).
        # docs/LOCAL_LLM.md 참고. CLI 구독 운영자는 끌 필요 없이 false 유지.
        self._has_local = False
        self._local_enabled = bool(ap.get("local_enabled"))
        self._local_endpoint = ap.get("local_endpoint", "http://localhost:11434")
        self._local_model = (ap.get("local_model") or "").strip()  # 추천 기본값 없음 — 환경이 다양
        self._local_search_backend = ap.get("local_search_backend", "duckduckgo")
        self._local_searxng_url = ap.get("local_searxng_url", "http://localhost:8888")
        self._local_search_results = int(ap.get("local_search_results", 4))
        self._local_timeout = int(ap.get("local_timeout", 120))
        if self._local_enabled:
            if not self._local_model:
                logger.warning("local_enabled=true 이나 local_model 미지정 — 로컬 LLM 비활성 "
                               "(config 에 ai_providers.local_model 설정 필요)")
            else:
                try:
                    from zusik.clients.local_llm import local_llm_available
                    self._has_local = local_llm_available(self._local_endpoint)
                    if not self._has_local:
                        logger.warning("local_enabled=true 이나 Ollama 무응답(%s) — 로컬 LLM 비활성",
                                       self._local_endpoint)
                except Exception:
                    self._has_local = False

        available = []
        if self._has_agy: available.append("agy")
        if self._has_codex: available.append("codex")
        if self._has_claude:
            available.append("claude(일시정지)" if _claude_paused() else "claude(opus/sonnet/haiku)")
        if self._has_local: available.append(f"local({self._local_model})")
        logger.info("AI: %s", ", ".join(available) or "백엔드 없음")

    @property
    def is_cli(self) -> bool:
        # "LLM 백엔드 사용 가능?" 의미로 사용됨(stock_screener 등의 게이트) — 로컬 전용
        # 유저(CLI 없음)도 통과해야 분석이 돈다.
        return (self._has_claude or self._has_codex
                or self._has_agy or self._has_local)

    @property
    def provider_name(self) -> str:
        if self._has_claude: return "claude"
        if self._has_codex: return "codex"
        if self._has_agy: return "agy"
        return "local"

    @staticmethod
    def _is_failed(result: str) -> bool:
        """모델 응답이 실패 상태인지 (fallback 필요)."""
        if not result:
            return True
        bad = ("오류", "빈 응답", "타임아웃", "quota", "cooldown",
               "exhausted", "rate limit", "rate_limit")
        return any(k in result.lower() for k in bad) or any(k in result for k in ("오류", "빈 응답", "타임아웃"))

    # ── 로컬 LLM(Ollama) ──

    def _run_local(self, prompt: str, use_web_search: bool = False) -> str:
        """로컬 Ollama 호출(검색 주입 옵션). 실패 시 실패 JSON → 상위 폴백."""
        from zusik.clients.local_llm import run_local
        return run_local(
            prompt, endpoint=self._local_endpoint, model=self._local_model,
            use_web_search=use_web_search, search_backend=self._local_search_backend,
            searxng_url=self._local_searxng_url, max_results=self._local_search_results,
            timeout=self._local_timeout,
        )

    def _try_local(self, prompt: str, use_web_search: bool = False):
        """로컬이 활성이면 먼저 시도. 성공 시 응답, 비활성/실패 시 None(상위 폴백).

        getattr 기본 False — __init__ 을 우회(`__new__`)해 만든 인스턴스도 안전(테스트/특수 생성)."""
        if not getattr(self, "_has_local", False):
            return None
        result = self._run_local(prompt, use_web_search)
        return result if not self._is_failed(result) else None

    @staticmethod
    def _classify_degraded(result: str) -> bool:
        """응답이 '격하/불가' 상태인지 — _is_failed(쿼터/오류/타임아웃) + 한도소진 sentinel.

        message()가 모든 폴백을 거치고도 내놓은 최종 결과 기준. True가 연속되면 LLM 다운.
        """
        if ClaudeClient._is_failed(result):
            return True
        low = (result or "").lower()
        return ("한도 소진" in (result or "")) or ("한도소진" in (result or "")) \
            or ("ai 한도" in low)

    def message(self, prompt: str, max_tokens: int = 1500,
                use_web_search: bool = False, model: str = "",
                tier: str = "easy") -> str:
        """용도별 모델 자동 선택. 결과 가용성은 data/llm_health.json 에 집계."""
        result = self._dispatch(prompt, max_tokens, use_web_search, model, tier)
        _record_llm_health(self._classify_degraded(result), result)
        return result

    def _dispatch(self, prompt: str, max_tokens: int, use_web_search: bool,
                  model: str, tier: str) -> str:
        if tier == "premium":
            return self._call_premium(prompt, use_web_search)
        elif tier == "hard":
            return self._call_hard(prompt, use_web_search)
        elif tier == "balanced":
            return self._call_balanced(prompt, use_web_search)
        elif tier == "cheap_web":
            return self._call_cheap_web(prompt, use_web_search)
        elif tier == "medium":
            return self._call_medium(prompt)
        else:
            return self._call_easy(prompt)

    def _call_cheap_web(self, prompt: str, use_web_search: bool = False) -> str:
        """장전/장후 리포트용 저렴 tier(사용자 토큰 절감 요청).

        agy/codex 우선 (사용자 별도 구독 — Claude 쿼터 미소모) + web_search 유지.
        Claude는 두 CLI 모두 cooldown/한도 도달 시에만 haiku 폴백. sonnet/opus 완전 제외.
        로컬 LLM 활성 시 최우선(쿼터/비용 0).
        """
        local = self._try_local(prompt, use_web_search)
        if local is not None:
            return local
        candidates = []
        if self._has_agy and _check_limit("agy"):
            candidates.append(("agy", lambda: self._run_agy(prompt, use_web_search)))
        if self._has_codex and _check_limit("codex") and not self._is_codex_cooldown():
            candidates.append(("codex", lambda: self._run_codex(prompt, use_web_search)))
        if not candidates:
            # 셋 다 막혀야만 haiku (web_search 미지원, 텍스트만)
            if self._has_claude and _check_limit("claude_haiku"):
                candidates.append(("claude_haiku", lambda: self._run_claude(prompt, "haiku", False)))
            if not candidates:
                return '{"signal":"hold","confidence":0,"reasoning":"AI 한도 소진"}'
        start = _next_rotation("cheap_web") % len(candidates)
        ordered = candidates[start:] + candidates[:start]
        for name, fn in ordered:
            result = fn()
            if not self._is_failed(result):
                _record_call(name)
                return result
        return self._call_easy(prompt)

    def _call_balanced(self, prompt: str, use_web_search: bool = False) -> str:
        """Claude/Codex/agy(Antigravity) 멀티 CLI 균등 분배.

        사용자 의도: 셋 다 구독중이라 "한쪽 몰림" 방지하며 모두 활용.
        모듈 카운터 _next_rotation('balanced')로 시작 인덱스를 매 호출 회전 →
        동시 호출자가 4명이어도 1명씩 다른 CLI로 시작.
        cooldown/한도 도달 CLI는 후보에서 자동 제외.
        모두 실패 시 haiku 폴백.
        로컬 LLM 활성 시 최우선(쿼터/비용 0).
        """
        local = self._try_local(prompt, use_web_search)
        if local is not None:
            return local
        candidates = []
        if self._has_claude and _check_limit("claude_sonnet"):
            candidates.append((
                "claude_sonnet",
                lambda: self._run_claude(prompt, "sonnet", use_web_search),
            ))
        if self._has_codex and _check_limit("codex") and not self._is_codex_cooldown():
            candidates.append((
                "codex",
                lambda: self._run_codex(prompt, use_web_search),
            ))
        if self._has_agy and _check_limit("agy"):
            candidates.append((
                "agy",
                lambda: self._run_agy(prompt, use_web_search),
            ))

        if not candidates:
            # 전부 막혔으면 haiku로 빠짐
            return self._call_easy(prompt)

        start = _next_rotation("balanced") % len(candidates)
        ordered = candidates[start:] + candidates[:start]
        for name, fn in ordered:
            result = fn()
            if not self._is_failed(result):
                _record_call(name)
                return result
        # 셋 다 실패 → easy 폴백 (haiku 시도)
        return self._call_easy(prompt)

    def _call_premium(self, prompt: str, use_web_search: bool) -> str:
        """장전 종목 선정 / 장후 리포트 → Claude Opus → Sonnet → hard tier."""
        if self._has_claude and _check_limit("claude_opus"):
            result = self._run_claude(prompt, "opus", use_web_search)
            if not self._is_failed(result):
                _record_call("claude_opus")
                return result
        if self._has_claude and _check_limit("claude_sonnet"):
            result = self._run_claude(prompt, "sonnet", use_web_search)
            if not self._is_failed(result):
                _record_call("claude_sonnet")
                return result
        local = self._try_local(prompt, use_web_search)
        if local is not None:
            return local
        return self._call_hard(prompt, use_web_search)

    def _call_hard(self, prompt: str, use_web_search: bool) -> str:
        """중요 매매 판단 → Sonnet → Haiku → medium tier."""
        if self._has_claude and _check_limit("claude_sonnet"):
            result = self._run_claude(prompt, "sonnet", use_web_search)
            if not self._is_failed(result):
                _record_call("claude_sonnet")
                return result
        if self._has_claude and _check_limit("claude_haiku"):
            result = self._run_claude(prompt, "haiku", False)
            if not self._is_failed(result):
                _record_call("claude_haiku")
                return result
        local = self._try_local(prompt, use_web_search)
        if local is not None:
            return local
        return self._call_medium(prompt)

    def _call_medium(self, prompt: str) -> str:
        """퀀트/펀더멘털 보조 — Codex/agy/Haiku 라운드로빈.

        같은 사이클 동시 호출 분산을 위해 라운드로빈 도입.
        codex 첫 → 다음 호출 haiku 첫 → 다음 codex … 순서로 회전.
        로컬 LLM 활성 시 최우선(쿼터/비용 0).
        """
        local = self._try_local(prompt, False)
        if local is not None:
            return local
        candidates = []
        if self._has_codex and _check_limit("codex") and not self._is_codex_cooldown():
            candidates.append(("codex", lambda: self._run_codex(prompt)))
        if self._has_agy and _check_limit("agy"):
            candidates.append(("agy", lambda: self._run_agy(prompt)))
        if self._has_claude and _check_limit("claude_haiku"):
            candidates.append(("claude_haiku", lambda: self._run_claude(prompt, "haiku", False)))
        if not candidates:
            return self._call_easy(prompt)

        start = _next_rotation("medium") % len(candidates)
        ordered = candidates[start:] + candidates[:start]
        for name, fn in ordered:
            result = fn()
            if not self._is_failed(result):
                _record_call(name)
                return result
        return self._call_easy(prompt)

    def _call_easy(self, prompt: str) -> str:
        """센티멘트/일반 — agy/Codex 라운드로빈 + Haiku 최종 폴백.

        첫 후보에 몰리던 부하를 라운드로빈으로 분산.
        cooldown/한도 도달 CLI는 candidates에서 자동 제외.
        로컬 LLM 활성 시 최우선(쿼터/비용 0).
        """
        local = self._try_local(prompt, False)
        if local is not None:
            return local
        candidates = []
        if self._has_agy and _check_limit("agy"):
            candidates.append(("agy", lambda: self._run_agy(prompt)))
        if self._has_codex and _check_limit("codex") and not self._is_codex_cooldown():
            candidates.append(("codex", lambda: self._run_codex(prompt)))
        if self._has_claude and _check_limit("claude_haiku"):
            candidates.append(("claude_haiku", lambda: self._run_claude(prompt, "haiku", False)))

        if not candidates:
            return '{"signal":"hold","confidence":0,"reasoning":"AI 한도 소진"}'

        start = _next_rotation("easy") % len(candidates)
        ordered = candidates[start:] + candidates[:start]
        for name, fn in ordered:
            result = fn()
            if not self._is_failed(result):
                _record_call(name)
                return result
        return '{"signal":"hold","confidence":0,"reasoning":"AI 한도 소진"}'

    # ── CLI 호출 ──

    # alias → 200k context 풀 모델명 매핑.
    # 'opus' alias는 사용자 환경에 따라 'claude-opus-4-7[1m]'으로 해석돼
    # "Usage credits required for 1M context" 오류를 일으킬 수 있음 → 200k 버전 명시.
    _MODEL_FULL = {
        "opus":   "claude-opus-4-7",
        "sonnet": "claude-sonnet-4-6",
        "haiku":  "claude-haiku-4-5-20251001",
    }

    def _run_claude(self, prompt: str, model: str, use_web_search: bool) -> str:
        # 비대화형 봇 호출이라 권한 프롬프트가 뜨면 hang됨.
        # bypassPermissions + WebSearch/WebFetch 명시 허용으로 무인 자동 승인.
        full_model = self._MODEL_FULL.get(model, model)
        # bypassPermissions 로 무인 승인하되, 로컬 파일/셸 도구는 --disallowedTools 로 차단.
        # 분석은 텍스트(+선택적 웹검색)만 필요하므로 Bash/Read/Write 등은 줄 이유가 없다.
        cmd = ["claude", "-p", prompt, "--model", full_model,
               "--permission-mode", "bypassPermissions",
               "--disallowedTools", _CLAUDE_DISALLOWED_TOOLS]
        if use_web_search:
            cmd.extend(["--allowedTools", "WebSearch,WebFetch"])
        return self._exec(cmd, f"claude_{model}", timeout=300 if use_web_search else 150)

    def _run_codex(self, prompt: str, use_web_search: bool = False) -> str:
        # codex exec는 별도 --search 플래그가 없어 -c features.web_search=true로 활성화.
        # interactive `codex --search`와 동일한 native Responses web_search tool 노출.
        cmd = ["codex", "exec", "--full-auto", "--skip-git-repo-check",
               "-o", "/dev/stdout"]
        if use_web_search:
            cmd.extend(["-c", "features.web_search=true"])
        cmd.append(prompt)
        return self._exec(cmd, "codex", timeout=300 if use_web_search else 150)

    def _run_agy(self, prompt: str, use_web_search: bool = False) -> str:
        """Antigravity(agy) CLI — 구글 Gemini 계열 provider.

        `agy -p`(=--print) 비대화형 단발 응답 모드. --dangerously-skip-permissions 로 도구
        승인 프롬프트를 자동 승인(비대화형 봇이 멈추지 않게). agy 는 에이전트형이라 필요 시
        자체 웹 도구로 검색하므로 use_web_search 별 분기는 두지 않고 타임아웃만 늘린다.
        인증은 ~/.gemini(HOME) — _child_env 가 HOME 보존하므로 동작.
        """
        cmd = ["agy", "-p", prompt, "--dangerously-skip-permissions"]
        return self._exec(cmd, "agy", timeout=300 if use_web_search else 180)

    # codex 세션 만료(로그인 필요) 시 자동 비활성화. 사용자가 `codex login` 한 번 하면 즉시
    # 복구되므로 cooldown은 짧게(15분) — 죽은 CLI를 매 호출 두드려 rotation 슬롯·시간을 낭비하고
    # 로그를 더럽히는 것만 차단하고, 15분마다 자동 재시도해 재로그인을 빠르게 흡수한다.
    _CODEX_COOLDOWN_FILE = os.path.join(tempfile.gettempdir(), "codex_cooldown_until.txt")
    # 세션 만료/토큰 갱신 실패 시 codex stderr에 나타나는 마커들.
    _CODEX_AUTH_DEAD_MARKERS = (
        "session has ended", "please log in again", "app_session_terminated",
        "failed to refresh token", "not logged in", "unauthorized",
    )
    # 사용량 한도(쿼터) 소진 시 codex stderr 마커 — 세션은 살아있고 한도만 리셋되면 복귀.
    _CODEX_QUOTA_MARKERS = (
        "usage limit", "hit your usage limit", "purchase more credits",
        "upgrade to pro",
    )

    def _is_codex_cooldown(self) -> bool:
        import os, time
        try:
            if os.path.exists(self._CODEX_COOLDOWN_FILE):
                with open(self._CODEX_COOLDOWN_FILE) as f:
                    until = float(f.read().strip() or 0)
                if time.time() < until:
                    return True
        except Exception:
            pass
        return False

    def _set_codex_cooldown(self, minutes: float = 15.0):
        import time
        try:
            with open(self._CODEX_COOLDOWN_FILE, "w") as f:
                f.write(str(time.time() + minutes * 60))
        except Exception:
            pass

    def _exec(self, cmd: list, name: str, timeout: int = 150) -> str:
        """CLI 실행. 빈 응답/타임아웃 시 1회 재시도 후 실패 반환.

        stdin=DEVNULL을 명시해 codex/claude CLI가 stdin 입력 대기로 멈추는 경로 차단
        ("Reading additional input from stdin..." stderr + 빈 stdout 즉시 반환 버그 방지).
        """
        for attempt in range(2):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    # 거래소/메신저 시크릿을 제거한 최소 env (_child_env).
                    # MAX_THINKING_TOKENS=0: claude CLI extended thinking 비활성
                    # 사용자 요청 "deep thinking 자제"). 매매 분석은 구조화 JSON 응답이라
                    # thinking 토큰이 품질 기여 대비 쿼터 소모가 큼. claude 외 CLI엔 무해.
                    env={**_child_env(), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                         "MAX_THINKING_TOKENS": "0"},
                )
                output = result.stdout.strip()
                stderr = (result.stderr or "").strip()
                slow = stderr.lower()
                # codex 세션 만료(로그인 필요) 감지 → 짧은 cooldown + 명확한 알림
                # 죽은 CLI를 매 호출 두드리지 않게 하고, 운영자에게 `codex login` 안내.
                if name == "codex" and any(m in slow for m in self._CODEX_AUTH_DEAD_MARKERS):
                    if not self._is_codex_cooldown():  # 진입 시 1회만 시끄럽게
                        logger.error(
                            "codex 세션 만료 — 'codex login' 재실행 필요. "
                            "15분간 codex 건너뛰고 claude로 분석 진행 (재로그인하면 자동 복구).")
                    self._set_codex_cooldown(15.0)
                    return '{"signal":"hold","confidence":0,"reasoning":"codex 세션 만료(로그인 필요)"}'
                # codex 사용량 한도(쿼터) 소진 — 세션 살아있고 한도 리셋되면 복귀.
                # 60분 cooldown 으로 헛호출 차단, 한도 회복 시 자동 재시도.
                if name == "codex" and any(m in slow for m in self._CODEX_QUOTA_MARKERS):
                    if not self._is_codex_cooldown():
                        logger.error(
                            "codex 사용량 한도 도달 — 한도 리셋/크레딧 충전 필요. "
                            "60분간 codex 건너뛰고 claude로 진행 (한도 회복 시 자동 복구).")
                    self._set_codex_cooldown(60.0)
                    return '{"signal":"hold","confidence":0,"reasoning":"codex 사용량 한도"}'
                if output:
                    return output
                logger.warning("%s: 빈 응답 (시도 %d/2) stderr=%s", name, attempt + 1, stderr[:200])
                if attempt == 0:
                    continue
            except subprocess.TimeoutExpired:
                logger.warning("%s: 타임아웃 (시도 %d/2)", name, attempt + 1)
                if attempt == 0:
                    continue
                return '{"signal":"hold","confidence":0,"reasoning":"타임아웃"}'
            except Exception as e:
                logger.warning("%s: %s", name, e)
                return '{"signal":"hold","confidence":0,"reasoning":"오류"}'
        return '{"signal":"hold","confidence":0,"reasoning":"빈 응답 2회"}'
