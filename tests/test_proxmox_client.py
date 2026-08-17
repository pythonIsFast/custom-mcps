import json
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from proxmox_client import ProxmoxClient, ProxmoxError, ProxmoxSettings


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", content_type="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.reason = "Test response"
        self.headers = {"content-type": content_type}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.headers = {}
        self.response = response
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.timeouts = []
        self.closed = False

    def send_binary(self, message):
        self.sent.append(message)

    def recv(self):
        return self.messages.pop(0)

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True


class ProxmoxClientTests(unittest.TestCase):
    def setUp(self):
        self.settings = ProxmoxSettings(
            url="https://pve.example:8006",
            token_id="automation@pve!mcp",
            token_secret="secret-value",
        )

    def test_request_stays_within_api_root_and_unwraps_data(self):
        session = FakeSession(FakeResponse(payload={"data": [{"node": "pve1"}]}))
        client = ProxmoxClient(self.settings, session)

        result = client.request("GET", "/nodes", query={"full": True})

        self.assertEqual(result, [{"node": "pve1"}])
        _, request = session.calls[0]
        self.assertEqual(request["params"], [("full", "1")])
        self.assertEqual(request["verify"], True)
        self.assertEqual(session.headers["Authorization"], "PVEAPIToken=automation@pve!mcp=secret-value")

    def test_path_rejects_urls_query_strings_and_traversal(self):
        for invalid in ("https://other.example/api", "/nodes?full=1", "/nodes/../access"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProxmoxError):
                    ProxmoxClient.normalize_path(invalid)

    def test_multiple_values_are_sent_as_repeated_form_parameters(self):
        session = FakeSession(FakeResponse(payload={"data": "UPID:test"}))
        client = ProxmoxClient(self.settings, session)

        client.request("POST", "/nodes/pve1/qemu/100/status/start", body={"tags": ["blue", "green"], "force": False})

        _, request = session.calls[0]
        self.assertEqual(request["data"], [("tags", "blue"), ("tags", "green"), ("force", "0")])

    def test_api_errors_include_proxmox_error_details(self):
        session = FakeSession(FakeResponse(status_code=403, payload={"errors": {"permission": "denied"}}))
        client = ProxmoxClient(self.settings, session)

        with self.assertRaisesRegex(ProxmoxError, "HTTP 403: permission: denied"):
            client.request("GET", "/nodes")

    def test_non_json_responses_are_returned_as_text(self):
        session = FakeSession(FakeResponse(payload=None, text="plain output", content_type="text/plain"))
        client = ProxmoxClient(self.settings, session)

        self.assertEqual(client.request("GET", "/nodes/pve1/report"), "plain output")

    def test_terminal_frame_uses_utf8_byte_length(self):
        self.assertEqual(
            ProxmoxClient._terminal_input_frame("printf 'ä'\n"),
            "0:12:printf 'ä'\n".encode("utf-8"),
        )

    def test_lxc_console_exec_authenticates_frames_and_captures_exit_code(self):
        token = "a" * 32
        start = f"__PVE_MCP_START_{token}__"
        done = f"__PVE_MCP_DONE_{token}__"
        connection = FakeWebSocket(
            [
                b"OK",
                b"echoed wrapper copy\r\n",
                f"wrapped line\r\r\n{start}\r\r\n".encode(),
                b"hello\r\r\n",
                f"\r\r\n{done}:7\r\r\n".encode(),
            ]
        )
        client = ProxmoxClient(self.settings, FakeSession(FakeResponse()))

        with (
            patch.object(
                client,
                "request",
                side_effect=[
                    {"cmode": "shell"},
                    {
                        "port": "5900",
                        "ticket": "PVEVNC:ticket",
                        "user": "automation@pve!mcp",
                    },
                ],
            ) as request,
            patch.object(
                client, "_open_lxc_terminal_websocket", return_value=connection
            ),
            patch(
                "proxmox_client.uuid.uuid4",
                return_value=SimpleNamespace(hex=token),
            ),
        ):
            result = client.lxc_console_exec("pve1", 101, "echo hello")

        self.assertEqual(
            request.call_args_list,
            [
                call("GET", "/nodes/pve1/lxc/101/config"),
                call("POST", "/nodes/pve1/lxc/101/termproxy"),
            ],
        )
        self.assertEqual(connection.sent[0], b"automation@pve!mcp:PVEVNC:ticket\n")
        self.assertEqual(connection.sent[1], b"1:1000:24:")
        self.assertTrue(connection.sent[2].startswith(b"0:"))
        self.assertEqual(connection.timeouts, [0.25])
        self.assertEqual(result.output, "hello")
        self.assertEqual(result.exit_code, 7)
        self.assertTrue(connection.closed)

    def test_lxc_console_exec_rejects_unsafe_identifiers_before_request(self):
        client = ProxmoxClient(self.settings, FakeSession(FakeResponse()))

        with self.assertRaisesRegex(ProxmoxError, "node must"):
            client.lxc_console_exec("pve1/../../other", 101, "id")

    def test_lxc_console_exec_requires_complete_termproxy_data(self):
        client = ProxmoxClient(self.settings, FakeSession(FakeResponse()))
        with patch.object(
            client, "request", side_effect=[{"cmode": "shell"}, {"port": 5900}]
        ):
            with self.assertRaisesRegex(ProxmoxError, "port, ticket, and user"):
                client.lxc_console_exec("pve1", 101, "id")

    def test_lxc_console_exec_rejects_login_console_before_termproxy(self):
        client = ProxmoxClient(self.settings, FakeSession(FakeResponse()))
        with patch.object(client, "request", return_value={}) as request:
            with self.assertRaisesRegex(ProxmoxError, "cmode='shell'"):
                client.lxc_console_exec("pve1", 101, "id")

        request.assert_called_once_with("GET", "/nodes/pve1/lxc/101/config")

    def test_lxc_websocket_url_encodes_ticket(self):
        client = ProxmoxClient(self.settings, FakeSession(FakeResponse()))

        url = client._lxc_websocket_url("pve1", 101, 5900, "PVEVNC:a+b/c==")

        self.assertEqual(
            url,
            "wss://pve.example:8006/api2/json/nodes/pve1/lxc/101/"
            "vncwebsocket?port=5900&vncticket=PVEVNC%3Aa%2Bb%2Fc%3D%3D",
        )

    def test_lxc_console_wrapper_quotes_single_quotes_without_base64(self):
        token = "b" * 32
        start = f"__PVE_MCP_START_{token}__"
        done = f"__PVE_MCP_DONE_{token}__"
        connection = FakeWebSocket(
            [b"OK", f"{start}\r\r\nit's safe\r\r\n{done}:0\r\r\n".encode()]
        )
        client = ProxmoxClient(self.settings, FakeSession(FakeResponse()))

        with (
            patch.object(
                client,
                "request",
                side_effect=[
                    {"cmode": "shell"},
                    {"port": 5900, "ticket": "ticket", "user": "root@pam"},
                ],
            ),
            patch.object(
                client, "_open_lxc_terminal_websocket", return_value=connection
            ),
            patch(
                "proxmox_client.uuid.uuid4",
                return_value=SimpleNamespace(hex=token),
            ),
        ):
            result = client.lxc_console_exec("pve1", 101, "printf \"it's safe\"")

        wrapper_frame = connection.sent[2].decode("utf-8")
        self.assertIn("it'\"'\"'s safe", wrapper_frame)
        self.assertNotIn("base64", wrapper_frame)
        self.assertEqual(result.output, "it's safe")


if __name__ == "__main__":
    unittest.main()
