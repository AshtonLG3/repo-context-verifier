"""Adaptive context budgeting for Sentinel.

The budget is a local proxy for context/tool-output pressure. It does not read or
estimate the user's private ChatGPT quota. It tracks what Sentinel itself returns,
suppresses exact duplicate context requests for an unchanged worktree generation,
and requires an explicit reason to extend a task budget.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

DEFAULT_MAX_CHARS = 120_000
DEFAULT_MAX_CALLS = 24
MAX_MAX_CHARS = 500_000
MAX_MAX_CALLS = 80
MAX_EXTENSIONS = 2
MAX_EXTENSION_CHARS = 120_000
MAX_EXTENSION_CALLS = 20


def init_budget(max_chars: int | None = None, max_calls: int | None = None) -> dict[str, Any]:
    chars = DEFAULT_MAX_CHARS if max_chars is None else int(max_chars)
    calls = DEFAULT_MAX_CALLS if max_calls is None else int(max_calls)
    chars = max(20_000, min(chars, MAX_MAX_CHARS))
    calls = max(5, min(calls, MAX_MAX_CALLS))
    return {
        "max_chars": chars,
        "max_calls": calls,
        "chars_used": 0,
        "calls_used": 0,
        "duplicates_suppressed": 0,
        "blocked_calls": 0,
        "seen": {},
        "extensions": [],
    }


def signature(tool_name: str, args: dict[str, Any], generation: str) -> str:
    normalized = {k: v for k, v in args.items() if k != "repo_path"}
    payload = json.dumps(
        {"tool": tool_name, "args": normalized, "generation": generation},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def preflight(
    budget: dict[str, Any],
    tool_name: str,
    args: dict[str, Any],
    generation: str,
) -> tuple[str, dict[str, Any] | None]:
    sig = signature(tool_name, args, generation)
    seen = budget.setdefault("seen", {})
    if sig in seen:
        budget["duplicates_suppressed"] = int(budget.get("duplicates_suppressed", 0)) + 1
        previous = seen[sig]
        return sig, {
            "blocked": True,
            "reason": "DUPLICATE_CONTEXT_SUPPRESSED",
            "tool": tool_name,
            "previous": {
                "chars": previous.get("chars"),
                "recorded_at": previous.get("recorded_at"),
            },
            "guidance": (
                "This exact context request was already returned for the current worktree generation. "
                "Reuse the existing result or change the query meaningfully."
            ),
        }

    calls_used = int(budget.get("calls_used", 0))
    chars_used = int(budget.get("chars_used", 0))
    if calls_used >= int(budget.get("max_calls", DEFAULT_MAX_CALLS)) or chars_used >= int(
        budget.get("max_chars", DEFAULT_MAX_CHARS)
    ):
        budget["blocked_calls"] = int(budget.get("blocked_calls", 0)) + 1
        return sig, {
            "blocked": True,
            "reason": "CONTEXT_BUDGET_EXHAUSTED",
            "budget": status(budget),
            "guidance": (
                "Use context_budget_extend only if additional repository context is necessary for correctness, "
                "and provide a concrete reason. Otherwise continue with context already gathered."
            ),
        }

    return sig, None


def record(
    budget: dict[str, Any],
    sig: str,
    tool_name: str,
    result: Any,
) -> dict[str, Any]:
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    chars = len(encoded)
    budget["calls_used"] = int(budget.get("calls_used", 0)) + 1
    budget["chars_used"] = int(budget.get("chars_used", 0)) + chars
    budget.setdefault("seen", {})[sig] = {
        "tool": tool_name,
        "chars": chars,
        "recorded_at": time.time(),
    }
    return status(budget)


def status(budget: dict[str, Any]) -> dict[str, Any]:
    max_chars = int(budget.get("max_chars", DEFAULT_MAX_CHARS))
    max_calls = int(budget.get("max_calls", DEFAULT_MAX_CALLS))
    chars_used = int(budget.get("chars_used", 0))
    calls_used = int(budget.get("calls_used", 0))
    chars_pct = round((chars_used / max_chars) * 100, 1) if max_chars else 0.0
    calls_pct = round((calls_used / max_calls) * 100, 1) if max_calls else 0.0
    pressure = max(chars_pct, calls_pct)
    return {
        "chars_used": chars_used,
        "max_chars": max_chars,
        "calls_used": calls_used,
        "max_calls": max_calls,
        "chars_percent": chars_pct,
        "calls_percent": calls_pct,
        "pressure_percent": pressure,
        "duplicates_suppressed": int(budget.get("duplicates_suppressed", 0)),
        "blocked_calls": int(budget.get("blocked_calls", 0)),
        "extensions_used": len(budget.get("extensions", [])),
        "state": "critical" if pressure >= 90 else "warning" if pressure >= 75 else "ok",
    }


def extend(
    budget: dict[str, Any],
    extra_chars: int,
    extra_calls: int,
    reason: str,
) -> dict[str, Any]:
    if not isinstance(reason, str) or len(reason.strip()) < 10:
        raise ValueError("reason must explain why more context is necessary")
    extensions = budget.setdefault("extensions", [])
    if len(extensions) >= MAX_EXTENSIONS:
        raise ValueError("Context budget extension limit reached for this task")

    extra_chars = max(0, min(int(extra_chars), MAX_EXTENSION_CHARS))
    extra_calls = max(0, min(int(extra_calls), MAX_EXTENSION_CALLS))
    if extra_chars == 0 and extra_calls == 0:
        raise ValueError("At least one positive extension amount is required")

    budget["max_chars"] = min(
        int(budget.get("max_chars", DEFAULT_MAX_CHARS)) + extra_chars,
        MAX_MAX_CHARS,
    )
    budget["max_calls"] = min(
        int(budget.get("max_calls", DEFAULT_MAX_CALLS)) + extra_calls,
        MAX_MAX_CALLS,
    )
    extensions.append(
        {
            "extra_chars": extra_chars,
            "extra_calls": extra_calls,
            "reason": reason.strip()[:500],
            "at": time.time(),
        }
    )
    return status(budget)
