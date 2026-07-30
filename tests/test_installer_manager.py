import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from installer_manager import ManagerApi


class InstallerManagerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home = root / "home"
        self.appdata = root / "appdata"
        self.install = root / "install"
        self.settings = root / "settings"
        self.api = ManagerApi(
            install_dir=self.install,
            settings_root=self.settings,
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

    def test_bridge_health_check_and_diagnostic_log(self):
        status = self.api.bridge_status()

        self.assertTrue(status["ok"])
        self.assertEqual(status["app_name"], "Custom MCP Manager")
        self.assertEqual(status["install_dir"], str(self.install))
        log_path = self.settings / "manager.log"
        self.assertTrue(log_path.exists())
        self.assertIn(
            "Manager API initialized.",
            log_path.read_text(encoding="utf-8"),
        )

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

    def test_remove_selected_entries_preserves_unrelated_json(self):
        path = self.appdata / "Claude" / "claude_desktop_config.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "mcpServers": {
                        "existing": {"command": "existing.exe"},
                        "custom_inventor": {"command": "old-inventor.exe"},
                        "custom_moodle": {"command": "old-moodle.exe"},
                    },
                }
            ),
            encoding="utf-8",
        )

        result = self.api.remove_client_entries(["claude"], ["inventor"])

        self.assertTrue(result["ok"])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertIn("existing", data["mcpServers"])
        self.assertNotIn("custom_inventor", data["mcpServers"])
        self.assertIn("custom_moodle", data["mcpServers"])
        self.assertEqual(len(result["backups"]), 1)

    def test_remove_codex_entry_preserves_other_sections(self):
        path = self.home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            'model = "example"\n\n'
            "[mcp_servers.custom_inventor]\n"
            'command = "inventor.exe"\nargs = []\n\n'
            "[mcp_servers.existing]\n"
            'command = "existing.exe"\nargs = []\n',
            encoding="utf-8",
        )

        result = self.api.remove_client_entries(["codex"], ["inventor"])

        self.assertTrue(result["ok"])
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("[mcp_servers.custom_inventor]", text)
        self.assertIn("[mcp_servers.existing]", text)
        self.assertIn('model = "example"', text)

    def test_restore_backup_keeps_safety_copy(self):
        path = self.home / ".cursor" / "mcp.json"
        path.parent.mkdir(parents=True)
        original = {"mcpServers": {"existing": {"command": "old.exe"}}}
        path.write_text(json.dumps(original), encoding="utf-8")
        backup = self.api._backup_file(path)
        path.write_text(json.dumps({"changed": True}), encoding="utf-8")

        result = self.api.restore_backup("cursor", str(backup))

        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
        self.assertTrue(Path(result["safety_backup"]).exists())

    def test_restore_rejects_file_outside_client_directory(self):
        outsider = Path(self.temp.name) / "mcp.json.backup-fake"
        outsider.write_text("{}", encoding="utf-8")

        result = self.api.restore_backup("cursor", str(outsider))

        self.assertFalse(result["ok"])
        self.assertIn("not valid", result["error"])

    def test_uninstall_removes_executable_and_metadata(self):
        self.api._save_metadata(
            {"inventor": {"release_tag": "v0.2.1", "path": "old"}}
        )

        result = self.api.uninstall_server("inventor")

        self.assertTrue(result["ok"])
        self.assertFalse(
            (self.api.bin_dir / "inventor-mcp-server.exe").exists()
        )
        self.assertNotIn("inventor", self.api._load_metadata())

    def test_state_compares_installed_and_available_hash(self):
        payload = b"test executable"
        digest = sha256(payload).hexdigest()
        self.api._release = {
            "tag": "v0.2.2",
            "assets": {
                "inventor-mcp-server.exe": {
                    "digest": f"sha256:{digest}"
                },
                "moodle-mcp-server.exe": {
                    "digest": "sha256:" + ("0" * 64)
                },
            },
        }

        state = self.api.get_state()

        self.assertTrue(state["servers"]["inventor"]["up_to_date"])
        self.assertFalse(state["servers"]["inventor"]["update_available"])
        self.assertTrue(state["servers"]["moodle"]["update_available"])

    def test_change_install_directory_moves_executables(self):
        new_install = Path(self.temp.name) / "custom-location"

        result = self.api.set_install_directory(str(new_install))

        self.assertTrue(result["ok"])
        self.assertEqual(self.api.install_dir, new_install.resolve())
        for name in ("inventor-mcp-server.exe", "moodle-mcp-server.exe"):
            self.assertTrue((new_install / "bin" / name).exists())
            self.assertFalse((self.install / "bin" / name).exists())
        settings = json.loads(self.api.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["install_dir"], str(new_install.resolve()))
        reloaded = ManagerApi(
            settings_root=self.settings,
            home_dir=self.home,
            appdata_dir=self.appdata,
        )
        self.assertEqual(reloaded.install_dir, new_install.resolve())


if __name__ == "__main__":
    unittest.main()
