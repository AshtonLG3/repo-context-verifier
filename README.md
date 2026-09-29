# Repo Context Verifier

Version 0.1.0. Local, read-only Codex plugin for bounded repository orientation and evidence-based verification.

The MCP server offers `repo_overview`, `repo_search`, and `repo_changes`. It reads a Git checkout supplied to each call, skips common generated paths and secret-like filenames, and caps results. It does not create a persistent graph or guarantee lower token use. No source is sent to a separate hosted indexing service by this plugin.

The `verify-repo-change` skill asks Codex to trace user-facing success and failure paths and verify the behavior at the relevant runtime level before saying a change is complete.

Requires Python 3 and Git on PATH. The compatibility manifest is `.codex-plugin/plugin.json`; `plugin.json` and `mcp.json` are portable manifests.
