#!/usr/bin/env python3
from __future__ import annotations
"""봇에게 명령 보내기.

사용법:
  python3 cmd.py 종목 목록
  python3 cmd.py 종목 추가 KR 005930 삼성전자
  python3 cmd.py 종목 추가 US SOFI SoFi NASD
  python3 cmd.py 종목 제거 US NVDA
  python3 cmd.py 상태
  python3 cmd.py 모드 aggressive
  python3 cmd.py 긴급홀딩
  python3 cmd.py 도움
"""

import sys
import os
# scripts/ 이동 — 저장소 루트를 import 경로에 추가.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zusik.clients.discord_commander import queue_command

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 cmd.py <명령>")
        print("예: python3 cmd.py 종목 목록")
        print("    python3 cmd.py 도움")
        sys.exit(1)

    cmd = " ".join(sys.argv[1:])
    queue_command(cmd)
    print(f"명령 전송: {cmd}")
    print("봇이 다음 틱(1분 내)에 처리합니다. Discord에서 결과를 확인하세요.")
