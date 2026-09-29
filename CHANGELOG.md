# Changelog

## 1.0.1

- Fixed stdio transport to use newline-delimited UTF-8 JSON-RPC as required by MCP instead of `Content-Length` framing
- Replaced the self-confirming framing test with newline-delimited transport coverage
- Added an end-to-end test through the official MCP Python client SDK
- Added best-effort redaction for common credentials in returned source snippets and governed command output
- Kept runtime dependencies standard-library-only; the MCP SDK is test-only

# Changelog

## 1.0.0

- Added adaptive per-task context budgeting
- Added exact duplicate-context suppression for unchanged worktree generations
- Added ranked `context_bundle` orientation
- Added explicit context-budget status and justified extension workflow
- Added compact final `task_report`
- Added artifact SHA-256 reporting
- Enforced hard stop of governed context and deployment polling after completion
- Added cross-platform GitHub Actions tests

## 0.3.0

- Added persistent local SQLite semantic repository memory
- Added incremental indexing of definitions, imports and references
- Added `semantic_find`, `dependency_context`, `change_impact` and stale-index detection

## 0.2.0

- Rebuilt the original proof of concept as a governed MCP bridge
- Added task state, required verification, bounded commands, artifact-aware build stopping and deployment polling limits

## 0.1.0

- Initial bounded repository orientation proof of concept
