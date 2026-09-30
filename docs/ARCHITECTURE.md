# Sentinel architecture

```text
Codex / reasoning model
        |
        | MCP stdio
        v
+-----------------------------+
| Sentinel server.py          |
|                             |
| Task + completion gate      |
| Context budget governor     |
| Process / polling governor  |
+-------------+---------------+
              |
      +-------+--------+
      |                |
      v                v
semantic_index.py   local commands
SQLite cache        Gradle/npm/tests
symbols/imports     Railway status
references          artifact checks
      |
      v
target Git checkout
```

## Control layers

### 1. Task state

A task records its goal, acceptance criteria, required checks, deliverables, command/output counters, external status checks and context budget.

### 2. Semantic memory

The semantic index is persisted outside the target repository. Refresh is incremental by file digest. It is intentionally language-agnostic and heuristic rather than a compiler frontend.

### 3. Context governor

Context-producing tools are measured by returned serialized characters and call count. Exact duplicate requests are fingerprinted against the current worktree generation and suppressed.

### 4. Process governor

Only a narrow allowlist of build/test/read/status commands is accepted by Sentinel process tools. Commands use `shell=False`, timeout bounds and output clipping. This constrains Sentinel's own command surface but is not sandboxing: a permitted repository build or test can execute repository-controlled code with the host account's permissions.

### 5. Verification gate

Required evidence must be recorded as passing before `task_finish` succeeds. Completion changes the task state to immutable/closed for governed context and external polling.

## Trust boundaries

Sentinel is local-first, but an MCP client with access to it can execute the server's allowlisted process tools in the selected repository. The target repository itself may contain build scripts that execute arbitrary code. Running a build therefore inherits the trust risk of running that repository's build normally.

## Non-goals

- exact token accounting
- reading ChatGPT subscription quota
- compiler-complete call graphs
- replacing device/browser verification
- replacing the reasoning model
