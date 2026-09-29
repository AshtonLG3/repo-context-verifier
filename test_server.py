import json
import os
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
                (root / "observation.txt").write_text("Observed toast disappearance in the running UI", encoding="utf-8")
                server.record_verification({
                    "repo_path": str(root),
                    "name": "behavior",
                    "status": "pass",
                    "evidence": "Observed disappearance in running UI",
                    "evidence_kind": "observation",
                    "evidence_paths": ["observation.txt"],
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
                    "acceptance": ["Deployment status inspected"],
                    "required_checks": ["deployment"],
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
                    "required_checks": ["inspection"],
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
                    "acceptance": ["Repository status inspected"],
                    "required_checks": ["inspection"],
                })
                command = server.run_bounded_command({"repo_path": str(root), "argv": ["git", "status", "--short"]})
                server.record_verification({"repo_path": str(root), "name": "inspection", "status": "pass",
                                            "evidence": "Inspected repository status", "evidence_kind": "command",
                                            "command_id": command["command_id"]})
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
        self.assertEqual(payload["result"]["serverInfo"]["version"], "1.1.0")
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

    def test_empty_plans_and_legacy_empty_tasks_cannot_complete(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache, patch.object(server, "CACHE_ROOT", Path(cache)):
            root = Path(td)
            self.make_repo(root)
            base = {"repo_path": td, "goal": "Verify", "acceptance": ["Observed behavior"], "required_checks": ["behavior"]}
            for field in ("acceptance", "required_checks"):
                for invalid in ([], ["  "]):
                    with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                        server.task_begin({**base, field: invalid})
            server._save_state(root, {"task": {"status": "active", "required_checks": [], "acceptance": ["behavior"]}})
            self.assertFalse(server.task_finish({"repo_path": td})["finished"])

    def test_unrelated_success_cannot_validate_old_artifact(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache, patch.object(server, "CACHE_ROOT", Path(cache)):
            root = Path(td)
            self.make_repo(root)
            artifact = root / "old.apk"
            artifact.write_bytes(b"old APK")
            os.utime(artifact, (1000, 1000))
            server.task_begin({"repo_path": td, "goal": "Build", "acceptance": ["Current APK"], "required_checks": ["build"]})
            result = server.build_artifact({"repo_path": td, "argv": [sys.executable, "-m", "compileall", "scanner.py"], "artifact_glob": "old.apk"})
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["check"]["status"], "fail")
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(result["rejected_artifacts"][0]["path"], "old.apk")
            self.assertFalse(server.task_finish({"repo_path": td})["finished"])

    def test_verified_cached_build_and_artifact_tampering(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache, patch.object(server, "CACHE_ROOT", Path(cache)):
            root = Path(td)
            self.make_repo(root)
            plan = {"repo_path": td, "goal": "Build", "acceptance": ["Compiled output"], "required_checks": ["build"]}
            args = {"repo_path": td, "argv": [sys.executable, "-m", "compileall", "scanner.py"], "artifact_glob": "__pycache__/*.pyc"}
            server.task_begin(plan)
            built = server.build_artifact(args)
            self.assertEqual(built["check"]["status"], "pass")
            self.assertTrue(server.task_finish({"repo_path": td})["finished"])
            server.task_begin(plan)
            cached = server.build_artifact(args)
            self.assertEqual(cached["check"]["status"], "pass")
            self.assertEqual(cached["artifacts"][0]["provenance"], "verified_reuse")
            output = root / cached["artifacts"][0]["path"]
            output.write_bytes(b"tampered output")
            self.assertFalse(server.task_finish({"repo_path": td})["finished"])

    def test_build_evidence_expires_when_source_changes(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache, patch.object(server, "CACHE_ROOT", Path(cache)):
            root = Path(td)
            self.make_repo(root)
            server.task_begin({"repo_path": td, "goal": "Build", "acceptance": ["Current output"], "required_checks": ["build"]})
            server.build_artifact({"repo_path": td, "argv": [sys.executable, "-m", "compileall", "scanner.py"], "artifact_glob": "__pycache__/*.pyc"})
            (root / "scanner.py").write_text("def changed():\n    return 2\n", encoding="utf-8")
            result = server.task_finish({"repo_path": td})
            self.assertFalse(result["finished"])
            self.assertIn("build", result["stale_evidence"])

    def test_verification_needs_real_current_evidence(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache, patch.object(server, "CACHE_ROOT", Path(cache)):
            root = Path(td)
            self.make_repo(root)
            server.task_begin({"repo_path": td, "goal": "Verify", "acceptance": ["Tests pass"], "required_checks": ["tests"]})
            evidence = {"repo_path": td, "name": "tests", "status": "pass", "evidence": "All good"}
            with self.assertRaises(ValueError):
                server.record_verification(evidence)
            with self.assertRaises(ValueError):
                server.record_verification({**evidence, "evidence_kind": "command", "command_id": "invented"})
            failed = server.run_bounded_command({"repo_path": td, "argv": ["git", "rev-parse", "--verify", "missing-ref"]})
            with self.assertRaises(ValueError):
                server.record_verification({**evidence, "evidence_kind": "command", "command_id": failed["command_id"]})
            command = server.run_bounded_command({"repo_path": td, "argv": ["git", "status", "--short"]})
            server.record_verification({**evidence, "evidence_kind": "command", "command_id": command["command_id"]})
            (root / "scanner.py").write_text("changed = True\n", encoding="utf-8")
            self.assertFalse(server.task_finish({"repo_path": td})["finished"])
            with self.assertRaises(ValueError):
                server.record_verification({**evidence, "evidence_kind": "command", "command_id": command["command_id"]})

    def test_observation_evidence_integrity_and_optional_failure(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache, patch.object(server, "CACHE_ROOT", Path(cache)):
            root = Path(td)
            self.make_repo(root)
            server.task_begin({"repo_path": td, "goal": "Observe", "acceptance": ["UI checked"], "required_checks": ["ui"]})
            report = root / "observation.txt"
            report.write_text("Observed success then reset", encoding="utf-8")
            evidence = {"repo_path": td, "name": "ui", "status": "pass", "evidence": "Observed success then reset",
                        "evidence_kind": "observation", "evidence_paths": ["observation.txt"]}
            server.record_verification(evidence)
            report.write_text("changed evidence", encoding="utf-8")
            self.assertFalse(server.task_finish({"repo_path": td})["finished"])
            server.record_verification(evidence)
            server.record_verification({"repo_path": td, "name": "extra-check", "status": "fail", "evidence": "Observed additional failure"})
            self.assertFalse(server.task_finish({"repo_path": td})["finished"])


if __name__ == "__main__":
    unittest.main()
