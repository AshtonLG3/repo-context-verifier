"""Small, read-only MCP server for bounded local repository orientation."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_FILES = 3000
MAX_MATCHES = 40
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".css", ".html"}
SKIP = {"node_modules", "dist", "build", ".git", ".next", "vendor", "coverage"}
SECRET = re.compile(r"(^|[._-])(env|secret|credential|token|key)([._-]|$)", re.I)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git command failed")
    return result.stdout


def files(root):
    listed = git(root, "ls-files", "-z").split("\0")
    return [p for p in listed if p and Path(p).suffix.lower() in SOURCE_SUFFIXES and
            not any(part in SKIP for part in Path(p).parts) and not SECRET.search(Path(p).name)][:MAX_FILES]


def resolve_root(value):
    if not value:
        raise ValueError("repo_path is required")
    requested = Path(value).expanduser().resolve(strict=True)
    root = Path(git(requested, "rev-parse", "--show-toplevel").strip()).resolve(strict=True)
    if requested != root and root not in requested.parents:
        raise ValueError("Path is outside the repository")
    return root


def overview(args):
    root = resolve_root(args.get("repo_path"))
    tracked = files(root)
    by_area = {}
    for path in tracked:
        area = Path(path).parts[0] if len(Path(path).parts) > 1 else "."
        by_area[area] = by_area.get(area, 0) + 1
    return {"root": str(root), "branch": git(root, "branch", "--show-current").strip(),
            "source_files_scanned": len(tracked), "truncated": len(tracked) == MAX_FILES,
            "areas": sorted(by_area.items(), key=lambda x: (-x[1], x[0]))[:30],
            "note": "File counts are structural orientation, not a dependency graph."}


def search(args):
    root = resolve_root(args.get("repo_path"))
    term = args.get("term", "")
    if not isinstance(term, str) or not 2 <= len(term) <= 100 or "\n" in term:
        raise ValueError("term must be 2-100 characters on one line")
    matches = []
    for path in files(root):
        try:
            with (root / path).open("r", encoding="utf-8", errors="replace") as stream:
                for number, line in enumerate(stream, 1):
                    if term.casefold() in line.casefold():
                        matches.append({"path": path, "line": number, "text": line.strip()[:200]})
                        if len(matches) >= MAX_MATCHES:
                            return {"matches": matches, "truncated": True}
        except OSError:
            continue
    return {"matches": matches, "truncated": False}


def changes(args):
    root = resolve_root(args.get("repo_path"))
    base = args.get("base", "HEAD")
    if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9_./~-]{1,100}", base):
        raise ValueError("Invalid Git base ref")
    changed = git(root, "diff", "--name-only", base, "--").splitlines()[:100]
    changed += git(root, "ls-files", "--others", "--exclude-standard").splitlines()[:100]
    changed = list(dict.fromkeys(changed))[:100]
    return {"root": str(root), "changed_files": changed,
            "note": "Lists changed files only. Inspect callers and user-facing paths before claiming impact coverage."}


TOOLS = {
    "repo_overview": (overview, "Summarize tracked source areas in a local Git repository."),
    "repo_search": (search, "Find bounded literal matches in tracked source files; returns paths and line excerpts."),
    "repo_changes": (changes, "List modified and untracked files relative to a Git ref."),
}


def send(payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
    sys.stdout.buffer.flush()


def receive():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", "replace").partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if not 0 < length <= 1_000_000:
        raise ValueError("Invalid request length")
    return json.loads(sys.stdin.buffer.read(length))


def handle(request):
    method = request.get("method")
    ident = request.get("id")
    if method == "initialize":
        result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "repo-context-verifier", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": name, "description": description,
                 "inputSchema": {"type": "object", "properties": {"repo_path": {"type": "string"},
                 **({"term": {"type": "string"}} if name == "repo_search" else {}),
                 **({"base": {"type": "string"}} if name == "repo_changes" else {})},
                 "required": ["repo_path"] + (["term"] if name == "repo_search" else [])}}
                 for name, (_, description) in TOOLS.items()]}
    elif method == "tools/call":
        params = request.get("params", {})
        name = params.get("name")
        if name not in TOOLS:
            raise ValueError("Unknown tool")
        try:
            value = TOOLS[name][0](params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value)}]}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    elif method == "ping":
        result = {}
    else:
        if ident is None:
            return None
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result} if ident is not None else None


if __name__ == "__main__":
    while True:
        try:
            request = receive()
            if request is None:
                break
            response = handle(request)
            if response is not None:
                send(response)
        except Exception as exc:
            print(f"MCP server error: {exc}", file=sys.stderr)
