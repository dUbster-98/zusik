from __future__ import annotations
import logging
import os
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

# 시끄러운 서드파티 로거 — 평시엔 WARNING 으로 낮춰 봇 결정 로그가 묻히지 않게 한다.
# verbose(log_level=DEBUG)면 INFO 로 풀어 더 자세히 본다.
_NOISY = (
    "discord", "discord.gateway", "discord.client", "discord.http",
    "websockets", "urllib3", "asyncio", "httpx", "httpcore",
)

# 매수/매도 '왜'를 한 곳에 모으는 전용 로거 (logs/decisions.log + 메인 로그/journal 동시).
decision_logger = logging.getLogger("decision")


def log_decision(action: str, name: str, code: str, qty, price,
                 reason: str = "", extra: str = "") -> None:
    """매매 결정 1줄 트레이스 — record_buy/record_sell 단일 관문에서 호출.

    '왜 샀나/팔았나'를 logs/decisions.log 에 모아 추적을 쉽게 한다. 매매 경로를 막지 않도록
    어떤 경우에도 예외를 밖으로 던지지 않는다.
    """
    try:
        tail = f" | {extra}" if extra else ""
        decision_logger.info(
            "결정 %-4s %s(%s) %s주 @%s — %s%s",
            action, name, code, qty, f"{int(price):,}", reason or "(사유없음)", tail,
        )
    except Exception:
        pass


def setup_logger(log_level: str = "INFO") -> logging.Logger:
    """콘솔(stdout→journal) + 일별 회전 파일 + 에러/결정 전용 파일 로거 설정.

    - stdout 핸들러: systemd StandardOutput=journal 로 흘러 `journalctl -u zusik -f` 가 작동.
      (과거 StreamHandler 기본 stderr → bot_error.log 가 모든 INFO 까지 받아 100MB+ 폭증하던 문제)
    - logs/bot.log: 자정 회전 + 14일 보관 (전체 로그, 디스크 무한증가 방지)
    - logs/errors.log: WARNING+ 만 (진짜 오류만 모아 추적)
    - logs/decisions.log: 매수/매도 사유 ('왜' 한 곳에)
    - verbose: log_level=DEBUG 면 서드파티 로거도 INFO 로 풀어 더 자세히
    """
    os.makedirs("logs", exist_ok=True)
    level = getattr(logging, str(log_level).upper(), logging.INFO)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):           # 재호출 시 핸들러 중복 방지
        root.removeHandler(h)

    # 1) stdout → systemd journal (live tail)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 2) 일별 회전 파일 (전체)
    fileh = TimedRotatingFileHandler(
        os.path.join("logs", "bot.log"), when="midnight",
        backupCount=14, encoding="utf-8",
    )
    fileh.suffix = "%Y-%m-%d"
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # 3) 에러 전용 파일 (WARNING+)
    errh = RotatingFileHandler(
        os.path.join("logs", "errors.log"), maxBytes=10_000_000,
        backupCount=5, encoding="utf-8",
    )
    errh.setLevel(logging.WARNING)
    errh.setFormatter(fmt)
    root.addHandler(errh)

    # 4) 결정 전용 파일 (매수/매도 사유) — 루트로도 전파돼 메인 로그/journal 에도 남는다.
    for h in list(decision_logger.handlers):
        decision_logger.removeHandler(h)
    dech = RotatingFileHandler(
        os.path.join("logs", "decisions.log"), maxBytes=5_000_000,
        backupCount=10, encoding="utf-8",
    )
    dech.setFormatter(fmt)
    decision_logger.addHandler(dech)
    decision_logger.setLevel(logging.INFO)
    decision_logger.propagate = True

    # 시끄러운 서드파티 — verbose(DEBUG) 가 아니면 WARNING 으로 낮춤
    lib_level = logging.INFO if level <= logging.DEBUG else logging.WARNING
    for name in _NOISY:
        logging.getLogger(name).setLevel(lib_level)

    return root
