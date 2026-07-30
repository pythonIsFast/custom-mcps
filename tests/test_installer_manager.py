import json
import tempfile
import unittest
from pathlib import Path

from installer_manager import ManagerApi


class InstallerManagerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home = root / "home"
        self.appdata = root / "appdata"
        self.install = root / "install"
        self.api = ManagerApi(
            install_dir=self.install,
            home_dir=self.home,
            appdata_dir=self.appdata,
        )
        self.api.bin_dir.mkdir(parents=True)
        for spec in (
            "inventor-mcp-server.exe",
            "moodle-mcp-server.exe",
        ):
            (self.api.bin_dir / spec).write_bytes(b"test executable")

    def tearDown(self):
        self.temp.cleanup()

    def test_claude_merge_preserves_existing_servers(self):
        path = self.appdata / "Claude" / "claude_desktop_config.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "mcpServers": {
                        "existing": {"command": "existing.exe", "args": []}
                    },
                }
            ),
            encoding="utf-8",
        )

        result = self.api.configure_clients(["claude"], ["inventor", "moodle"])

        self.assertTrue(result["ok"])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertIn("existing", data["mcpServers"])
        self.assertIn("custom_inventor", data["mcpServers"])
        self.assertIn("custom_moodle", data["mcpServers"])
        self.assertEqual(len(result["backups"]), 1)
        self.assertTrue(Path(result["backups"][0]).exists())

    def test_vscode_uses_servers_root_and_stdio_type(self):
        result = self.api.configure_clients(["vscode"], ["inventor"])

        self.assertTrue(result["ok"])
        path = self.appdata / "Code" / "User" / "mcp.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        server = data["servers"]["custom_inventor"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["args"], [])

    def test_codex_merge_preserves_unrelated_toml(self):
        path = self.home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            'model = "example"\n\n[mcp_servers.existing]\n'
            'command = "existing.exe"\nargs = []\n',
            encoding="utf-8",
        )

        result = self.api.configure_clients(["codex"], ["moodle"])

        self.assertTrue(result["ok"])
        text = path.read_text(encoding="utf-8")
        self.assertIn('model = "example"', text)
        self.assertIn("[mcp_servers.existing]", text)
        self.assertIn("[mcp_servers.custom_moodle]", text)
        self.assertIn("moodle-mcp-server.exe", text)

    def test_invalid_json_is_never_overwritten(self):
        path = self.home / ".cursor" / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json", encoding="utf-8")

        result = self.api.configure_clients(["cursor"], ["inventor"])

        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{not valid json")


if __name__ == "__main__":
    unittest.main()
