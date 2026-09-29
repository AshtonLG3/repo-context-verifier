import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import server


class ServerTests(unittest.TestCase):
    def make_repo(self, root: Path):
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "scanner.py").write_text(
            "def show_toast():\n    return 'success'\n", encoding="utf-8"
        )
        (root / ".env").write_text("PRIVATE=secret\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "scanner.py", ".env"], check=True)
        subprocess.run([
            "git", "-C", str(root), "-c", "user.name=Test",
            "-c", "user.email=test@example.com", "commit", "-qm", "initial"
        ], check=True)

    def test_repo_context_is_bounded_and_skips_secret_like_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_repo(root)
            overview = server.repo_overview({"repo_path": str(root)})
            self.assertEqual(overview["source_files_scanned"], 1)
            found = server.repo_search({"repo_path": str(root), "term": "toast"})
            self.assertEqual([x["path"] for x in found["matches"]], ["scanner.py"])

    def test_task_finish_requires_passing_checks(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            self.make_repo(root)
            old = server.CACHE_ROOT
            server.CACHE_ROOT = Path(cache)
            try:
                server.task_begin({
                    "repo_path": str(root),
                    "goal": "Fix toast",
                    "acceptance": ["toast disappears"],
                    "required_checks": ["behavior"],
                })
                blocked = server.task_finish({"repo_path": str(root)})
                self.assertFalse(blocked["finished"])
                server.record_verification({
                    "repo_path": str(root),
                    "name": "behavior",
                    "status": "pass",
                    "evidence": "Observed disappearance in running UI",
                })
                self.assertTrue(server.task_finish({"repo_path": str(root)})["finished"])
            finally:
                server.CACHE_ROOT = old

    def test_build_artifact_marks_check_and_stops(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            self.make_repo(root)
            old = server.CACHE_ROOT
            server.CACHE_ROOT = Path(cache)
            try:
                server.task_begin({
                    "repo_path": str(root),
                    "goal": "Build artifact",
                    "acceptance": ["artifact exists"],
                    "required_checks": ["build"],
                })
                result = server.build_artifact({
                    "repo_path": str(root),
                    "argv": [sys.executable, "-m", "compileall", "scanner.py"],
                    "artifact_glob": "__pycache__/*.pyc",
                    "check_name": "build",
                    "timeout_seconds": 10,
                })
                self.assertEqual(result["check"]["status"], "pass")
                self.assertIn("BUILD_COMPLETE", result["stop_recommendation"])
                self.assertEqual(len(result["artifacts"][0]["sha256"]), 64)
                self.assertTrue(server.task_finish({"repo_path": str(root)})["finished"])
            finally:
                server.CACHE_ROOT = old

    def test_external_status_is_hard_limited(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            self.make_repo(root)
            old = server.CACHE_ROOT
            server.CACHE_ROOT = Path(cache)
            try:
                server.task_begin({
                    "repo_path": str(root),
                    "goal": "Deploy",
                    "acceptance": [],
                    "required_checks": [],
                })
                args = {"repo_path": str(root), "argv": ["railway", "status"], "key": "railway"}
                fake = {"argv": ["railway", "status"], "returncode": 0, "timed_out": False,
                        "duration_seconds": 0.01, "output": "healthy", "output_truncated": False}
                with patch.object(server, "_run_process", return_value=fake):
                    self.assertFalse(server.external_status(args)["blocked"])
                    self.assertFalse(server.external_status(args)["blocked"])
                    third = server.external_status(args)
                self.assertTrue(third["blocked"])
                self.assertEqual(third["reason"], "POLLING_BUDGET_EXHAUSTED")
            finally:
                server.CACHE_ROOT = old

    def test_command_policy_rejects_general_shell(self):
        with self.assertRaises(ValueError):
            server._classify_command(["powershell", "-Command", "Get-ChildItem"])

    def test_persistent_semantic_index_and_impact(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "service.py").write_text(
                "def clock_user():\n    return True\n", encoding="utf-8"
            )
            (root / "screen.py").write_text(
                "from service import clock_user\n\ndef tap():\n    return clock_user()\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(root), "add", "service.py", "screen.py"], check=True)
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Test",
                "-c", "user.email=test@example.com", "commit", "-qm", "initial"
            ], check=True)

            old = server.CACHE_ROOT
            server.CACHE_ROOT = Path(cache)
            try:
                refreshed = server.semantic_refresh({"repo_path": str(root)})
                self.assertEqual(refreshed["totals"]["files"], 2)
                found = server.semantic_find({"repo_path": str(root), "query": "clock_user"})
                self.assertTrue(any(x["path"] == "service.py" for x in found["definitions"]))
                context = server.dependency_context({"repo_path": str(root), "symbol": "clock_user"})
                self.assertIn("screen.py", context["candidate_files"])
                impact = server.change_impact({"repo_path": str(root), "paths": ["service.py"]})
                self.assertTrue(any(x["path"] == "screen.py" for x in impact["candidate_affected_files"]))
                status = server.semantic_status({"repo_path": str(root)})
                self.assertTrue(status["indexed"])
                self.assertFalse(status["stale"])
                bundle = server.context_bundle({
                    "repo_path": str(root),
                    "query": "clock user screen",
                    "limit": 5,
                })
                ranked_paths = [x["path"] for x in bundle["ranked_files"]]
                self.assertIn("service.py", ranked_paths)
                self.assertIn("screen.py", ranked_paths)
                (root / "service.py").write_text(
                    "def clock_user():\n    return False\n", encoding="utf-8"
                )
                dirty_status = server.semantic_status({"repo_path": str(root)})
                self.assertTrue(dirty_status["stale"])
                self.assertTrue(dirty_status["worktree_dirty"])
            finally:
                server.CACHE_ROOT = old

    def test_context_budget_suppresses_duplicate_requests_and_reports(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            self.make_repo(root)
            old = server.CACHE_ROOT
            server.CACHE_ROOT = Path(cache)
            try:
                server.task_begin({
                    "repo_path": str(root),
                    "goal": "Inspect toast behavior",
                    "acceptance": ["find toast implementation"],
                    "required_checks": [],
                    "max_context_chars": 20000,
                    "max_context_calls": 5,
                })
                first = server._execute_tool("repo_search", {
                    "repo_path": str(root),
                    "term": "toast",
                })
                self.assertIn("matches", first)
                self.assertIn("_sentinel_context_budget", first)

                duplicate = server._execute_tool("repo_search", {
                    "repo_path": str(root),
                    "term": "toast",
                })
                self.assertTrue(duplicate["blocked"])
                self.assertEqual(duplicate["reason"], "DUPLICATE_CONTEXT_SUPPRESSED")

                status = server.context_budget_status({"repo_path": str(root)})
                self.assertEqual(status["calls_used"], 1)
                self.assertEqual(status["duplicates_suppressed"], 1)

                extended = server.context_budget_extend({
                    "repo_path": str(root),
                    "extra_chars": 20000,
                    "extra_calls": 3,
                    "reason": "Need to trace a separate error path before implementation.",
                })
                self.assertTrue(extended["extended"])
                report = server.task_report({"repo_path": str(root)})
                self.assertEqual(report["context_budget"]["extensions_used"], 1)
                self.assertIn("not ChatGPT/Codex token", report["note"])
            finally:
                server.CACHE_ROOT = old

    def test_stop_controller_blocks_context_after_completion(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            self.make_repo(root)
            old = server.CACHE_ROOT
            server.CACHE_ROOT = Path(cache)
            try:
                server.task_begin({
                    "repo_path": str(root),
                    "goal": "Inspect and finish",
                    "acceptance": [],
                    "required_checks": [],
                })
                self.assertTrue(server.task_finish({"repo_path": str(root)})["finished"])
                blocked = server._execute_tool("repo_search", {
                    "repo_path": str(root),
                    "term": "toast",
                })
                self.assertTrue(blocked["blocked"])
                self.assertEqual(blocked["reason"], "TASK_ALREADY_COMPLETE")
                with self.assertRaises(ValueError):
                    server.external_status({
                        "repo_path": str(root),
                        "argv": ["railway", "status"],
                        "key": "railway",
                    })
            finally:
                server.CACHE_ROOT = old

    def test_mcp_lists_control_tools(self):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in response["result"]["tools"]}
        self.assertTrue({"build_artifact", "external_status", "task_finish", "symbol_context", "semantic_refresh", "semantic_find", "dependency_context", "change_impact", "context_bundle", "context_budget_status", "context_budget_extend", "task_report"} <= names)

    def test_stdio_initialization_uses_newline_delimited_jsonrpc(self):
        request = json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"}
        }).encode() + b"\n"
        result = subprocess.run(
            [sys.executable, str(HERE / "server.py")],
            input=request, capture_output=True, timeout=5, check=True
        )
        self.assertNotIn(b"Content-Length:", result.stdout)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["result"]["serverInfo"]["version"], "1.0.1")
        self.assertIn("stop running tools", payload["result"]["instructions"])

    def test_returned_source_context_redacts_common_secret_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "config.py").write_text(
                'api_key = "sk-1234567890ABCDEFGHIJKLMNOP"\n'
                'password = "super-secret-password"\n'
                'safe_value = "hello"\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "config.py"], check=True)
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Test",
                "-c", "user.email=test@example.com", "commit", "-qm", "initial"
            ], check=True)

            key = server.repo_search({"repo_path": str(root), "term": "api_key"})
            password = server.repo_search({"repo_path": str(root), "term": "password"})
            self.assertIn("[REDACTED]", key["matches"][0]["text"])
            self.assertNotIn("1234567890ABCDEFGHIJKLMNOP", key["matches"][0]["text"])
            self.assertIn("[REDACTED]", password["matches"][0]["text"])
            self.assertNotIn("super-secret-password", password["matches"][0]["text"])


if __name__ == "__main__":
    unittest.main()
