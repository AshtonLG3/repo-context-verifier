import sys
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # Local standard-library-only test runs may omit the optional SDK.
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None


@unittest.skipUnless(ClientSession is not None, "official mcp client SDK not installed")
class OfficialMcpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_client_initializes_lists_and_calls_tool(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(HERE / "server.py")],
            cwd=str(HERE),
            env={"SENTINEL_HOME": cache.name},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.server_info.name, "repo-context-verifier")

                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertIn("repo_overview", names)
                self.assertIn("context_bundle", names)
                self.assertIn("task_finish", names)

                result = await session.call_tool(
                    "repo_overview",
                    arguments={"repo_path": str(HERE)},
                )
                self.assertFalse(result.is_error)
                self.assertTrue(result.content)

                async def call(tool_name, **args):
                    return await session.call_tool(tool_name, arguments={"repo_path": str(HERE), **args})

                invalid = await call("task_begin", goal="Verify transport", acceptance=["MCP works"], required_checks=[])
                self.assertTrue(invalid.is_error)
                started = await call("task_begin", goal="Verify transport — UTF-8 ✓", acceptance=["Git status command succeeds"], required_checks=["status"])
                self.assertFalse(started.is_error)
                blocked = await call("task_finish")
                self.assertFalse(json.loads(blocked.content[0].text)["finished"])
                fabricated = await call("record_verification", name="status", status="pass", evidence="Looks good")
                self.assertTrue(fabricated.is_error)
                command = await call("run_bounded_command", argv=["git", "status", "--short"])
                command_id = json.loads(command.content[0].text)["command_id"]
                recorded = await call("record_verification", name="status", status="pass", evidence="Git status completed",
                                      evidence_kind="command", command_id=command_id)
                self.assertFalse(recorded.is_error)
                finished = await call("task_finish")
                self.assertTrue(json.loads(finished.content[0].text)["finished"])
                report = await call("task_report")
                self.assertIn("UTF-8 ✓", json.loads(report.content[0].text)["goal"])


if __name__ == "__main__":
    unittest.main()
