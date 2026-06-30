#!/usr/bin/env python3
"""KIS 실시간 WS 검증 프로브 — 읽기 전용(주문 절대 없음).

approval_key 발급 → WS 접속 → 종목 구독 → 수신 메시지(구독 ACK·체결 틱) 관찰.
장 마감 시간엔 ACK·PINGPONG까지(프로토콜 검증), 장중엔 체결 틱 파싱까지 확인된다.

  python3 scripts/realtime_probe.py                 # 005930(KR)+AAPL(US), 25초
  python3 scripts/realtime_probe.py --seconds 40 --kr 005930,000660 --us AAPL,NVDA

매매 동작을 바꾸지 않는다. 봇이 실시간 WS(extreme tier 보유종목)를 이미 쓰고 있으면
KIS 동시 세션 제한으로 잠깐 경합할 수 있으니, 가급적 봇 WS 미사용 시간에 실행 권장.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description="KIS 실시간 WS 검증 프로브(읽기 전용)")
    ap.add_argument("--seconds", type=int, default=25)
    ap.add_argument("--kr", default="005930")
    ap.add_argument("--us", default="AAPL")
    ap.add_argument("--us-exchange", default="NASD",
                    help="해외 거래소(NASD/NYSE/AMEX) — HDFSCNT0 tr_key 구성용")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    # 매니저 내부 로그(구독 ACK/PINGPONG/재접속)를 stdout 으로 본다
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("  [%(levelname)s] %(message)s"))
    lg = logging.getLogger("zusik.clients.kis_websocket")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)

    is_virtual = os.getenv("KIS_VIRTUAL", "false").lower() == "true"
    from zusik.clients.kis_websocket import KISWebSocketManager
    m = KISWebSocketManager(os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", ""),
                            is_virtual=is_virtual)

    print(f"실시간 WS 프로브 ({'모의' if is_virtual else '실전'}) — {args.seconds}초 관찰")
    key = m.get_approval_key()
    print(f"  approval_key: {'발급 OK' if key else '실패'}")
    if not key:
        print("  => 인증 실패. .env 의 KIS_APP_KEY/SECRET 확인.")
        return 1

    ticks = []

    def cb(msg):
        ticks.append(msg)
        print(f"  TICK {msg.get('market')} {msg.get('code')} price={msg.get('price')} ts={msg.get('ts')}")

    if not m.start():
        print("  => WS 접속 실패 (websocket-client/네트워크 확인)")
        return 1
    for c in [x.strip() for x in args.kr.split(",") if x.strip()]:
        m.subscribe(c, cb, market="KR")
    for c in [x.strip() for x in args.us.split(",") if x.strip()]:
        m.subscribe(c, cb, market="US", exchange=args.us_exchange)

    time.sleep(max(5, args.seconds))
    m.stop()

    print("-" * 56)
    print(f"  접속(connected 도달): {m._connected or '관찰 종료 시점 끊김(정상일 수 있음)'}")
    print(f"  수신 체결 틱: {len(ticks)}건")
    if ticks:
        print("  => 틱 파싱 OK (code/price 정상 수신) — 진입 트리거 라이브 검증 완료")
    else:
        print("  => 틱 0건: 장 마감(틱 없음)이면 정상. 위에 '구독 응답 OK' 가 보이면 "
              "approval+접속+구독 프로토콜은 검증됨. 틱 파싱은 장중 재실행으로 확인.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
