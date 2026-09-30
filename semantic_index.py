"""Persistent local semantic index for Sentinel.

This module intentionally stays standard-library only. It builds a lightweight,
language-agnostic graph from tracked source files using definitions, imports,
references, and file-level edges. It is not a full compiler AST and reports that
limitation explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

DEFINITION_PATTERNS = [
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(r"\b(?:function|class|interface|enum|type)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"),
    re.compile(r"\b(?:fun|class|interface|object|enum\s+class)\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(r"\b(?:struct|trait|enum|fn)\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(r"\b(?:class|interface|record|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
]

IMPORT_PATTERNS = [
    re.compile(r"^\s*from\s+([A-Za-z0-9_\.]+)\s+import\s+"),
    re.compile(r"^\s*import\s+([A-Za-z0-9_\.]+)"),
    re.compile(r"\bfrom\s+[\"']([^\"']+)[\"']"),
    re.compile(r"\brequire\(\s*[\"']([^\"']+)[\"']\s*\)"),
    re.compile(r"^\s*import\s+([A-Za-z0-9_\.]+)"),
]

IDENTIFIER = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]{2,}\b")
MAX_REFS_PER_FILE = 5000

STOP_WORDS = {
    "return", "class", "function", "interface", "import", "export", "from", "const",
    "let", "var", "public", "private", "protected", "static", "async", "await", "while",
    "for", "with", "true", "false", "none", "null", "this", "self", "super", "extends",
    "implements", "package", "string", "number", "boolean", "object", "void", "data",
    "when", "where", "then", "else", "elif", "except", "finally", "yield", "break",
}


def db_path(cache_root: Path, repo_key: str) -> Path:
    root = cache_root / "semantic"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{repo_key}.sqlite3"


def connect(cache_root: Path, repo_key: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(cache_root, repo_key))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            digest TEXT NOT NULL,
            size INTEGER NOT NULL,
            indexed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER NOT NULL,
            kind TEXT NOT NULL,
            UNIQUE(name, path, line, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
        CREATE TABLE IF NOT EXISTS imports (
            source_path TEXT NOT NULL,
            target TEXT NOT NULL,
            line INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_imports_source ON imports(source_path);
        CREATE INDEX IF NOT EXISTS idx_imports_target ON imports(target);
        CREATE TABLE IF NOT EXISTS refs (
            symbol TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_refs_symbol ON refs(symbol);
        CREATE INDEX IF NOT EXISTS idx_refs_path ON refs(path);
        """
    )
    return conn


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def _symbol_kind(line: str) -> str:
    lowered = line.lower()
    for kind in ("class", "interface", "enum", "function", "def", "fun", "fn", "struct", "trait", "type", "object"):
        if re.search(rf"\b{re.escape(kind)}\b", lowered):
            return kind
    return "symbol"


def parse_source(path: str, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []

    for number, line in enumerate(text.splitlines(), 1):
        for pattern in DEFINITION_PATTERNS:
            match = pattern.search(line)
            if match:
                symbols.append({
                    "name": match.group(1),
                    "path": path,
                    "line": number,
                    "kind": _symbol_kind(line),
                })
                break

        for pattern in IMPORT_PATTERNS:
            match = pattern.search(line)
            if match:
                imports.append({
                    "source_path": path,
                    "target": match.group(1),
                    "line": number,
                })
                break

        seen: set[str] = set()
        for ident in IDENTIFIER.findall(line):
            lowered = ident.lower()
            if lowered in STOP_WORDS or ident in seen:
                continue
            seen.add(ident)
            if len(refs) < MAX_REFS_PER_FILE:
                refs.append({"symbol": ident, "path": path, "line": number})

    return symbols, imports, refs


def refresh_index(
    cache_root: Path,
    repo_key: str,
    root: Path,
    files: Iterable[str],
    head: str,
) -> dict[str, Any]:
    conn = connect(cache_root, repo_key)
    started = time.time()
    scanned = 0
    changed = 0
    symbols_written = 0
    imports_written = 0
    refs_written = 0
    live_paths: set[str] = set()

    try:
        for rel in files:
            full = root / rel
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
                stat = full.stat()
            except OSError:
                # Do not mark unreadable/missing paths live. If they were indexed
                # previously, the cleanup pass below must remove their stale rows.
                continue
            live_paths.add(rel)
            scanned += 1
            digest = _digest(text)
            old = conn.execute("SELECT digest FROM files WHERE path=?", (rel,)).fetchone()
            if old and old["digest"] == digest:
                continue

            changed += 1
            conn.execute("DELETE FROM symbols WHERE path=?", (rel,))
            conn.execute("DELETE FROM imports WHERE source_path=?", (rel,))
            conn.execute("DELETE FROM refs WHERE path=?", (rel,))

            symbols, imports, refs = parse_source(rel, text)
            conn.executemany(
                "INSERT OR IGNORE INTO symbols(name,path,line,kind) VALUES(?,?,?,?)",
                [(x["name"], x["path"], x["line"], x["kind"]) for x in symbols],
            )
            conn.executemany(
                "INSERT INTO imports(source_path,target,line) VALUES(?,?,?)",
                [(x["source_path"], x["target"], x["line"]) for x in imports],
            )
            conn.executemany(
                "INSERT INTO refs(symbol,path,line) VALUES(?,?,?)",
                [(x["symbol"], x["path"], x["line"]) for x in refs],
            )
            conn.execute(
                "INSERT INTO files(path,digest,size,indexed_at) VALUES(?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET digest=excluded.digest,size=excluded.size,indexed_at=excluded.indexed_at",
                (rel, digest, stat.st_size, time.time()),
            )
            symbols_written += len(symbols)
            imports_written += len(imports)
            refs_written += len(refs)

        indexed_paths = {row["path"] for row in conn.execute("SELECT path FROM files")}
        removed = sorted(indexed_paths - live_paths)
        for rel in removed:
            conn.execute("DELETE FROM files WHERE path=?", (rel,))
            conn.execute("DELETE FROM symbols WHERE path=?", (rel,))
            conn.execute("DELETE FROM imports WHERE source_path=?", (rel,))
            conn.execute("DELETE FROM refs WHERE path=?", (rel,))

        conn.execute(
            "INSERT INTO meta(key,value) VALUES('head',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (head,),
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('updated_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(time.time()),),
        )
        conn.commit()

        totals = {
            "files": conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"],
            "symbols": conn.execute("SELECT COUNT(*) c FROM symbols").fetchone()["c"],
            "imports": conn.execute("SELECT COUNT(*) c FROM imports").fetchone()["c"],
            "references": conn.execute("SELECT COUNT(*) c FROM refs").fetchone()["c"],
        }
        return {
            "head": head,
            "files_scanned": scanned,
            "files_reindexed": changed,
            "files_removed": len(removed),
            "symbols_written": symbols_written,
            "imports_written": imports_written,
            "references_written": refs_written,
            "totals": totals,
            "duration_seconds": round(time.time() - started, 3),
            "persistent": True,
            "note": "Lightweight semantic index; not a compiler-grade AST/call graph.",
        }
    finally:
        conn.close()


def index_status(cache_root: Path, repo_key: str) -> dict[str, Any]:
    path = db_path(cache_root, repo_key)
    if not path.exists():
        return {"indexed": False, "path": str(path)}
    conn = connect(cache_root, repo_key)
    try:
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM meta")}
        totals = {
            "files": conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"],
            "symbols": conn.execute("SELECT COUNT(*) c FROM symbols").fetchone()["c"],
            "imports": conn.execute("SELECT COUNT(*) c FROM imports").fetchone()["c"],
            "references": conn.execute("SELECT COUNT(*) c FROM refs").fetchone()["c"],
        }
        return {"indexed": True, "path": str(path), "meta": meta, "totals": totals}
    finally:
        conn.close()


def semantic_search(cache_root: Path, repo_key: str, query: str, limit: int = 30) -> dict[str, Any]:
    if not query or len(query) > 120:
        raise ValueError("query must be 1-120 characters")
    limit = max(1, min(int(limit), 50))
    conn = connect(cache_root, repo_key)
    try:
        exact = list(conn.execute(
            "SELECT name,path,line,kind FROM symbols WHERE lower(name)=lower(?) ORDER BY path,line LIMIT ?",
            (query, limit),
        ))
        remaining = max(0, limit - len(exact))
        partial = []
        if remaining:
            partial = list(conn.execute(
                "SELECT name,path,line,kind FROM symbols WHERE lower(name) LIKE lower(?) "
                "AND lower(name)<>lower(?) ORDER BY length(name),path,line LIMIT ?",
                (f"%{query}%", query, remaining),
            ))
        refs = list(conn.execute(
            "SELECT symbol,path,line FROM refs WHERE lower(symbol)=lower(?) ORDER BY path,line LIMIT ?",
            (query, limit),
        ))
        return {
            "query": query,
            "definitions": [dict(row) for row in exact + partial],
            "references": [dict(row) for row in refs],
            "note": "Definitions are heuristic. Confirm runtime/control-flow details in source.",
        }
    finally:
        conn.close()


def dependency_context(cache_root: Path, repo_key: str, symbol: str, limit: int = 40) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{1,119}", symbol):
        raise ValueError("symbol must be a simple identifier")
    limit = max(1, min(int(limit), 60))
    conn = connect(cache_root, repo_key)
    try:
        definitions = [dict(row) for row in conn.execute(
            "SELECT name,path,line,kind FROM symbols WHERE lower(name)=lower(?) ORDER BY path,line LIMIT ?",
            (symbol, limit),
        )]
        refs = [dict(row) for row in conn.execute(
            "SELECT symbol,path,line FROM refs WHERE lower(symbol)=lower(?) ORDER BY path,line LIMIT ?",
            (symbol, limit),
        )]
        paths = sorted({x["path"] for x in definitions + refs})[:25]
        imports: list[dict[str, Any]] = []
        for path in paths:
            imports.extend(dict(row) for row in conn.execute(
                "SELECT source_path,target,line FROM imports WHERE source_path=? ORDER BY line LIMIT 20",
                (path,),
            ))
            if len(imports) >= limit:
                break
        return {
            "symbol": symbol,
            "definitions": definitions,
            "reference_sites": refs,
            "related_imports": imports[:limit],
            "candidate_files": paths,
            "note": "Reference sites approximate callers/consumers; dynamic dispatch and generated wiring may be missed.",
        }
    finally:
        conn.close()


def impact_for_paths(cache_root: Path, repo_key: str, changed_paths: list[str], limit: int = 60) -> dict[str, Any]:
    conn = connect(cache_root, repo_key)
    try:
        defined: set[str] = set()
        for path in changed_paths[:100]:
            defined.update(row["name"] for row in conn.execute(
                "SELECT name FROM symbols WHERE path=?",
                (path,),
            ))

        affected: dict[str, dict[str, Any]] = {}
        for symbol in sorted(defined):
            for row in conn.execute(
                "SELECT path,line FROM refs WHERE symbol=? ORDER BY path,line LIMIT ?",
                (symbol, limit),
            ):
                if row["path"] in changed_paths:
                    continue
                key = row["path"]
                item = affected.setdefault(key, {"path": key, "symbols": set(), "lines": []})
                item["symbols"].add(symbol)
                if len(item["lines"]) < 10:
                    item["lines"].append(row["line"])

        normalized = []
        for item in affected.values():
            normalized.append({
                "path": item["path"],
                "symbols": sorted(item["symbols"])[:20],
                "lines": item["lines"],
            })
        normalized.sort(key=lambda x: (-len(x["symbols"]), x["path"]))
        return {
            "changed_paths": changed_paths[:100],
            "defined_symbols_considered": sorted(defined)[:200],
            "candidate_affected_files": normalized[:limit],
            "note": "Impact is conservative heuristic evidence, not proof of complete dependency coverage.",
        }
    finally:
        conn.close()


def rank_context(
    cache_root: Path,
    repo_key: str,
    query: str,
    changed_paths: list[str] | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """Rank likely relevant files for a task/query using the persistent index."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    limit = max(3, min(int(limit), 25))
    changed = set(changed_paths or [])
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]{2,}", query)
        if token.lower() not in STOP_WORDS
    }
    if not tokens:
        tokens = {query.strip().lower()[:80]}

    conn = connect(cache_root, repo_key)
    try:
        scores: dict[str, float] = {}
        reasons: dict[str, set[str]] = {}
        symbols_by_path: dict[str, set[str]] = {}

        def bump(path: str, amount: float, reason: str, symbol: str | None = None) -> None:
            scores[path] = scores.get(path, 0.0) + amount
            reasons.setdefault(path, set()).add(reason)
            if symbol:
                symbols_by_path.setdefault(path, set()).add(symbol)

        for path in changed:
            bump(path, 8.0, "changed file")

        indexed_files = [row["path"] for row in conn.execute("SELECT path FROM files")]
        for path in indexed_files:
            lower_path = path.lower()
            for token in tokens:
                if token in lower_path:
                    bump(path, 4.0, f"path matches '{token}'")

        for token in tokens:
            exact = list(conn.execute(
                "SELECT name,path,line FROM symbols WHERE lower(name)=? LIMIT 80",
                (token,),
            ))
            for row in exact:
                bump(row["path"], 10.0, f"defines '{row['name']}'", row["name"])

            partial = list(conn.execute(
                "SELECT name,path,line FROM symbols WHERE lower(name) LIKE ? AND lower(name)<>? LIMIT 80",
                (f"%{token}%", token),
            ))
            for row in partial:
                bump(row["path"], 6.0, f"related symbol '{row['name']}'", row["name"])

            refs = list(conn.execute(
                "SELECT symbol,path,line FROM refs WHERE lower(symbol)=? LIMIT 120",
                (token,),
            ))
            for row in refs:
                bump(row["path"], 2.5, f"references '{row['symbol']}'", row["symbol"])

            imports = list(conn.execute(
                "SELECT source_path,target,line FROM imports WHERE lower(target) LIKE ? LIMIT 80",
                (f"%{token}%",),
            ))
            for row in imports:
                bump(row["source_path"], 2.0, f"imports '{row['target']}'")

        ranked = []
        for path, score in scores.items():
            ranked.append({
                "path": path,
                "score": round(score, 2),
                "reasons": sorted(reasons.get(path, set()))[:8],
                "symbols": sorted(symbols_by_path.get(path, set()))[:12],
                "changed": path in changed,
            })
        ranked.sort(key=lambda x: (-x["score"], not x["changed"], x["path"]))

        return {
            "query": query,
            "tokens": sorted(tokens),
            "ranked_files": ranked[:limit],
            "considered_files": len(indexed_files),
            "note": (
                "Ranking combines changed-file, path, definition, reference, and import signals. "
                "It is for context selection, not proof of dependency coverage."
            ),
        }
    finally:
        conn.close()
