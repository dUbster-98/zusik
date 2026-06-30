from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock


def ensure_stubbed_modules():
    """`claude_analyst` import에 필요한 외부 모듈을 최소 stub로 보강."""
    if "anthropic" not in sys.modules:
        anthropic = types.ModuleType("anthropic")

        class _Anthropic:
            pass

        anthropic.Anthropic = _Anthropic
        sys.modules["anthropic"] = anthropic

    if "numpy" not in sys.modules:
        numpy = types.ModuleType("numpy")

        def _mean(values):
            values = list(values)
            return sum(values) / len(values) if values else 0

        numpy.mean = _mean
        sys.modules["numpy"] = numpy

    if "pandas" not in sys.modules:
        pandas = types.ModuleType("pandas")

        class _DataFrame:
            pass

        pandas.DataFrame = _DataFrame
        pandas.notna = lambda value: value is not None
        sys.modules["pandas"] = pandas


def import_claude_analyst():
    ensure_stubbed_modules()
    return importlib.import_module("zusik.analysis.claude_analyst")


def make_claude_analyst(perf: dict | None = None, memory: Mock | None = None):
    mod = import_claude_analyst()
    analyst = mod.ClaudeAnalyst.__new__(mod.ClaudeAnalyst)
    analyst.ROLES = list(mod.ClaudeAnalyst.ROLES)
    analyst.analysts = {
        "fundamental": SimpleNamespace(name_kr="펀더멘털"),
        "sentiment": SimpleNamespace(name_kr="센티멘트"),
        "quant": SimpleNamespace(name_kr="퀀트"),
        "generalist": SimpleNamespace(name_kr="종합"),
    }
    analyst._perf = perf or {
        role: {"correct": 0, "wrong": 0, "total": 0, "weight": 1.0}
        for role in analyst.ROLES
    }
    analyst.memory = memory or Mock(record_outcome=Mock(), add_mistake=Mock())
    return analyst, mod
