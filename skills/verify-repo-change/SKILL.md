---
name: verify-repo-change
description: Govern Codex repository changes with persistent semantic context, adaptive context budgets, bounded processes, artifact-aware stopping, and evidence-based completion.
---

# Govern a repository change with Sentinel

1. Confirm the canonical checkout and branch. Do not operate on an accidental duplicate checkout.
2. Translate the request into observable acceptance criteria. Include success, failure, reset and repeat behaviour when relevant.
3. Start with `task_begin`. Required checks must represent what must actually be established before claiming completion.
4. Use `context_bundle` for first-pass orientation. It may refresh stale semantic memory automatically and should replace broad exploratory reading.
5. Use `semantic_find`, `dependency_context`, or `change_impact` for focused follow-up. Use `repo_search` or `symbol_context` only when semantic evidence is insufficient.
6. Respect the context budget. Do not evade duplicate suppression by cosmetically changing the same query. Extend the budget only when more repository context is necessary for correctness and state why.
7. Implement the smallest complete change. Use `repo_changes` to detect unrelated edits.
8. Prefer `run_bounded_command` over open-ended process waiting.
9. When a build must produce an artifact, use `build_artifact`. If it returns `BUILD_COMPLETE`, the artifact exists and the build command succeeded. Do not continue polling Gradle, npm or another build process merely to watch it.
10. For Railway or another asynchronous external service, use `external_status`. It is intentionally limited to two reads per task key. Do not work around the limit by changing keys for the same deployment.
11. Verify behaviour at the most direct available level. Unit tests, HTTP responses, artifact existence, browser observation, physical-device observation and production deployment are different evidence levels.
12. Record actual evidence with `record_verification`. Do not upgrade indirect evidence into certainty.
13. Call `task_finish`. If it refuses, report the missing or unverified behaviour rather than claiming full completion.
14. When `task_finish` succeeds, stop. Use `task_report` for the compact final summary. Start a new task only if the user requests additional work or new evidence invalidates completion.

A high-reasoning model is compatible with this workflow. Sentinel exists to reduce avoidable repository context, repeated reads and process waiting without lowering the reasoning ceiling.
