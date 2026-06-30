from __future__ import annotations
"""레포 루트 기준 경로 해석 — 모듈이 어느 서브패키지에 있든 data/·config·.env 를 찾게."""
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # zusik/paths.py → zusik/ → repo root


def data_path(*parts) -> str:
    return str(ROOT.joinpath("data", *parts))


def reports_path(*parts) -> str:
    """사람이 읽는 산출물(월간 HTML 등) 경로. data/(상태)·logs/(로그)와 분리."""
    return str(ROOT.joinpath("reports", *parts))


def write_json_atomic(path: str, data) -> None:
    """tmp + os.replace 원자적 JSON 쓰기 — 프로세스 중단 시 부분 파손 파일 방지.

    상태 파일은 부분 기록되면 다음 read가 파싱 실패로 신호를 통째로 잃는다. 동일 디렉터리에
    임시파일로 쓴 뒤 atomic rename 한다 (다른 상태 파일들의 _save_json 패턴과 동일).
    """
    path = str(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or None, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def config_path(name: str = "config.yaml") -> str:
    return str(ROOT / name)


def env_path() -> str:
    return str(ROOT / ".env")
