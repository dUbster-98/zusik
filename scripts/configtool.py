#!/usr/bin/env python3
"""config 설정 도구 — config.yaml 을 직접 손대지 않고 안전하게 설정 변경 (현대화).

config.yaml(주석 보존 기본값)은 그대로 두고, 사용자 변경은 config.local.yaml(로컬,
gitignore)에 분리 저장한다. 봇은 load_config()에서 config.local.yaml 을 최종 깊은 병합
→ 사용자 명시 설정이 최우선(학습값 위). 점(.) 경로로 중첩 키 접근.

사용법:
    python3 configtool.py show                          # 효과적 설정 + 오버라이드 표시
    python3 configtool.py list                          # 로컬 오버라이드만 표시
    python3 configtool.py get  risk.daily_loss_limit    # 효과적 값 조회
    python3 configtool.py set  risk.daily_loss_limit -20000   # 오버라이드 설정
    python3 configtool.py set  position.buy_tranches '[0.4, 0.3, 0.3]'
    python3 configtool.py unset risk.daily_loss_limit   # 오버라이드 제거

값은 YAML 로 파싱(타입 자동): -20000→int, 0.02→float, true→bool, null→None,
'[0.3,0.5]'→list, defensive→str. 변경 시 자동 백업(config.local.yaml.bak).
"""
from __future__ import annotations

import os
import sys

import yaml

# 이 파일은 scripts/ 아래에 있으므로 레포 루트는 두 단계 상위다.
# (dirname 한 번이면 scripts/ 가 되어 봇이 읽지 않는 scripts/config*.yaml 을 건드린다.)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "config.yaml")
LOCAL = os.path.join(REPO, "config.local.yaml")

# 과거 경로 버그로 scripts/ 아래에 stray 파일이 생겼으면 경고 (봇은 이 파일을 읽지 않음).
_STRAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.yaml")
if os.path.exists(_STRAY):
    sys.stderr.write(
        f"[경고] 봇이 읽지 않는 stray 설정 파일이 있습니다: {_STRAY}\n"
        f"        실제 적용 경로는 {LOCAL} 입니다. stray 파일을 확인 후 삭제하세요.\n"
    )


def _load_yaml(path) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_local(data: dict):
    if os.path.exists(LOCAL):  # 백업
        with open(LOCAL, encoding="utf-8") as f:
            old = f.read()
        with open(LOCAL + ".bak", "w", encoding="utf-8") as f:
            f.write(old)
    with open(LOCAL, "w", encoding="utf-8") as f:
        f.write("# 로컬 설정 오버라이드 — configtool.py 가 관리 (직접 편집 비권장).\n")
        f.write("# config.yaml(기본값) 위에 깊은 병합되며 gitignore 로 제외됨.\n\n")
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _effective() -> dict:
    """config.yaml + config.local.yaml 깊은 병합 (apply_mode/학습 제외한 순수 병합 미리보기)."""
    base = _load_yaml(CONFIG)
    local = _load_yaml(LOCAL)
    _deep_merge(base, local)
    return base


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _get_path(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _set_path(d: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _unset_path(d: dict, dotted: str) -> bool:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            return False
        cur = cur[part]
    if parts[-1] in cur:
        del cur[parts[-1]]
        return True
    return False


def cmd_show():
    eff = _effective()
    local = _load_yaml(LOCAL)
    print("=== 효과적 설정 (config.yaml + 로컬 오버라이드) ===")
    print(yaml.safe_dump(eff, allow_unicode=True, sort_keys=False).rstrip())
    print("\n=== 로컬 오버라이드 (config.local.yaml) ===")
    print(yaml.safe_dump(local, allow_unicode=True, sort_keys=False).rstrip() if local else "(없음)")


def cmd_list():
    local = _load_yaml(LOCAL)
    print(yaml.safe_dump(local, allow_unicode=True, sort_keys=False).rstrip() if local else "(오버라이드 없음)")


def cmd_get(key):
    val, found = _get_path(_effective(), key)
    if not found:
        print(f"(키 없음: {key})")
        return 1
    print(yaml.safe_dump({key: val}, allow_unicode=True, sort_keys=False).rstrip())
    return 0


def cmd_set(key, raw):
    try:
        value = yaml.safe_load(raw)   # 타입 자동 추론
    except Exception as e:
        print(f"값 파싱 실패: {e}")
        return 1
    local = _load_yaml(LOCAL)
    _set_path(local, key, value)
    _save_local(local)
    print(f"오버라이드 설정: {key} = {value!r}")
    print("   재시작 시 적용 (sudo systemctl restart zusik). config.yaml 원본은 변경 없음.")
    return 0


def cmd_unset(key):
    local = _load_yaml(LOCAL)
    if _unset_path(local, key):
        _save_local(local)
        print(f"오버라이드 제거: {key}")
    else:
        print(f"(오버라이드에 없음: {key})")
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "show":
        return cmd_show() or 0
    if cmd == "list":
        return cmd_list() or 0
    if cmd == "get" and len(argv) >= 2:
        return cmd_get(argv[1])
    if cmd == "set" and len(argv) >= 3:
        return cmd_set(argv[1], " ".join(argv[2:]))
    if cmd == "unset" and len(argv) >= 2:
        return cmd_unset(argv[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
