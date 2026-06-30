from __future__ import annotations
"""운영 헬스체크 — 셋업 직후/장 시작 전에 KIS·AI·메신저·경로가 정상인지 한 번에 확인.

실행:
    python main.py --healthcheck            # 사람이 읽는 점검 리포트
    python main.py --healthcheck --notify   # 결과 요약을 메신저로도 전송 (장전 cron 용)

설계 원칙:
  - 시크릿 값은 절대 출력하지 않는다(설정됨/없음만). 정보 탈취 위험 차단.
  - 초보자도 읽도록 각 항목에 한국어 결과와 해결 힌트를 붙인다.
  - 종료코드: 핵심 점검(파이썬/KIS 키/KIS API/경로) 실패 시 1, 그 외 0 → cron/CI 에서 활용.
"""
import os
import sys

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_TAG = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


def _line(status: str, name: str, detail: str = "") -> str:
    return f"{_TAG[status]} {name}" + (f" — {detail}" if detail else "")


def _probe_ai_providers(ai) -> tuple:
    """provider(agy/codex/claude) 마다 실제 1회 호출해 (status, "AI", detail) 산출.

    과거 버그: _has_*(CLI 설치됨)로 목록 만들고 통합 1회 probe → codex 가 죽어도 agy/claude 가
    응답하면 전체 "OK"로 표시되고 codex 가 정상처럼 보였다. 이제 provider 개별 호출 + 호출 후
    cooldown 재확인으로 판정한다(codex 쿼터 sentinel 은 _is_failed 가 못 잡으므로 cooldown
    설정 여부가 더 견고한 신호). 이미 cooldown 이면 호출 없이 불가 처리.
    """
    from zusik.clients.claude_client import ClaudeClient
    Q = "한 단어로만 답해: OK"
    probes = []  # (name, run_fn, cooldown_fn|None)
    if getattr(ai, "_has_agy", False):
        probes.append(("agy", lambda: ai._run_agy(Q), None))
    if getattr(ai, "_has_codex", False):
        probes.append(("codex", lambda: ai._run_codex(Q), ai._is_codex_cooldown))
    if getattr(ai, "_has_claude", False):
        probes.append(("claude", lambda: ai._run_claude(Q, "haiku", False), None))

    if not probes and not os.getenv("ANTHROPIC_API_KEY"):
        return (WARN, "AI", "CLI/키 없음 — 로컬 전략만 작동 (claude/codex/agy 설치 또는 ANTHROPIC_API_KEY)")

    oks, bad = [], []
    for name, run, cd in probes:
        if cd and cd():                      # 이미 cooldown(최근 실패) → 호출 없이 불가
            bad.append(f"{name}(cooldown 제한)")
            continue
        try:
            r = run()
            failed = ClaudeClient._is_failed(r) or bool(cd and cd())  # 호출 후 cooldown 설정됐으면 실패
            (oks if (r and not failed) else bad).append(name if (r and not failed) else f"{name}(불가)")
        except Exception:
            bad.append(f"{name}(예외)")
    if not bad:
        return (OK, "AI", f"{', '.join(oks)} — 전부 응답 OK")
    if oks:
        return (WARN, "AI", f"정상: {', '.join(oks)} / 불가: {', '.join(bad)} (login/쿼터 확인)")
    return (FAIL, "AI", f"전 provider 불가: {', '.join(bad)} — 로컬 전략만 작동")


def _collect_results(client, config):
    """KIS/AI/메신저/경로 점검만 수행 → (results, mode). 출력·전송은 호출측 담당."""
    results = []  # list[(status, name, detail)]

    # 1) Python 버전 (운영 서버는 3.8 가정)
    v = sys.version_info
    if v >= (3, 8):
        results.append((OK, "Python", f"{v.major}.{v.minor}.{v.micro}"))
    else:
        results.append((FAIL, "Python", f"{v.major}.{v.minor} — 3.8 이상이 필요합니다"))

    # 2) .env 의 KIS 키 (값은 표시하지 않음)
    missing = [k for k in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO") if not os.getenv(k)]
    if missing:
        results.append((FAIL, ".env KIS 키", f"누락: {', '.join(missing)} (cp .env.example .env 후 입력)"))
    else:
        results.append((OK, ".env KIS 키", "설정됨"))

    # 3) KIS API — 토큰 발급(연결성 핵심) + 시세 조회(보너스)
    virtual = bool(getattr(client, "is_virtual", True))
    mode = "모의투자" if virtual else "실전투자"
    try:
        client._ensure_token()
        try:
            p = int(client.get_current_price("005930").get("price", 0) or 0)
            detail = f"토큰 발급 + 시세 OK (삼성전자 {p:,}원)" if p else "토큰 발급 OK"
        except Exception:
            detail = "토큰 발급 OK (시세 조회는 장중에 확인)"
        results.append((OK, f"KIS API ({mode})", detail))
    except Exception as e:
        results.append((FAIL, f"KIS API ({mode})",
                        f"연결 실패: {str(e)[:120]} (키/계좌번호/네트워크 확인)"))

    # 4) AI — provider 별 실제 1회 응답 (각각 독립 검증, _probe_ai_providers).
    try:
        from zusik.clients.claude_client import ClaudeClient
        ai = ClaudeClient(prefer_cli=True)
        results.append(_probe_ai_providers(ai))
    except Exception as e:
        results.append((WARN, "AI", f"점검 중 예외: {str(e)[:100]}"))

    # 4b) 로컬 LLM (Ollama) — config.ai_providers.local_enabled=true 일 때만 점검
    try:
        from zusik.clients.claude_client import ClaudeClient
        lai = ClaudeClient(prefer_cli=True)
        if getattr(lai, "_local_enabled", False):
            if lai._has_local:
                r = lai._run_local("한 단어로만 답해: OK", False)
                if r and not ClaudeClient._is_failed(r):
                    results.append((OK, "로컬 LLM",
                                    f"{lai._local_model} @ {lai._local_endpoint} — 응답 OK"))
                else:
                    results.append((WARN, "로컬 LLM",
                                    f"{lai._local_model} 연결됐으나 응답 실패: {str(r)[:80]} "
                                    f"(모델 pull 여부 확인: `ollama pull {lai._local_model}`)"))
            else:
                results.append((WARN, "로컬 LLM",
                                f"local_enabled=true 이나 Ollama 무응답 ({lai._local_endpoint}) "
                                f"— `ollama serve` 실행/엔드포인트 확인"))
    except Exception as e:
        results.append((WARN, "로컬 LLM", f"점검 중 예외: {str(e)[:100]}"))

    # 5) 메신저 (알림 경로)
    msgr = []
    if os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_BOT_TOKEN"):
        msgr.append("Discord")
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        msgr.append("Telegram")
    if os.getenv("SLACK_WEBHOOK_URL"):
        msgr.append("Slack")
    if msgr:
        results.append((OK, "메신저", ", ".join(msgr)))
    else:
        results.append((WARN, "메신저", "미설정 — 알림을 받으려면 .env 에 DISCORD_WEBHOOK_URL 등을 설정"))

    # 6) 경로 쓰기 (로그/데이터)
    from zusik import paths
    bad = []
    for sub in ("logs", "data"):
        d = str(paths.ROOT / sub)
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".hc_write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except Exception as e:
            bad.append(f"{sub} ({str(e)[:60]})")
    if bad:
        results.append((FAIL, "경로 쓰기", ", ".join(bad)))
    else:
        results.append((OK, "경로 쓰기", "logs/ data/ 쓰기 가능"))

    return results, mode


def _format_report(results, mode: str):
    """results → (리포트 텍스트, summary, n_fail). 사람이 읽는 형식."""
    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    n_warn = sum(1 for s, _, _ in results if s == WARN)
    lines = [f"zusik 헬스체크 ({mode})", "-" * 52]
    lines += [_line(s, name, detail) for s, name, detail in results]
    lines.append("-" * 52)
    if n_fail:
        summary = f"실패 {n_fail}건 — 매매 시작 전 해결 필요 (경고 {n_warn}건)"
    elif n_warn:
        summary = f"통과 (경고 {n_warn}건) — 매매 가능, 위 경고 확인 권장"
    else:
        summary = "전부 정상 — 매매 준비 완료"
    lines.append(f"결과: {summary}")
    return "\n".join(lines), summary, n_fail


def healthcheck_text(client, config):
    """체크 수행 → (exit_code, 리포트 텍스트). /점검 명령·CLI 공용. 메신저 전송 없음."""
    results, mode = _collect_results(client, config)
    text, _summary, n_fail = _format_report(results, mode)
    return (1 if n_fail else 0), text


def run_healthcheck(client, config, discord=None, notify: bool = False) -> int:
    """점검 실행 → 리포트 출력 → (옵션) 메신저 전송. 핵심 실패 시 1 반환."""
    results, mode = _collect_results(client, config)
    text, summary, n_fail = _format_report(results, mode)
    print("\n" + text + "\n")

    # ── (옵션) 메신저 전송 — 장전 cron 에서 결과를 받아보기 위함. 시크릿은 포함하지 않음 ──
    if notify:
        non_ok = [_line(s, name, detail) for s, name, detail in results if s != OK]
        body = f"[zusik 헬스체크] {summary}"
        if non_ok:
            body += "\n" + "\n".join(non_ok)
        how = _send_notify(discord, body)
        print({"direct": "메신저 전송: 완료",
               "queue": "메신저 전송: 라이브 봇 명령 큐로 전달 (봇이 1분 내 Discord 게시)",
               "fail": "메신저 전송: 실패 — 메신저 미설정/봇 미가동"}.get(how, ""))

    return 1 if n_fail else 0


def _send_notify(discord, body: str) -> str:
    """결과를 메신저로 전송. webhook/Telegram/Slack 은 직접, Discord 봇토큰 구성은 명령 큐로.

    Returns: "direct" | "queue" | "fail".
    """
    if discord is not None:
        for method in ("notify_info", "send_bot_message", "send_message"):
            fn = getattr(discord, method, None)
            if callable(fn):
                try:
                    fn(body)
                    return "direct"
                except Exception:
                    pass
    # webhook 없는 Discord 봇토큰 구성 → 라이브 봇이 폴링하는 명령 큐(watchdog_alert)로
    try:
        _enqueue_alert(body)
        return "queue"
    except Exception:
        return "fail"


def _enqueue_alert(message: str) -> None:
    """data/commands.json 에 watchdog_alert 추가 → 라이브 봇이 읽어 Discord 봇 채널로 게시."""
    import json
    from datetime import datetime
    from zusik import paths
    path = paths.data_path("commands.json")
    cmds = []
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cmds = json.load(f) or []
    except Exception:
        cmds = []
    cmds.append({"cmd": "watchdog_alert", "message": message,
                 "timestamp": datetime.now().isoformat(), "processed": False})
    paths.write_json_atomic(path, cmds[-50:])
