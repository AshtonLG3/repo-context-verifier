# Repo Context Verifier — Sentinel MCP

**Version 1.1.0**

Sentinel is a local-first MCP control bridge for Codex repository work. It is designed for a specific failure mode: the strongest coding model can still waste expensive context rediscovering a repository, keep waiting after an APK is already built, repeatedly poll an asynchronous deployment, or declare success from green tests while a user-visible defect remains.

Sentinel does not replace the reasoning model. It governs the work around it.

## What Sentinel controls

### Persistent repository intelligence

Sentinel maintains a local SQLite semantic index under `~/.repo-context-verifier/` (or `SENTINEL_HOME`).

It incrementally stores:

- source files and content digests
- likely symbol definitions
- import relationships
- symbol reference sites
- repository HEAD metadata

The index is reused across sessions. Unchanged files are not reparsed.

Core tools:

- `semantic_refresh` — incrementally refresh persistent repo memory
- `semantic_status` — report index state and detect HEAD/worktree staleness
- `semantic_find` — query definitions and reference sites
- `dependency_context` — return candidate consumers, imports and related files
- `change_impact` — estimate likely blast-radius files from changed definitions
- `context_bundle` — rank a compact first-pass set of relevant files for a task

`context_bundle` is the preferred orientation tool. It can refresh stale semantic memory automatically and ranks files using changed-file, path, definition, reference and import signals.

### Adaptive context budgeting

Each governed task receives a local context budget. By default Sentinel allows:

- 120,000 returned context characters
- 24 context-producing tool calls

These are **not ChatGPT/Codex token or weekly quota measurements**. They are measurable local proxies for the amount of repository context Sentinel itself feeds back to the model.

Sentinel:

- tracks context calls and returned characters
- suppresses an exact duplicate context request while the worktree generation is unchanged
- reports pressure as `ok`, `warning` or `critical`
- blocks new context calls once the configured budget is exhausted
- permits at most two explicit budget extensions, each requiring a concrete reason

Tools:

- `context_budget_status`
- `context_budget_extend`
- `task_report`

This lets a high-reasoning model remain the brain while avoiding repeated repo discovery.

### Process governor

Sentinel is not a generic shell. Governed process tools use an allowlist, `shell=False`, hard timeouts and capped output.

Tools:

- `run_bounded_command`
- `build_artifact`
- `artifact_status`
- `external_status`

`build_artifact` snapshots matching files before and after a bounded build. It returns `BUILD_COMPLETE` only when the command succeeds, inputs stay unchanged, and an artifact is newly produced or updated. Unchanged cached output is accepted only when a prior successful build record matches the command, source inputs and artifact hash. An unrelated successful command cannot validate an old APK. Artifact existence does not prove Android signing, version, installation or runtime behavior; those need separate checks.

`external_status` is intentionally limited to two checks per task key. It is meant for asynchronous systems such as Railway where repeated polling can consume time and agent context without improving the code change.

### Verification and completion gate

A task begins with observable acceptance criteria and required evidence:

- `task_begin`
- `record_verification`
- `task_finish`

A green unit test is evidence that the test passed; it is not automatically evidence that the UI behaved correctly. Sentinel keeps those evidence levels distinct.

For user-facing work, verification can cover:

- success path
- failure path
- reset path
- repeat action
- browser/runtime observation
- physical-device observation
- artifact build
- production/deployment state

Every task requires at least one nonblank acceptance criterion and required check. `task_finish` refuses completion when a required check lacks passing evidence, any recorded check fails, source inputs changed after verification, or evidence files changed or disappeared. Legacy empty plans cannot complete.

Passing `record_verification` calls must provide an `evidence_kind`:

- `command`: reference a `command_id` returned by `run_bounded_command`. Sentinel verifies the command exited successfully, did not time out, and still applies to the current source inputs. This proves command success, not the truth of an arbitrary behavior claim.
- `observation`: supply `evidence_paths` containing 1–10 nonempty files inside the repository, such as screenshots or captured browser/device logs. Sentinel hashes these files and marks the record as a **reported observation**. It verifies the files' integrity, not what a screenshot or log means.

Text alone cannot pass. Build checks are recorded by `build_artifact` and cannot be replaced with a generic observation. Existing 1.0.1 evidence must be recorded again using the new fields.

After `task_finish` succeeds, the stop controller blocks further governed repository-context work and external polling for that task. New work should start a new task.

## Recommended Codex workflow

1. Call `task_begin` with the exact observable acceptance criteria and only the checks that must truly pass.
2. Call `context_bundle` with the task in plain language.
3. Use `semantic_find`, `dependency_context`, or `change_impact` only for focused follow-up.
4. Fall back to `repo_search` or `symbol_context` when semantic evidence is insufficient.
5. Implement the smallest complete change.
6. Use `repo_changes` to catch unrelated edits.
7. Run bounded tests/builds.
8. If an APK/package/binary is required, use `build_artifact` and stop build polling after `BUILD_COMPLETE`.
9. Use `external_status` sparingly for asynchronous deployment status.
10. Record direct evidence with `record_verification`.
11. Call `task_finish`.
12. Use `task_report` for the final compact verification/process/context summary.

## Installation

Requirements:

- Python 3.10+
- Git on `PATH`
- Codex or another MCP client that can launch a local stdio server

The runtime server uses only the Python standard library. CI also installs the official MCP Python client SDK so the stdio transport is tested end-to-end through a real MCP client.

Portable MCP configuration:

```json
{
  "mcpServers": {
    "repo-context-verifier": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/repo-context-verifier/server.py"]
    }
  }
}
```

The repository also includes Codex plugin manifests.

## Local data and privacy

Sentinel keeps its own state outside the target repository by default:

```text
~/.repo-context-verifier/
  tasks/
  semantic/
```

Set `SENTINEL_HOME` to move the cache.

Repository orientation skips common generated directories and secret-like filenames. Returned source snippets and governed command output also apply best-effort redaction for common credential assignments and token formats. This reduces accidental exposure but is not a complete secret scanner, so repositories should still keep credentials out of source. No hosted indexing service is required and Sentinel itself does not upload source code elsewhere.

The MCP client still has whatever access you explicitly give it, and governed build/status tools execute allowlisted local commands. Treat installation of any MCP server with process tools as code-execution access to the selected checkout.

## What Sentinel does not claim

Sentinel deliberately does **not** claim that:

- it can read the private ChatGPT/Codex weekly quota counter
- returned character counts equal tokens or credits
- its semantic index is a compiler-grade call graph
- static evidence proves user-visible runtime behaviour
- it can discover every dependency created through reflection, generated code, dynamic dispatch, dependency injection or framework magic
- installing Sentinel guarantees a particular percentage of quota savings

Those claims should be measured empirically on comparable tasks.

## Testing

Run locally:

```bash
python -m unittest -v
```

GitHub Actions runs the suite on Windows and Ubuntu with Python 3.10 and 3.12.

The suite covers:

- bounded repo context and secret-like filename skipping
- persistent semantic indexing
- ranked context selection and impact analysis
- worktree staleness detection
- adaptive context accounting
- duplicate-context suppression
- explicit budget extension
- artifact-aware stopping and SHA-256
- deployment polling limits
- command allowlisting
- required verification
- hard stop after task completion
- MCP tool discovery and stdio initialization

## Design principle

**Keep the strongest model where its reasoning matters. Spend less of its turn rediscovering, waiting and re-reading.**
