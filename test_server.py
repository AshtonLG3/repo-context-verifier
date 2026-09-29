import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import server


class ServerTests(unittest.TestCase):
    def test_repository_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "scanner.py").write_text("def show_toast():\n    return 'success'\n", encoding="utf-8")
            (root / ".env").write_text("PRIVATE=secret\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "scanner.py", ".env"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"], check=True)
            (root / "scanner.py").write_text("def show_toast():\n    return 'done'\n", encoding="utf-8")
            self.assertEqual(server.overview({"repo_path": str(root)})["source_files_scanned"], 1)
            found = server.search({"repo_path": str(root), "term": "toast"})["matches"]
            self.assertEqual([row["path"] for row in found], ["scanner.py"])
            self.assertIn("scanner.py", server.changes({"repo_path": str(root)})["changed_files"])

    def test_mcp_tool_listing(self):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, {"repo_overview", "repo_search", "repo_changes"})

    def test_stdio_transport(self):
        request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}}).encode()
        frame = b"Content-Length: " + str(len(request)).encode() + b"\r\n\r\n" + request
        result = subprocess.run([sys.executable, str(HERE / "server.py")], input=frame,
                                capture_output=True, timeout=5, check=True)
        header, body = result.stdout.split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Length:", header)
        self.assertEqual(json.loads(body)["result"]["serverInfo"]["name"], "repo-context-verifier")


if __name__ == "__main__":
    unittest.main()
