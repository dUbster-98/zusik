from __future__ import annotations
"""로컬 LLM(Ollama) provider + 웹검색 주입 — API 비용/쿼터 0.

API 대형 LLM(claude/codex/agy) 대신 로컬 모델로 분석을 돌리고 싶은 사용자를 위한 백엔드.
운영 기본값은 OFF(`config.yaml: ai_providers.local_enabled: false`)이며, 켜면
`claude_client.ClaudeClient` 의 저렴 티어가 로컬을 우선 사용한다.

설계 원칙:
  - 새 pip 의존성 0. 이미 설치된 `requests` 만 사용 → Python 3.8 그대로 호환.
  - 로컬 모델 자체엔 웹검색이 없으므로, `use_web_search` 시 검색 결과 스니펫을 프롬프트
    상단에 주입하는 RAG 방식으로 "검색 가능"을 구현(best-effort).
  - 무예외(fail-safe): 어떤 단계가 실패해도 CLI provider 들과 동일한 실패 JSON 문자열을
    반환해 `ClaudeClient._is_failed` 가 상위 폴백으로 넘기게 한다(봇이 멈추지 않음).

검색 백엔드(키 불필요):
  - duckduckgo : 컨테이너/계정 없이 HTML 엔드포인트 조회(가장 단순, 대표적 무키 선택).
  - searxng    : 자체호스팅 메타서치의 JSON API(`{url}/search?format=json`) — 봇이 반복
                 호출해도 차단/레이트리밋이 없어 안정적(Docker 1컨테이너 상주).
  - none       : 검색 비활성(프롬프트만으로 응답).
키 기반 엔진(Brave/Tavily 등)은 docs/LOCAL_LLM.md 의 확장 안내를 참고.
"""

import html
import logging
import re

logger = logging.getLogger(__name__)

_FAIL = '{"signal":"hold","confidence":0,"reasoning":"%s"}'

# 검색 조회 시 봇임을 숨기지 않되 일반 UA로 — 일부 엔진이 빈 UA 를 막음.
_UA = "Mozilla/5.0 (X11; Linux x86_64) zusik-local-llm/1.0"


def local_llm_available(endpoint: str, timeout: float = 2.0) -> bool:
    """Ollama 가 응답하는지(모델 목록 엔드포인트) 빠르게 확인."""
    try:
        import requests
        r = requests.get(f"{endpoint.rstrip('/')}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _query_from_prompt(prompt: str, limit: int = 120) -> str:
    """긴 분석 프롬프트에서 검색어를 best-effort 로 추출(첫 비어있지 않은 줄, 절단)."""
    for line in prompt.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return prompt.strip()[:limit]


def _search_duckduckgo(query: str, max_results: int, timeout: float) -> list:
    """DuckDuckGo HTML 엔드포인트 조회(키/계정 불필요). 의존성 없이 정규식 파싱(best-effort)."""
    import requests
    r = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query}, headers={"User-Agent": _UA}, timeout=timeout,
    )
    if r.status_code != 200:
        return []
    out = []
    # 결과 링크 + 스니펫을 느슨하게 추출(HTML 구조 변동에 깨지면 빈 리스트 → 검색 컨텍스트 없음).
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', r.text, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
    for i in range(min(len(titles), max_results)):
        title = html.unescape(re.sub(r"<[^>]+>", "", titles[i])).strip()
        snip = ""
        if i < len(snippets):
            snip = html.unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()
        if title:
            out.append({"title": title, "snippet": snip})
    return out


def _search_searxng(query: str, searxng_url: str, max_results: int, timeout: float) -> list:
    """SearXNG JSON API 조회(`{url}/search?format=json`). 키 불필요, 안정적."""
    import requests
    r = requests.get(
        f"{searxng_url.rstrip('/')}/search",
        params={"q": query, "format": "json"},
        headers={"User-Agent": _UA}, timeout=timeout,
    )
    if r.status_code != 200:
        return []
    data = r.json() or {}
    out = []
    for item in (data.get("results") or [])[:max_results]:
        title = (item.get("title") or "").strip()
        snip = (item.get("content") or "").strip()
        if title:
            out.append({"title": title, "snippet": snip})
    return out


def web_search(query: str, backend: str = "duckduckgo", searxng_url: str = "",
               max_results: int = 4, timeout: float = 15.0) -> str:
    """검색 결과를 프롬프트 주입용 텍스트 블록으로 반환. 실패 시 빈 문자열(무예외)."""
    try:
        if backend == "searxng":
            results = _search_searxng(query, searxng_url, max_results, timeout)
        elif backend == "duckduckgo":
            results = _search_duckduckgo(query, max_results, timeout)
        else:
            return ""
        if not results:
            return ""
        lines = ["[웹검색 결과 — 참고용]"]
        for i, it in enumerate(results, 1):
            line = f"{i}. {it['title']}"
            if it.get("snippet"):
                line += f" — {it['snippet']}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        logger.debug("web_search 실패(%s): %s", backend, e)
        return ""


def run_local(prompt: str, *, endpoint: str, model: str, use_web_search: bool = False,
              search_backend: str = "duckduckgo", searxng_url: str = "",
              max_results: int = 4, timeout: int = 120) -> str:
    """Ollama 로 1회 생성. 실패 시 CLI provider 들과 동일한 실패 JSON(상위 폴백 트리거)."""
    try:
        full = prompt
        if use_web_search and search_backend and search_backend != "none":
            ctx = web_search(_query_from_prompt(prompt), search_backend, searxng_url, max_results)
            if ctx:
                full = ctx + "\n\n" + prompt
        import requests
        resp = requests.post(
            f"{endpoint.rstrip('/')}/api/generate",
            json={"model": model, "prompt": full, "stream": False,
                  "options": {"temperature": 0.3}},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning("로컬 LLM HTTP %s (model=%s)", resp.status_code, model)
            return _FAIL % "로컬 LLM 오류"
        out = ((resp.json() or {}).get("response") or "").strip()
        return out or (_FAIL % "로컬 LLM 빈 응답")
    except Exception as e:
        logger.warning("로컬 LLM 호출 실패: %s", e)
        return _FAIL % "로컬 LLM 오류"
