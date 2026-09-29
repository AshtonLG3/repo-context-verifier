---
name: verify-repo-change
description: Govern Codex repository changes with bounded context, process limits, artifact-aware stopping, and evidence-based completion.
---

# Verify a repository change with Sentinel

1. Confirm the canonical checkout and branch. Do not operate on an accidental duplicate checkout.
2. Translate the user's request into observable acceptance criteria. Include success, failure, reset and repeat behaviour when those paths matter.
3. Start with `task_begin`. Required checks must represent what must actually be established before claiming completion.
4. Use `repo_overview` at most once when orientation is needed. Prefer `repo_search` and `symbol_context` to broad file reads. The symbol tool is heuristic, so inspect real source before claiming dependency coverage.
5. Implement the smallest complete change. Use `repo_changes` to detect unrelated edits.
6. Prefer `run_bounded_command` over open-ended process waiting.
7. When a build must produce an artifact, use `build_artifact`. If it returns `BUILD_COMPLETE`, the artifact exists and the build command succeeded. Do not continue polling Gradle, npm, or another build process merely to watch it.
8. For Railway or another asynchronous external service, use `external_status`. It is intentionally limited to two status reads per task key. Do not work around the limit by changing keys for the same deployment.
9. Verify behaviour at the most direct available level. A unit test, HTTP response, APK existence, browser observation, physical-device observation, and production deployment are different evidence levels. Record what was actually established with `record_verification`.
10. Call `task_finish`. If it refuses because required evidence is missing, do not claim the task is fully complete. Report the missing or unverified behaviour precisely.
11. When `task_finish` succeeds, stop running tools unless the user requested additional work or new evidence invalidates completion.

Using a high-reasoning model is compatible with this workflow. The purpose of Sentinel is to reduce avoidable context/process waste without lowering the reasoning ceiling.
