"""Sentinel MCP control bridge for Codex repository work.

Local-first MCP server that combines bounded repository context, persistent task
state, process governance, artifact-aware stopping, and evidence-based completion.
Standard-library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import semantic_index
import context_budget

VERSION = "1.1.0"
MAX_FILES = 4000
MAX_MATCHES = 50
MAX_AREAS = 40
MAX_OUTPUT_CHARS = 20_000
MAX_COMMAND_SECONDS = 900
MAX_STATUS_CHECKS = 2

SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".go", ".rs",
    ".cs", ".cpp", ".c", ".h", ".hpp", ".css", ".scss", ".html", ".vue", ".svelte",
    ".json", ".toml", ".yaml", ".yml", ".gradle", ".xml", ".sql", ".sh", ".ps1",
}
SKIP_PARTS = {
    ".git", ".idea", ".gradle", ".venv", "venv", "node_modules", "dist", "build",
    ".next", "vendor", "coverage", "target", "out", "Pods", ".dart_tool",
}
SECRET_NAME = re.compile(
    r"(^|[._-])(env|secret|secrets|credential|credentials|token|tokens|private[-_]?key)([._-]|$)",
    re.I,
)

CACHE_ROOT = Path(os.environ.get(
    "SENTINEL_HOME",
    str(Path.home() / ".repo-context-verifier"),
)).expanduser()

CONTEXT_TOOLS = {
    "repo_overview", "repo_search", "symbol_context", "repo_changes",
    "semantic_status", "semantic_find", "dependency_context",
    "change_impact", "context_bundle",
}


def _git(root: Path, *args: str, timeout: int = 20) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git command failed")
    return result.stdout


def _resolve_root(value: str | None) -> Path:
    if not value:
        raise ValueError("repo_path is required")
    requested = Path(value).expanduser().resolve(strict=True)
    root = Path(_git(requested, "rev-parse", "--show-toplevel").strip()).resolve(strict=True)
    if requested != root and root not in requested.parents:
        raise ValueError("Path is outside the repository")
    return root


def _tracked_files(root: Path) -> list[str]:
    listed = _git(root, "ls-files", "-z").split("\0")
    result: list[str] = []
    for path in listed:
        if not path:
            continue
        p = Path(path)
        if any(part in SKIP_PARTS for part in p.parts):
            continue
        if SECRET_NAME.search(p.name):
            continue
        if p.suffix.lower() not in SOURCE_SUFFIXES and p.name not in {
            "Dockerfile", "Procfile", "Makefile", "gradlew", "gradlew.bat",
        }:
            continue
        result.append(path)
        if len(result) >= MAX_FILES:
            break
    return result


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return head + "\n...[output truncated by Sentinel]...\n" + tail, True


_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(api[_-]?key|secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|
       token|password|passwd|authorization)\b
    (\s*[:=]\s*)
    (["']?)([^\s"',;}{]+)(\3)
    """
)
_SECRET_TOKEN_PATTERNS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]
_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _redact_sensitive_text(text: str) -> str:
    """Best-effort redaction for common credential material in returned context/output."""
    def repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    redacted = _SECRET_ASSIGNMENT.sub(repl, text)
    for pattern in _SECRET_TOKEN_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    if _PRIVATE_KEY_MARKER.search(redacted):
        redacted = _PRIVATE_KEY_MARKER.sub("-----BEGIN [REDACTED] PRIVATE KEY-----", redacted)
    return redacted


def _repo_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:20]


def _state_path(root: Path) -> Path:
    path = CACHE_ROOT / "tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{_repo_key(root)}.json"


def _load_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    if not path.exists():
        return {
            "repo": str(root),
            "task": None,
            "status_checks": {},
            "history": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:
        return {
            "repo": str(root),
            "task": None,
            "status_checks": {},
            "history": [],
        }


def _save_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _current_task(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _load_state(root)
    task = state.get("task")
    if not isinstance(task, dict):
        raise ValueError("No active task. Call task_begin first.")
    return state, task


def _dirty_paths(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "-m", "-o", "--exclude-standard", "-z").split("\0")
    return [p for p in raw if p][:200]


def _worktree_generation(root: Path) -> str:
    """Cheap generation fingerprint for duplicate-context suppression."""
    head = _git(root, "rev-parse", "HEAD").strip()
    pieces = [head]
    for rel in _dirty_paths(root):
        try:
            stat = (root / rel).stat()
            pieces.append(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            pieces.append(f"{rel}:missing")
    return hashlib.sha256("\n".join(pieces).encode("utf-8")).hexdigest()[:24]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Evidence and artifacts must be files inside the repository")
    return path


def _source_fingerprint(root: Path, excluded: list[str] | None = None) -> str:
    """Hash current inputs, including staged edits and new source files, not just HEAD."""
    tracked = set(_git(root, "ls-files", "-z").split("\0"))
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    paths = tracked | {p for p in untracked if Path(p).suffix.lower() in SOURCE_SUFFIXES}
    ignored = set(excluded or [])
    digest = hashlib.sha256(_git(root, "rev-parse", "HEAD").strip().encode())
    for relative in sorted(paths - {""} - ignored):
        if any(part in SKIP_PARTS for part in Path(relative).parts):
            continue
        path = root / relative
        digest.update(relative.encode("utf-8") + b"\0")
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(_sha256_file(path).encode())
        else:
            digest.update(b"missing")
    return digest.hexdigest()


def _artifact_snapshot(root: Path, pattern: str) -> dict[str, dict[str, Any]]:
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("artifact_glob must stay inside the repository")
    found = {}
    for candidate in root.glob(pattern):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        path = _contained_file(root, relative)
        stat = path.stat()
        if stat.st_size <= 0:
            continue
        if len(found) >= 100:
            raise ValueError("artifact_glob matches too many files; use a narrower pattern")
        found[relative] = {
            "path": relative, "size": stat.st_size, "mtime": stat.st_mtime,
            "mtime_ns": stat.st_mtime_ns, "sha256": _sha256_file(path),
        }
    return found


def _check_freshness(root: Path, check: dict[str, Any]) -> str | None:
    if check.get("evidence_kind") not in {"command", "observation", "build"}:
        return "Evidence predates verifiable records; record it again"
    if check.get("source_fingerprint") != _source_fingerprint(root, check.get("input_exclusions")):
        return "Repository inputs changed after verification"
    for item in check.get("evidence_files", []):
        try:
            path = _contained_file(root, item["path"])
            if _sha256_file(path) != item["sha256"]:
                return f"Evidence file changed: {item['path']}"
        except (OSError, ValueError, KeyError):
            return "Evidence file is missing or outside the repository"
    return None


def repo_overview(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    tracked = _tracked_files(root)
    by_area: dict[str, int] = {}
    by_suffix: dict[str, int] = {}
    for path in tracked:
        p = Path(path)
        area = p.parts[0] if len(p.parts) > 1 else "."
        by_area[area] = by_area.get(area, 0) + 1
        suffix = p.suffix.lower() or p.name
        by_suffix[suffix] = by_suffix.get(suffix, 0) + 1

    return {
        "root": str(root),
        "branch": _git(root, "branch", "--show-current").strip(),
        "head": _git(root, "rev-parse", "HEAD").strip(),
        "source_files_scanned": len(tracked),
        "truncated": len(tracked) >= MAX_FILES,
        "areas": sorted(by_area.items(), key=lambda x: (-x[1], x[0]))[:MAX_AREAS],
        "languages_or_types": sorted(by_suffix.items(), key=lambda x: (-x[1], x[0]))[:20],
        "guidance": "Use targeted search/symbol tools before opening large files.",
    }


def repo_search(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    term = args.get("term", "")
    if not isinstance(term, str) or not 2 <= len(term) <= 120 or "\n" in term:
        raise ValueError("term must be 2-120 characters on one line")
    max_matches = min(max(int(args.get("max_matches", 30)), 1), MAX_MATCHES)
    matches: list[dict[str, Any]] = []

    for path in _tracked_files(root):
        try:
            with (root / path).open("r", encoding="utf-8", errors="replace") as stream:
                for number, line in enumerate(stream, 1):
                    if term.casefold() in line.casefold():
                        matches.append({
                            "path": path,
                            "line": number,
                            "text": _redact_sensitive_text(line.strip())[:240],
                        })
                        if len(matches) >= max_matches:
                            return {"matches": matches, "truncated": True}
        except OSError:
            continue
    return {"matches": matches, "truncated": False}


def symbol_context(args: dict[str, Any]) -> dict[str, Any]:
    """Heuristic symbol locator/references, intentionally not advertised as an AST graph."""
    root = _resolve_root(args.get("repo_path"))
    symbol = args.get("symbol", "")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_.$-]{1,119}", symbol):
        raise ValueError("symbol must be a simple 2-120 character identifier")

    definition_patterns = [
        re.compile(rf"\b(def|class|interface|enum|fun|function|struct|trait)\s+{re.escape(symbol)}\b"),
        re.compile(rf"\b{re.escape(symbol)}\s*[:=]\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>)"),
    ]
    definitions: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []

    for path in _tracked_files(root):
        try:
            with (root / path).open("r", encoding="utf-8", errors="replace") as stream:
                for number, line in enumerate(stream, 1):
                    if symbol not in line:
                        continue
                    row = {"path": path, "line": number, "text": _redact_sensitive_text(line.strip())[:240]}
                    if any(p.search(line) for p in definition_patterns):
                        if len(definitions) < 20:
                            definitions.append(row)
                    elif len(references) < 40:
                        references.append(row)
                    if len(definitions) >= 20 and len(references) >= 40:
                        break
        except OSError:
            continue

    return {
        "symbol": symbol,
        "definitions": definitions,
        "references": references,
        "note": "Heuristic textual symbol context; verify callers/dependencies in source before claiming full impact coverage.",
    }



def semantic_refresh(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    return semantic_index.refresh_index(
        CACHE_ROOT,
        _repo_key(root),
        root,
        _tracked_files(root),
        _git(root, "rev-parse", "HEAD").strip(),
    )


def semantic_status(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    status = semantic_index.index_status(CACHE_ROOT, _repo_key(root))
    status["current_head"] = _git(root, "rev-parse", "HEAD").strip()
    dirty = _dirty_paths(root)
    status["dirty_paths"] = dirty[:50]
    status["worktree_dirty"] = bool(dirty)
    if status.get("indexed"):
        status["stale"] = (
            status.get("meta", {}).get("head") != status["current_head"]
            or bool(dirty)
        )
    return status


def semantic_find(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    return semantic_index.semantic_search(
        CACHE_ROOT,
        _repo_key(root),
        args.get("query", ""),
        args.get("limit", 30),
    )


def dependency_context(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    return semantic_index.dependency_context(
        CACHE_ROOT,
        _repo_key(root),
        args.get("symbol", ""),
        args.get("limit", 40),
    )


def change_impact(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    changed = args.get("paths")
    if changed is None:
        base = args.get("base", "HEAD")
        changed = _git(root, "diff", "--name-only", base, "--").splitlines()
        changed += _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
        changed = list(dict.fromkeys(changed))[:100]
    if not isinstance(changed, list) or any(not isinstance(x, str) for x in changed):
        raise ValueError("paths must be a list of repository-relative strings")
    return semantic_index.impact_for_paths(
        CACHE_ROOT,
        _repo_key(root),
        changed,
        args.get("limit", 60),
    )



def context_bundle(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    query = args.get("query", "")
    limit = args.get("limit", 12)
    refresh_if_stale = args.get("refresh_if_stale", True)

    status = semantic_index.index_status(CACHE_ROOT, _repo_key(root))
    current_head = _git(root, "rev-parse", "HEAD").strip()
    dirty = _dirty_paths(root)
    stale = (
        not status.get("indexed")
        or status.get("meta", {}).get("head") != current_head
        or bool(dirty)
    )
    refreshed = False
    if stale and refresh_if_stale:
        semantic_index.refresh_index(
            CACHE_ROOT,
            _repo_key(root),
            root,
            _tracked_files(root),
            current_head,
        )
        refreshed = True

    ranked = semantic_index.rank_context(
        CACHE_ROOT,
        _repo_key(root),
        query,
        dirty,
        limit,
    )
    ranked["index_refreshed"] = refreshed
    ranked["worktree_dirty"] = bool(dirty)
    ranked["guidance"] = (
        "Open only the highest-ranked files needed to resolve the task. "
        "Use dependency_context or change_impact for focused follow-up rather than broad rescans."
    )
    return ranked


def repo_changes(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    base = args.get("base", "HEAD")
    if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9_./~^{}-]{1,120}", base):
        raise ValueError("Invalid Git base ref")
    changed = _git(root, "diff", "--name-only", base, "--").splitlines()
    changed += _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    changed = list(dict.fromkeys(changed))[:150]
    return {
        "root": str(root),
        "changed_files": changed,
        "count": len(changed),
        "guidance": "Review unrelated edits and trace user-facing paths before completion.",
    }


def task_begin(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    goal = args.get("goal", "")
    acceptance = args.get("acceptance", [])
    required = args.get("required_checks", [])

    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal is required")
    if not isinstance(acceptance, list) or not acceptance or any(not isinstance(x, str) or not x.strip() for x in acceptance):
        raise ValueError("acceptance must contain at least one non-empty criterion")
    if not isinstance(required, list) or not required or any(not isinstance(x, str) or not x.strip() for x in required):
        raise ValueError("required_checks must contain at least one non-empty check")
    if len(required) > 12:
        raise ValueError("At most 12 required checks are allowed")

    state = _load_state(root)
    previous = state.get("task")
    if isinstance(previous, dict):
        state.setdefault("history", []).append(previous)
        state["history"] = state["history"][-10:]

    task_id = f"{int(time.time())}-{hashlib.sha1(goal.encode('utf-8')).hexdigest()[:8]}"
    task = {
        "id": task_id,
        "goal": goal.strip(),
        "acceptance": [x.strip() for x in acceptance[:20]],
        "required_checks": list(dict.fromkeys(x.strip() for x in required)),
        "checks": {},
        "started_at": time.time(),
        "status": "active",
        "deliverables": [],
        "commands": 0,
        "output_chars": 0,
        "command_records": {},
        "context_budget": context_budget.init_budget(
            args.get("max_context_chars"),
            args.get("max_context_calls"),
        ),
    }
    state["task"] = task
    state["status_checks"] = {}
    _save_state(root, state)
    return {
        "task_id": task_id,
        "status": "active",
        "required_checks": task["required_checks"],
        "context_budget": context_budget.status(task["context_budget"]),
        "guidance": (
            "Use context_bundle first for orientation, record evidence for required checks, "
            "and stop when task_finish succeeds."
        ),
    }


def task_status(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    state = _load_state(root)
    return {
        "task": state.get("task"),
        "external_status_checks": state.get("status_checks", {}),
    }



def context_budget_status(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    _, task = _current_task(root)
    budget = task.setdefault("context_budget", context_budget.init_budget())
    return context_budget.status(budget)


def context_budget_extend(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    state, task = _current_task(root)
    if task.get("status") != "active":
        raise ValueError("Task is not active")
    budget = task.setdefault("context_budget", context_budget.init_budget())
    updated = context_budget.extend(
        budget,
        args.get("extra_chars", 0),
        args.get("extra_calls", 0),
        args.get("reason", ""),
    )
    state["task"] = task
    _save_state(root, state)
    return {
        "extended": True,
        "budget": updated,
        "guidance": "Use the added context only for the stated correctness reason.",
    }


def task_report(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    state = _load_state(root)
    task = state.get("task")
    if not isinstance(task, dict):
        raise ValueError("No task state is available")
    budget = task.get("context_budget") or context_budget.init_budget()
    checks = task.get("checks", {})
    return {
        "task_id": task.get("id"),
        "goal": task.get("goal"),
        "status": task.get("status"),
        "duration_seconds": round(
            (task.get("finished_at", time.time()) - task.get("started_at", time.time())),
            2,
        ),
        "required_checks": task.get("required_checks", []),
        "checks": checks,
        "deliverables": task.get("deliverables", []),
        "commands": task.get("commands", 0),
        "command_output_chars": task.get("output_chars", 0),
        "context_budget": context_budget.status(budget),
        "external_status_checks": state.get("status_checks", {}),
        "note": (
            "Context metrics cover Sentinel-returned context only; they are not ChatGPT/Codex token or quota measurements."
        ),
    }


def _validate_argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise ValueError("argv must be a non-empty list of strings")
    if len(value) > 40:
        raise ValueError("argv has too many elements")
    return value


def _exe_name(argv: list[str]) -> str:
    return Path(argv[0]).name.lower()


def _classify_command(argv: list[str]) -> str:
    """Allow only common build/test/status commands; Sentinel is not a general shell."""
    exe = _exe_name(argv)
    args = [x.lower() for x in argv[1:]]

    if exe in {"gradlew", "gradlew.bat", "gradle", "gradle.bat"}:
        if any("assemble" in x or "bundle" in x or x in {"build", "test", "check"} for x in args):
            return "build"
    if exe in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"}:
        joined = " ".join(args)
        if any(word in joined for word in (" test", "build", "lint", "typecheck")) or (args and args[0] in {"test", "build"}):
            return "build"
    if exe in {"cargo", "go", "dotnet"} and args and args[0] in {"build", "test", "check"}:
        return "build"
    if exe in {"python", "python.exe", "py", "py.exe"} and len(args) >= 2 and args[0] == "-m" and args[1] in {"pytest", "unittest", "compileall"}:
        return "build" if args[1] == "compileall" else "test"
    if exe == "git" and args and args[0] in {"status", "diff", "show", "rev-parse"}:
        return "read"
    if exe in {"railway", "railway.exe"} and args and args[0] in {"status", "logs"}:
        return "external_status"
    raise ValueError(
        "Command is outside Sentinel's allowlist. Use the normal Codex terminal for deliberate "
        "one-off commands; Sentinel only governs common build/test/read/status operations."
    )


def _require_kind(argv: list[str], allowed: set[str]) -> str:
    kind = _classify_command(argv)
    if kind not in allowed:
        raise ValueError(f"Command kind {kind!r} is not allowed for this tool")
    return kind


def _run_process(root: Path, argv: list[str], timeout: int) -> dict[str, Any]:
    timeout = min(max(int(timeout), 1), MAX_COMMAND_SECONDS)
    started = time.time()
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        timed_out = False
        returncode = result.returncode
        combined = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        combined = out + (("\n" + err) if err else "")

    combined = _redact_sensitive_text(combined)
    clipped, truncated = _clip(combined)
    return {
        "argv": argv,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 2),
        "output": clipped,
        "output_truncated": truncated,
    }


def run_bounded_command(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    argv = _validate_argv(args.get("argv"))
    timeout = min(max(int(args.get("timeout_seconds", 300)), 1), MAX_COMMAND_SECONDS)
    _require_kind(argv, {"build", "test", "read"})
    state, task = _current_task(root)

    if task.get("status") != "active":
        raise ValueError("Task is not active; begin a new task before running commands")

    before = _source_fingerprint(root)
    result = _run_process(root, argv, timeout)
    after = _source_fingerprint(root)
    command_id = uuid.uuid4().hex
    result["command_id"] = command_id
    records = task.setdefault("command_records", {})
    records[command_id] = {
        "argv": argv, "returncode": result["returncode"], "timed_out": result["timed_out"],
        "source_fingerprint": after, "inputs_unchanged": before == after,
        "output_sha256": hashlib.sha256(result["output"].encode("utf-8")).hexdigest(),
        "recorded_at": time.time(),
    }
    task["command_records"] = dict(list(records.items())[-30:])
    task["commands"] = int(task.get("commands", 0)) + 1
    task["output_chars"] = int(task.get("output_chars", 0)) + len(result["output"])
    state["task"] = task
    _save_state(root, state)

    result["guidance"] = (
        "If the requested deliverable already exists and required verification is complete, "
        "do not run additional commands; call task_finish."
    )
    return result


def build_artifact(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    argv = _validate_argv(args.get("argv"))
    pattern = args.get("artifact_glob", "")
    check_name = args.get("check_name", "build")
    timeout = min(max(int(args.get("timeout_seconds", 720)), 1), MAX_COMMAND_SECONDS)

    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("artifact_glob is required")
    if not isinstance(check_name, str) or not check_name.strip():
        raise ValueError("check_name must be non-empty")

    _require_kind(argv, {"build"})
    state, task = _current_task(root)
    if task.get("status") != "active":
        raise ValueError("Task is not active")

    before_artifacts = _artifact_snapshot(root, pattern)
    before_inputs = _source_fingerprint(root, list(before_artifacts))
    result = _run_process(root, argv, timeout)
    after_artifacts = _artifact_snapshot(root, pattern)
    exclusions = sorted(set(before_artifacts) | set(after_artifacts))
    after_inputs = _source_fingerprint(root, exclusions)
    signature = hashlib.sha256(json.dumps({"argv": argv, "glob": pattern,
                                          "inputs": after_inputs}, sort_keys=True).encode()).hexdigest()
    previous = state.get("build_records", {}).get(signature, {})
    artifacts: list[dict[str, Any]] = []
    rejected = []
    for relative, item in after_artifacts.items():
        old = before_artifacts.get(relative)
        changed = old is None or any(old[k] != item[k] for k in ("sha256", "size", "mtime_ns"))
        reuse = previous.get(relative) == item["sha256"]
        if changed or reuse:
            artifacts.append({**item, "fresh_for_task": changed,
                              "provenance": "produced_by_command" if changed else "verified_reuse"})
        else:
            rejected.append({"path": relative, "reason": "Unchanged artifact has no matching successful build record"})
    artifacts.sort(key=lambda x: x["mtime"], reverse=True)

    passed = (result["returncode"] == 0 and not result["timed_out"]
              and bool(artifacts) and before_inputs == after_inputs)
    evidence = {
        "status": "pass" if passed else "fail",
        "evidence_kind": "build",
        "source_fingerprint": after_inputs,
        "input_exclusions": exclusions,
        "evidence_files": [{"path": a["path"], "sha256": a["sha256"]} for a in artifacts],
        "verified_by": "sentinel_build",
        "evidence": (
            f"command exit={result['returncode']}; artifacts="
            + ", ".join(a["path"] for a in artifacts[:10])
        ),
        "recorded_at": time.time(),
    }
    task.setdefault("checks", {})[check_name] = evidence
    if passed:
        records = state.setdefault("build_records", {})
        records[signature] = {a["path"]: a["sha256"] for a in artifacts}
        state["build_records"] = dict(list(records.items())[-20:])
        known = {x["path"]: x for x in task.setdefault("deliverables", [])}
        known.update({a["path"]: a for a in artifacts[:10]})
        task["deliverables"] = list(known.values())
    task["commands"] = int(task.get("commands", 0)) + 1
    task["output_chars"] = int(task.get("output_chars", 0)) + len(result["output"])
    state["task"] = task
    _save_state(root, state)

    return {
        **result,
        "artifacts": artifacts[:10],
        "rejected_artifacts": rejected[:10],
        "inputs_unchanged": before_inputs == after_inputs,
        "check": evidence,
        "stop_recommendation": (
            "BUILD_COMPLETE: successful command produced or verifiably reused the artifact for unchanged inputs."
            if passed else
            "Build or artifact verification failed; inspect the bounded output before retrying."
        ),
    }


def artifact_status(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    pattern = args.get("artifact_glob", "")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("artifact_glob is required")
    found = []
    for candidate in root.glob(pattern):
        try:
            if candidate.is_file():
                stat = candidate.stat()
                found.append({
                    "path": str(candidate.relative_to(root)),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
        except OSError:
            continue
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return {"artifacts": found[:20], "count": len(found)}


def external_status(args: dict[str, Any]) -> dict[str, Any]:
    """Bounded status check for asynchronous systems such as Railway."""
    root = _resolve_root(args.get("repo_path"))
    argv = _validate_argv(args.get("argv"))
    key = args.get("key", "deployment")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", key):
        raise ValueError("key must contain only letters, numbers, dot, underscore, or dash")

    _require_kind(argv, {"external_status"})
    state, task = _current_task(root)
    if task.get("status") != "active":
        raise ValueError("Task is not active; external polling is closed after completion")
    checks = state.setdefault("status_checks", {})
    used = int(checks.get(key, 0))
    if used >= MAX_STATUS_CHECKS:
        return {
            "blocked": True,
            "reason": "POLLING_BUDGET_EXHAUSTED",
            "checks_used": used,
            "max_checks": MAX_STATUS_CHECKS,
            "guidance": (
                "The external system is asynchronous. Stop polling in this task unless the user "
                "explicitly requests continued verification."
            ),
        }

    result = _run_process(root, argv, min(int(args.get("timeout_seconds", 60)), 60))
    checks[key] = used + 1
    task["commands"] = int(task.get("commands", 0)) + 1
    task["output_chars"] = int(task.get("output_chars", 0)) + len(result["output"])
    state["task"] = task
    state["status_checks"] = checks
    _save_state(root, state)

    return {
        **result,
        "blocked": False,
        "checks_used": checks[key],
        "checks_remaining": MAX_STATUS_CHECKS - checks[key],
        "guidance": (
            "Do not repeatedly poll asynchronous deployments. A second status check is the hard limit for this task."
        ),
    }


def record_verification(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    name = args.get("name", "")
    status = args.get("status", "")
    evidence = args.get("evidence", "")

    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if status not in {"pass", "fail", "unverified"}:
        raise ValueError("status must be pass, fail, or unverified")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("evidence is required")

    state, task = _current_task(root)
    if task.get("status") != "active":
        raise ValueError("Task is not active; verification evidence is immutable after completion")
    name = name.strip()
    kind = args.get("evidence_kind")
    details: dict[str, Any] = {}
    if status == "pass":
        if kind == "command":
            command_id = args.get("command_id")
            record = task.get("command_records", {}).get(command_id)
            if not record or record["returncode"] != 0 or record["timed_out"] or not record["inputs_unchanged"]:
                raise ValueError("Passing command evidence requires a successful Sentinel command_id with unchanged inputs")
            if record["source_fingerprint"] != _source_fingerprint(root):
                raise ValueError("Repository inputs changed after the command; rerun the verification")
            details = {"command_id": command_id, "verified_by": "sentinel_process",
                       "source_fingerprint": record["source_fingerprint"], "command": record}
        elif kind == "observation":
            paths = args.get("evidence_paths")
            if not isinstance(paths, list) or not paths or len(paths) > 10 or any(not isinstance(p, str) for p in paths):
                raise ValueError("Observation evidence requires 1-10 repository-relative evidence_paths")
            files = []
            for relative in paths:
                path = _contained_file(root, relative)
                if path.stat().st_size == 0:
                    raise ValueError("Evidence files must not be empty")
                files.append({"path": path.relative_to(root).as_posix(), "sha256": _sha256_file(path)})
            details = {"verified_by": "reported_observation", "evidence_files": files,
                       "source_fingerprint": _source_fingerprint(root),
                       "limitation": "Sentinel verifies file existence and integrity, not the truth of the observation"}
        else:
            raise ValueError("Passing evidence requires evidence_kind command or observation; text alone cannot pass")
    existing = task.get("checks", {}).get(name, {})
    if existing.get("evidence_kind") == "build" and status == "pass":
        raise ValueError("Build checks must be updated by build_artifact")
    task.setdefault("checks", {})[name] = {
        "status": status,
        "evidence": evidence[:4000],
        "evidence_kind": kind,
        **details,
        "recorded_at": time.time(),
    }
    state["task"] = task
    _save_state(root, state)
    return {
        "name": name,
        "status": status,
        "required": name in task.get("required_checks", []),
    }


def task_finish(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_root(args.get("repo_path"))
    state, task = _current_task(root)

    required = task.get("required_checks", [])
    checks = task.get("checks", {})
    if not required or not task.get("acceptance") or any(not isinstance(x, str) or not x.strip() for x in required):
        return {"finished": False, "reason": "EMPTY_VERIFICATION_PLAN",
                "guidance": "Begin a task with observable acceptance criteria and at least one required check"}
    missing = [name for name in required if checks.get(name, {}).get("status") != "pass"]
    missing += [name for name, check in checks.items() if check.get("status") == "fail" and name not in missing]
    stale = {name: reason for name, check in checks.items() if check.get("status") == "pass"
             and (reason := _check_freshness(root, check))}
    missing += [name for name in stale if name not in missing]
    if missing:
        return {
            "finished": False,
            "reason": "REQUIRED_VERIFICATION_INCOMPLETE",
            "missing_or_failed": missing,
            "stale_evidence": stale,
            "checks": checks,
            "guidance": (
                "Do not claim full completion. Verify these checks or report them explicitly as unverified."
            ),
        }

    task["status"] = "complete"
    task["finished_at"] = time.time()
    state["task"] = task
    _save_state(root, state)
    return {
        "finished": True,
        "task_id": task.get("id"),
        "deliverables": task.get("deliverables", []),
        "commands": task.get("commands", 0),
        "output_chars": task.get("output_chars", 0),
        "guidance": "TASK_COMPLETE. Do not run additional commands unless new work is requested or evidence becomes invalid.",
    }


TOOLS = {
    "repo_overview": (repo_overview, "Summarize bounded source areas, branch, and HEAD for a local Git repository."),
    "repo_search": (repo_search, "Find bounded literal matches in tracked source files without dumping whole files."),
    "symbol_context": (symbol_context, "Locate likely symbol definitions and bounded references; heuristic, not a full AST graph."),
    "repo_changes": (repo_changes, "List changed and untracked files relative to a Git ref."),
    "semantic_refresh": (semantic_refresh, "Build or incrementally refresh the persistent local semantic index for this repository."),
    "semantic_status": (semantic_status, "Show whether the persistent semantic index exists and whether it is stale against HEAD."),
    "semantic_find": (semantic_find, "Query indexed symbol definitions and reference sites without rescanning the whole repository."),
    "dependency_context": (dependency_context, "Return candidate definitions, consumers, imports, and files related to an indexed symbol."),
    "change_impact": (change_impact, "Estimate candidate affected files from symbols defined in changed paths using the persistent index."),
    "context_bundle": (context_bundle, "Rank a compact set of likely relevant files for the task, refreshing semantic memory when stale."),
    "task_begin": (task_begin, "Begin a governed task with acceptance criteria and required verification checks."),
    "task_status": (task_status, "Read the current governed task, evidence, deliverables, polling usage, and context budget."),
    "context_budget_status": (context_budget_status, "Show Sentinel context-call/output pressure for the active task."),
    "context_budget_extend": (context_budget_extend, "Explicitly extend the active task context budget when correctness requires more repository context."),
    "task_report": (task_report, "Return a compact task completion, verification, process, and context-budget report."),
    "run_bounded_command": (run_bounded_command, "Run one allowlisted local build/test/read command with a hard timeout and capped output."),
    "build_artifact": (build_artifact, "Run an allowlisted bounded build and verify that the expected artifact exists before returning."),
    "artifact_status": (artifact_status, "Inspect matching artifacts without starting or polling a build process."),
    "external_status": (external_status, "Run a bounded Railway status/logs command; at most two checks per task key."),
    "record_verification": (record_verification, "Record pass/fail/unverified evidence for a task verification check."),
    "task_finish": (task_finish, "Finish only when every required verification check has passing evidence."),
}


def _schema_for(name: str) -> dict[str, Any]:
    common_repo = {"repo_path": {"type": "string"}}
    properties: dict[str, Any] = dict(common_repo)
    required = ["repo_path"]

    if name == "repo_search":
        properties.update({"term": {"type": "string"}, "max_matches": {"type": "integer", "minimum": 1, "maximum": MAX_MATCHES}})
        required.append("term")
    elif name == "symbol_context":
        properties["symbol"] = {"type": "string"}
        required.append("symbol")
    elif name == "repo_changes":
        properties["base"] = {"type": "string", "default": "HEAD"}
    elif name == "semantic_find":
        properties.update({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        })
        required.append("query")
    elif name == "dependency_context":
        properties.update({
            "symbol": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60},
        })
        required.append("symbol")
    elif name == "change_impact":
        properties.update({
            "paths": {"type": "array", "items": {"type": "string"}},
            "base": {"type": "string", "default": "HEAD"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60},
        })
    elif name == "context_bundle":
        properties.update({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 3, "maximum": 25},
            "refresh_if_stale": {"type": "boolean", "default": True},
        })
        required.append("query")
    elif name == "task_begin":
        properties.update({
            "goal": {"type": "string"},
            "acceptance": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "required_checks": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "max_context_chars": {"type": "integer", "minimum": 20000, "maximum": 500000},
            "max_context_calls": {"type": "integer", "minimum": 5, "maximum": 80},
        })
        required += ["goal", "acceptance", "required_checks"]
    elif name == "context_budget_extend":
        properties.update({
            "extra_chars": {"type": "integer", "minimum": 0, "maximum": 120000},
            "extra_calls": {"type": "integer", "minimum": 0, "maximum": 20},
            "reason": {"type": "string"},
        })
        required += ["extra_chars", "extra_calls", "reason"]
    elif name == "run_bounded_command":
        properties.update({
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_COMMAND_SECONDS},
        })
        required.append("argv")
    elif name == "build_artifact":
        properties.update({
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "artifact_glob": {"type": "string"},
            "check_name": {"type": "string", "default": "build"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_COMMAND_SECONDS},
        })
        required += ["argv", "artifact_glob"]
    elif name == "artifact_status":
        properties["artifact_glob"] = {"type": "string"}
        required.append("artifact_glob")
    elif name == "external_status":
        properties.update({
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "key": {"type": "string", "default": "deployment"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
        })
        required.append("argv")
    elif name == "record_verification":
        properties.update({
            "name": {"type": "string"},
            "status": {"type": "string", "enum": ["pass", "fail", "unverified"]},
            "evidence": {"type": "string"},
            "evidence_kind": {"type": "string", "enum": ["command", "observation"]},
            "command_id": {"type": "string"},
            "evidence_paths": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "string"}},
        })
        required += ["name", "status", "evidence"]

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _send(payload: dict[str, Any]) -> None:
    # MCP stdio uses one UTF-8 JSON-RPC message per line. It is not LSP framing.
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if "\n" in data or "\r" in data:
        raise ValueError("Serialized MCP message must not contain embedded newlines")
    sys.stdout.buffer.write((data + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _receive() -> dict[str, Any] | None:
    # Read exactly one newline-delimited MCP JSON-RPC message.
    line = sys.stdin.buffer.readline(1_000_002)
    if not line:
        return None
    if len(line) > 1_000_001:
        raise ValueError("MCP request exceeds maximum line length")
    if not line.endswith(b"\n"):
        raise ValueError("MCP stdio request must be newline-delimited")
    raw = line.rstrip(b"\r\n")
    if not raw:
        raise ValueError("Empty MCP message")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON-RPC request must be an object")
    return payload



def _execute_tool(name: str, args: dict[str, Any]) -> Any:
    if name not in CONTEXT_TOOLS:
        return TOOLS[name][0](args)

    root = _resolve_root(args.get("repo_path"))
    state = _load_state(root)
    task = state.get("task")
    if not isinstance(task, dict):
        return TOOLS[name][0](args)
    if task.get("status") == "complete":
        return {
            "blocked": True,
            "reason": "TASK_ALREADY_COMPLETE",
            "guidance": "Start a new task if additional repository work is required.",
        }
    if task.get("status") != "active":
        return TOOLS[name][0](args)

    budget = task.setdefault("context_budget", context_budget.init_budget())
    generation = _worktree_generation(root)
    sig, blocked = context_budget.preflight(budget, name, args, generation)
    if blocked is not None:
        state["task"] = task
        _save_state(root, state)
        return blocked

    value = TOOLS[name][0](args)
    pressure = context_budget.record(budget, sig, name, value)
    state["task"] = task
    _save_state(root, state)

    if isinstance(value, dict):
        value = dict(value)
        value["_sentinel_context_budget"] = pressure
    return value


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    ident = request.get("id")

    if method == "initialize":
        requested = request.get("params", {}).get("protocolVersion", "2025-06-18")
        result = {
            "protocolVersion": requested,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "repo-context-verifier", "version": VERSION},
            "instructions": (
                "Start governed implementation work with task_begin. Prefer context_bundle for first-pass orientation, "
                "then semantic_find, dependency_context, or change_impact for focused follow-up. Exact duplicate context "
                "requests on an unchanged worktree are suppressed and context pressure is budgeted per task. Prefer "
                "build_artifact over manually waiting on build processes. external_status is limited to two checks per "
                "task key. Record direct evidence for required checks. If task_finish succeeds, stop running tools."
            ),
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": name, "description": description, "inputSchema": _schema_for(name)}
                for name, (_, description) in TOOLS.items()
            ]
        }
    elif method == "tools/call":
        params = request.get("params", {})
        name = params.get("name")
        if name not in TOOLS:
            raise ValueError("Unknown tool")
        try:
            value = _execute_tool(name, params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        except Exception as exc:
            result = {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }
    elif method == "ping":
        result = {}
    else:
        if ident is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": ident,
            "error": {"code": -32601, "message": "Method not found"},
        }

    if ident is None:
        return None
    return {"jsonrpc": "2.0", "id": ident, "result": result}


if __name__ == "__main__":
    while True:
        try:
            request = _receive()
            if request is None:
                break
            response = handle(request)
            if response is not None:
                _send(response)
        except Exception as exc:
            print(f"Sentinel MCP server error: {exc}", file=sys.stderr)
