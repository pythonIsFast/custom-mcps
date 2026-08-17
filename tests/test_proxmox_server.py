import sys
import types
import unittest
from unittest.mock import patch

from proxmox_client import LxcConsoleResult, ProxmoxError


class FakeFastMCP:
    def __init__(self, _name):
        pass

    def tool(self):
        return lambda function: function

    def run(self):
        pass


fake_fastmcp = types.ModuleType("fastmcp")
fake_fastmcp.FastMCP = FakeFastMCP
fake_fastmcp_server = types.ModuleType("fastmcp.server")
fake_fastmcp_server_server = types.ModuleType("fastmcp.server.server")
fake_fastmcp_server_server.FastMCP = FakeFastMCP
sys.modules["fastmcp"] = fake_fastmcp
sys.modules["fastmcp.server"] = fake_fastmcp_server
sys.modules["fastmcp.server.server"] = fake_fastmcp_server_server

import proxmox_mcp_server as server


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"data": "ok"}

    def lxc_console_exec(self, **kwargs):
        self.calls.append(("lxc_console_exec", kwargs))
        return LxcConsoleResult(
            node=kwargs["node"],
            vmid=kwargs["vmid"],
            command=kwargs["command"],
            output="root",
            exit_code=0,
            duration_seconds=0.25,
        )


class ProxmoxServerTests(unittest.TestCase):
    def test_state_changing_generic_request_requires_confirmation(self):
        with self.assertRaisesRegex(ProxmoxError, "confirm=true"):
            server.pve_request("DELETE", "/nodes/pve1/qemu/100")

    def test_confirmed_request_is_forwarded_unchanged(self):
        client = FakeClient()
        with patch.object(server, "_client", return_value=client):
            result = server.pve_request(
                "POST",
                "/nodes/pve1/qemu/100/status/start",
                body={"skiplock": False},
                confirm=True,
            )

        self.assertEqual(result, {"data": "ok"})
        self.assertEqual(
            client.calls,
            [
                (
                    "POST",
                    "/nodes/pve1/qemu/100/status/start",
                    {"query": None, "body": {"skiplock": False}},
                )
            ],
        )

    def test_schema_request_uses_the_live_schema_query_parameter(self):
        client = FakeClient()
        with patch.object(server, "_client", return_value=client):
            server.pve_api_schema("/nodes/pve1/qemu")

        self.assertEqual(
            client.calls,
            [("GET", "/nodes/pve1/qemu", {"query": {"schema": 1}})],
        )

    def test_lxc_console_exec_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(ProxmoxError, "confirm=true"):
            server.pve_lxc_console_exec("pve1", 101, "id")

    def test_confirmed_lxc_console_exec_is_forwarded(self):
        client = FakeClient()
        with patch.object(server, "_client", return_value=client):
            result = server.pve_lxc_console_exec(
                "pve1", 101, "id", timeout_seconds=20.0, confirm=True
            )

        self.assertEqual(result["output"], "root")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(
            client.calls,
            [
                (
                    "lxc_console_exec",
                    {
                        "node": "pve1",
                        "vmid": 101,
                        "command": "id",
                        "timeout_seconds": 20.0,
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
