# Repo Context Verifier — Sentinel Control Bridge

Version **0.3.0** adds persistent semantic repository memory to the v0.2 control bridge.

It is a local-first MCP control bridge for Codex repository work. The goal is not to replace a strong reasoning model. It is to stop that model wasting time and context on repeated repository orientation, unbounded command output, unnecessary Gradle/build waiting, and repeated polling of asynchronous deployments.

## What changed

v0.3 keeps the v0.2 task/process governor and adds a persistent local SQLite semantic index so Codex can reuse structural knowledge across sessions:

- `repo_overview` — bounded repository map, branch and HEAD
- `repo_search` — capped literal search across tracked source files
- `symbol_context` — heuristic symbol definitions and bounded references
- `repo_changes` — changed/untracked file list\n- `semantic_refresh` / `semantic_status` — incrementally maintain and inspect the persistent local index\n- `semantic_find` — query indexed definitions and references without rescanning the repo\n- `dependency_context` — return candidate consumers, definitions, imports and related files for a symbol\n- `change_impact` — estimate likely affected files from symbols defined in changed paths
- `task_begin` / `task_status` — persistent task state and acceptance criteria
- `run_bounded_command` — hard timeout and capped command output
- `build_artifact` — bounded build plus artifact verification
- `artifact_status` — check an artifact without rerunning a build
- `external_status` — deployment/status checks limited to two per task key
- `record_verification` — store pass/fail/unverified evidence
- `task_finish` — refuses completion while required verification is incomplete

Task state is stored outside the repository under `~/.repo-context-verifier/` by default. Set `SENTINEL_HOME` to override it.

## Why this exists

A build can already be useful while the agent keeps waiting on Gradle. A Railway deploy can already be triggered while the agent keeps polling it. A test suite can be green while a visible UI defect still exists.

Sentinel separates those concerns:

1. **Orient narrowly.** Return paths and evidence instead of dumping the repository.
2. **Run processes with bounds.** Commands have timeouts and output caps.
3. **Recognize deliverables.** A successful build plus a real APK/artifact is a stop condition.
4. **Bound asynchronous polling.** External status checks have a hard per-task ceiling.
5. **Require evidence.** `task_finish` cannot succeed until required checks are recorded as passing.
6. **Stop when complete.** A successful finish explicitly tells the agent not to keep running commands.

## Install

Requirements:

- Python 3.10+
- Git on `PATH`
- A Codex/MCP client that can launch a local stdio server

The repository includes portable MCP manifests. The server itself has no third-party Python dependencies.

## Recommended workflow

For implementation work:

1. Call `task_begin` with concrete observable acceptance criteria and the checks that must pass.
2. Use `repo_overview` once only if the repository is unfamiliar.
3. Use `repo_search` or `symbol_context` before opening broad files.
4. Make the smallest complete change.
5. Use `build_artifact` when the task expects an APK/package/binary. If it returns `BUILD_COMPLETE`, do not keep polling the build.
6. Use `external_status` only when an asynchronous service needs a status read. It allows at most two checks per task key.
7. Record direct evidence with `record_verification`.
8. Call `task_finish`. If it refuses, report the missing verification instead of claiming success.

## Verification philosophy

A green test proves the test passed. It does not automatically prove the user-visible behaviour is correct.

For UI/runtime changes, acceptance criteria should cover relevant success, failure, reset and repeat paths. Where direct device/browser verification is unavailable, record that state as `unverified` rather than upgrading indirect evidence into certainty.

## Security and privacy

- Source inspection is local.
- Secret-like filenames and common generated directories are skipped by repo context tools.
- Command execution uses argv arrays with `shell=False`.
- Command output is capped before being returned to the model.
- The server does not upload source to a hosted indexing service.
- Governed command tools intentionally execute only an allowlisted set of build/test/read/status commands. Sentinel is not a general shell.

## Limits

The semantic index is persistent and incremental, but it remains heuristic rather than a compiler-grade call graph. Dynamic dispatch, reflection, generated code and framework wiring may be missed. A later release can optionally bridge to Graphify/Graft/CodeGraph after separate privacy and effectiveness testing.

Sentinel also cannot read the private ChatGPT weekly quota counter. It controls measurable proxies instead: tool calls, command duration, output volume, artifact completion and deployment polling.

## Test

```bash
python -m unittest -v
```

The test suite covers bounded repo context, persistent semantic indexing and impact analysis, required verification, artifact-aware stopping, external polling limits, tool discovery and stdio initialization.
