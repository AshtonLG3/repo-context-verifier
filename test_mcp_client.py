import sys
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
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(HERE / "server.py")],
            cwd=str(HERE),
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
                self.assertFalse(result.isError)
                self.assertTrue(result.content)


if __name__ == "__main__":
    unittest.main()
