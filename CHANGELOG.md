# Changelog

## 1.1.0

- Reject empty or blank acceptance criteria and required checks, including legacy empty completion plans.
- Require passing verification to reference a successful Sentinel command or hashed observation files; text alone cannot pass.
- Recheck source inputs and evidence hashes at completion, and block any recorded failure.
- Compare artifacts before and after builds. Unchanged artifacts pass only with a prior successful record for the same command, inputs, and artifact hash.
- Label reported observations explicitly: file integrity does not prove the observation is true.
- Add regression tests for old artifacts, cached builds, tampering, stale verification, empty plans, and invented command evidence.
- Integrate semantic freshness and protocol fixes from 1.0.2; explicitly superseded tasks preserve their failed/unfinished evidence in history.

## 1.0.2

- Purge stale semantic rows when indexed files are deleted or become unreadable
- Include non-ignored untracked source files in semantic refreshes
- Prevent a new task from silently replacing an active task
- Negotiate only explicitly supported handshake-era MCP versions and counter-offer 2025-11-25 for unknown/modern initialize requests
- Clarify that the command allowlist is a process governor, not an OS sandbox
- Add regression tests for deletion, untracked indexing, active-task replacement, and protocol negotiation


## 1.0.1

- Fixed stdio transport to use newline-delimited UTF-8 JSON-RPC as required by MCP instead of `Content-Length` framing
- Replaced the self-confirming framing test with newline-delimited transport coverage
- Added an end-to-end test through the official MCP Python client SDK
- Added best-effort redaction for common credentials in returned source snippets and governed command output
- Kept runtime dependencies standard-library-only; the MCP SDK is test-only

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
