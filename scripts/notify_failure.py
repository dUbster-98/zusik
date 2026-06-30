#!/usr/bin/env python3
"""systemd OnFailure 훅 — 봇이 비정상 종료/실패하면 즉시 1회 알림.

zusik.service 의 `OnFailure=zusik-notify-failure.service` 가 이 스크립트를 실행한다.
메인 프로세스가 죽은 상태라 in-process Discord 봇을 쓸 수 없으므로 .env 의 webhook/
Telegram 으로 직접 전송한다. (주기 감시·복구 통보는 scripts/watchdog.py 가 담당.)

사용: python3 scripts/notify_failure.py "사유 메시지"
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    reason = sys.argv[1] if len(sys.argv) > 1 else "봇 비정상 종료 — systemd 재시작 시도 중"
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"**Zusik 코어 다운**\n{reason}\n시각: {when}\n확인: `sudo systemctl status zusik`"

    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except Exception:
        pass

    sent = False
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if webhook:
        try:
            import requests
            requests.post(webhook, json={"content": msg}, timeout=10)
            sent = True
        except Exception as e:
            print(f"Discord 전송 실패: {e}")

    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat:
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                          json={"chat_id": tg_chat, "text": msg}, timeout=10)
            sent = True
        except Exception as e:
            print(f"Telegram 전송 실패: {e}")

    if not sent:
        print(f"[ALERT] {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
