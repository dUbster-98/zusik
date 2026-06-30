from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

# OS 중립 임시 경로 (Windows엔 /tmp 없음 — 하드코딩 시 쓰기 실패로 cooldown 미적용 → CI 실패)
_TMP = tempfile.gettempdir()
_CODEX_CD = os.path.join(_TMP, "_test_codex_cd.txt")


def _make_client(**flags):
    """__init__(=CLI 탐지 subprocess)를 우회하고 플래그만 세팅한 ClaudeClient."""
    mod = importlib.import_module("zusik.clients.claude_client")
    c = mod.ClaudeClient.__new__(mod.ClaudeClient)
    c._has_claude = flags.get("claude", True)
    c._has_codex = flags.get("codex", True)
    c._has_agy = flags.get("agy", False)
    c._has_local = flags.get("local", False)
    # 테스트 격리: cooldown 파일을 임시 경로로
    c._CODEX_COOLDOWN_FILE = flags.get("codex_file", _CODEX_CD)
    if os.path.exists(c._CODEX_COOLDOWN_FILE):
        os.remove(c._CODEX_COOLDOWN_FILE)
    return c, mod


class CodexCooldownTests(unittest.TestCase):
    def tearDown(self):
        if os.path.exists(_CODEX_CD):
            os.remove(_CODEX_CD)

    def test_cooldown_roundtrip(self):
        c, _ = _make_client()
        self.assertFalse(c._is_codex_cooldown())
        c._set_codex_cooldown(15.0)
        self.assertTrue(c._is_codex_cooldown())

    def test_balanced_skips_codex_when_cooldown(self):
        #: codex 세션 만료로 cooldown이면 balanced 라우팅이
        # codex를 건드리지 않고 claude로 가야 함 (죽은 CLI 두드리기 방지).
        #: DAILY_LIMITS Claude=0 변경에 대비해 _check_limit 패치로 격리.
        c, mod = _make_client(claude=True, codex=True)
        c._set_codex_cooldown(15.0)

        def _boom(*a, **k):
            raise AssertionError("cooldown 중에는 codex를 호출하면 안 됨")

        c._run_codex = _boom
        c._run_claude = lambda prompt, model, web: '{"signal":"buy","confidence":0.6}'
        with patch.object(mod, "_check_limit", return_value=True):
            out = c._call_balanced("prompt")
        self.assertIn("buy", out)

    def test_exec_detects_codex_session_expired(self):
        # codex가 "session has ended / failed to refresh token"을 stderr로 뱉으면
        # _exec이 cooldown을 걸고 명확한 stub을 반환해야 함.
        c, mod = _make_client()

        class _CP:
            stdout = ""
            stderr = ("ERROR: Failed to refresh token: 400 Bad Request: "
                      "Your session has ended. Please log in again.")

        with patch.object(mod.subprocess, "run", return_value=_CP()):
            out = c._exec(["codex", "exec", "x"], "codex", timeout=5)
        self.assertIn("세션 만료", out)
        self.assertTrue(c._is_codex_cooldown())

    def test_exec_normal_codex_output_no_cooldown(self):
        # 정상 응답이면 cooldown을 걸지 않는다 (오탐 방지).
        c, mod = _make_client()

        class _CP:
            stdout = '{"signal":"hold","confidence":0.4}'
            stderr = "Reading additional input from stdin..."

        with patch.object(mod.subprocess, "run", return_value=_CP()):
            out = c._exec(["codex", "exec", "x"], "codex", timeout=5)
        self.assertIn("hold", out)
        self.assertFalse(c._is_codex_cooldown())


if __name__ == "__main__":
    unittest.main()
