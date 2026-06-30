#!/usr/bin/env python3
"""봇 가용성 감시 — systemd timer(zusik-watchdog.timer) 5분 또는 cron 으로 실행.

감지 + 전이 1회 통보(복구 포함):
  - 코어 다운: systemctl is-active != active  또는  heartbeat(status.json) stale
  - LLM 다운: data/llm_health.json status == down (message() 가 집계)
  - 반복 에러: 최근 로그의 동일 ERROR N회 (1시간 dedup)

down→up/up→down 전이에서만 알림 → 5분마다 스팸 방지 + 복구도 통보.
메인이 죽어 commands.json 큐를 못 읽는 경우를 위해 webhook 직접 전송 경로 병행.

설치: bash deploy/setup_service.sh (systemd timer) 또는 crontab:
  */5 * * * * cd /path/to/zusik && /usr/bin/python3 scripts/watchdog.py
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO, "logs")
DATA_DIR = os.path.join(REPO, "data")
STATE_FILE = os.path.join(DATA_DIR, "watchdog_state.json")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
LLM_HEALTH_FILE = os.path.join(DATA_DIR, "llm_health.json")
CONFIG_FILE = os.path.join(REPO, "config.yaml")

ERROR_THRESHOLD = 3            # 같은 에러 N번 반복되면 알림
HEARTBEAT_STALE_MIN = 10      # status.json generated_at 가 N분 넘게 안 갱신되면 hang


def _load_cfg() -> dict:
    """config.yaml watchdog 블록 (없으면 기본값). PyYAML 없으면 조용히 무시."""
    try:
        import yaml
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return (cfg.get("watchdog", {}) or {})
    except Exception:
        return {}


def get_latest_log() -> str:
    """오늘 로그 파일 경로 (없으면 가장 최근)."""
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(LOG_DIR, f"bot_{today}.log")
    if os.path.exists(path):
        return path
    logs = sorted(glob.glob(os.path.join(LOG_DIR, "bot_*.log")))
    return logs[-1] if logs else ""


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_check": "", "alerted_errors": {}, "core_down": False, "llm_down": False}


def save_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_process_alive() -> bool:
    """봇 systemd 유닛이 active 인지."""
    try:
        r = subprocess.run(["systemctl", "is-active", "zusik"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def heartbeat_age_min() -> float | None:
    """status.json generated_at 기준 마지막 tick 경과(분). 없거나 파싱 실패면 None.

    봇은 매 tick(1분) status.json 을 갱신한다 → 프로세스 active 인데 이 값이 멈추면 hang.
    """
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            gen = (json.load(f) or {}).get("generated_at", "")
        ts = datetime.strptime(gen, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - ts).total_seconds() / 60.0
    except Exception:
        return None


def read_llm_status() -> tuple[str, str]:
    """(status, last_reason) — llm_health.json. 없으면 ('ok','')."""
    try:
        with open(LLM_HEALTH_FILE, encoding="utf-8") as f:
            h = json.load(f) or {}
        return str(h.get("status", "ok")), str(h.get("last_reason", ""))
    except Exception:
        return "ok", ""


def check_recent_errors(log_path: str, minutes: int = 10) -> dict:
    """최근 N분간 ERROR 블록 집계 (마지막 줄을 키로)."""
    if not log_path or not os.path.exists(log_path):
        return {}
    cutoff = datetime.now() - timedelta(minutes=minutes)
    errors: dict[str, int] = {}
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "ERROR" in line or "Traceback" in line:
            block = [line.rstrip()]
            j = i + 1
            while j < len(lines) and (
                lines[j].startswith(" ") or lines[j].startswith("Traceback")
                or "Error" in lines[j]
            ):
                block.append(lines[j].rstrip())
                j += 1
            try:
                ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                if ts >= cutoff:
                    key = block[-1].strip()[:120]
                    errors[key] = errors.get(key, 0) + 1
            except Exception:
                pass
            i = j
        else:
            i += 1
    return errors


def send_discord_alert(message: str):
    """commands.json 큐(봇 생존 시) + webhook 직접(봇 사망 시) 양 경로 발송."""
    try:
        cmd_file = os.path.join(DATA_DIR, "commands.json")
        cmds = []
        if os.path.exists(cmd_file):
            try:
                with open(cmd_file, encoding="utf-8") as f:
                    cmds = json.load(f)
            except Exception:
                cmds = []
        cmds.append({"cmd": "watchdog_alert", "message": message,
                     "timestamp": datetime.now().isoformat(), "processed": False})
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(cmd_file, "w", encoding="utf-8") as f:
            json.dump(cmds[-50:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"큐 기록 실패: {e}")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
        sent = False
        webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
        if webhook:
            import requests
            requests.post(webhook, json={"content": message}, timeout=10)
            sent = True
        tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat:
            import requests
            requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                          json={"chat_id": tg_chat, "text": message}, timeout=10)
            sent = True
        if not sent:
            print(f"[ALERT] {message}")
    except Exception as e:
        print(f"webhook 전송 실패: {e}")


def main():
    cfg = _load_cfg()
    stale_min = float(cfg.get("heartbeat_stale_min", HEARTBEAT_STALE_MIN))
    state = load_state()
    alerts = []

    # 1) 코어 가용성 (프로세스 + heartbeat) — 전이에서만 통보
    alive = check_process_alive()
    age = heartbeat_age_min()
    hb_stale = (age is not None and age > stale_min)
    core_down = (not alive) or hb_stale
    was_down = bool(state.get("core_down", False))
    if core_down and not was_down:
        if not alive:
            alerts.append("코어 다운 — 봇 프로세스 비활성. `sudo systemctl restart zusik`")
        else:
            alerts.append(f"코어 멈춤 의심 — 마지막 tick {age:.0f}분 전(>{stale_min:.0f}분). "
                          f"프로세스는 active 이나 루프 hang 가능. `sudo systemctl restart zusik`")
        state["core_down"] = True
        state["core_down_since"] = datetime.now().isoformat()
    elif (not core_down) and was_down:
        since = state.get("core_down_since", "")
        dur = ""
        try:
            dur = f" (다운 {int((datetime.now() - datetime.fromisoformat(since)).total_seconds() / 60)}분)"
        except Exception:
            pass
        alerts.append(f"코어 정상화{dur} — 봇 재가동 확인")
        state["core_down"] = False

    # 2) LLM 가용성 — 전이에서만 통보
    llm_status, llm_reason = read_llm_status()
    llm_down = (llm_status == "down")
    llm_was_down = bool(state.get("llm_down", False))
    if llm_down and not llm_was_down:
        alerts.append("LLM 작동 불가 — claude/codex login 또는 쿼터 확인 "
                      "(로컬 전략으로 매매는 지속)" + (f"\n사유: {llm_reason}" if llm_reason else ""))
        state["llm_down"] = True
    elif (not llm_down) and llm_was_down:
        alerts.append("LLM 복구 — AI 분석 정상화")
        state["llm_down"] = False

    # 3) 반복 에러 (1시간 dedup) — 봇이 살아서 로그를 쌓는 중일 때 의미
    log_path = get_latest_log()
    if not core_down:
        for error_key, count in check_recent_errors(log_path, minutes=10).items():
            if count < ERROR_THRESHOLD:
                continue
            last_alert = state.get("alerted_errors", {}).get(error_key, "")
            if last_alert:
                try:
                    if (datetime.now() - datetime.fromisoformat(last_alert)).total_seconds() < 3600:
                        continue
                except Exception:
                    pass
            alerts.append(f"반복 에러 ({count}회/10분):\n```\n{error_key}\n```")
            state.setdefault("alerted_errors", {})[error_key] = datetime.now().isoformat()

    # 4) 전송
    if alerts:
        msg = "**Zusik Watchdog**\n" + "\n".join(alerts)
        send_discord_alert(msg)
        print(msg)
    else:
        print(f"[{datetime.now().strftime('%H:%M')}] 정상 "
              f"(tick {age:.0f}분 전, LLM {llm_status})" if age is not None
              else f"[{datetime.now().strftime('%H:%M')}] 정상 (LLM {llm_status})")

    state["last_check"] = datetime.now().isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
