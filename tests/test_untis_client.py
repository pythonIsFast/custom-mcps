import base64
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

from untis_client import UntisClient, UntisError, UntisSettings


class FakeResponse:
    def __init__(self, data=None, status=200, url="https://school.webuntis.com/WebUntis/"):
        self._data = data
        self.status_code = status
        self.url = url
        self.ok = 200 <= status < 400
        self.headers = {"Content-Type": "application/json"}
        self.content = b"x" if data is not None else b""
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, claims=None):
        claims = claims or {
            "exp": 4_000_000_000,
            "tenantId": 42,
            "roles": ["STUDENT"],
            "permissions": ["TIMETABLE_API_READ"],
            "userId": 7,
            "personId": 8,
        }
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        self.token = f"header.{payload}.signature"
        self.headers = {}
        self.verify = True
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/api/token/new"):
            return FakeResponse(self.token, url=url)
        return FakeResponse({"page": "login"}, url=url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if "/jsonrpc_web/" in url:
            return FakeResponse({"result": {"ok": True}}, url=url)
        return FakeResponse({"state": "SUCCESS"}, url=url)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse({"ok": True}, url=url)


class UntisClientTests(unittest.TestCase):
    def setUp(self):
        self.settings = UntisSettings(
            "https://school.webuntis.com/WebUntis/", "My School", "user", "secret"
        )
        self.session = FakeSession()
        self.client = UntisClient(self.settings, self.session)

    def test_login_uses_browser_form_and_exposes_no_secret(self):
        identity = self.client.login()

        login_call = self.session.calls[1]
        self.assertEqual(login_call[0], "POST")
        self.assertTrue(login_call[1].endswith("/WebUntis/j_spring_security_check"))
        self.assertEqual(login_call[2]["data"]["school"], "My School")
        self.assertEqual(login_call[2]["data"]["j_password"], "secret")
        self.assertNotIn("password", identity)
        self.assertNotIn("token", identity)
        self.assertEqual(identity["roles"], ["STUDENT"])
        self.assertTrue(identity["tenant_id_detected"])

    def test_rest_request_adds_bearer_tenant_and_schoolyear_headers(self):
        self.client.login()
        result = self.client.request(
            "GET", "/api/rest/view/v1/schoolyears", school_year_id=99
        )

        call = self.session.calls[-1]
        headers = call[2]["headers"]
        self.assertEqual(result, {"ok": True})
        self.assertEqual(headers["Authorization"], f"Bearer {self.session.token}")
        self.assertEqual(headers["Tenant-Id"], "42")
        self.assertEqual(headers["X-Webuntis-Api-School-Year-Id"], "99")

    def test_api_path_rejects_external_and_traversal_paths(self):
        with self.assertRaises(UntisError):
            self.client._api_url("https://evil.example/api/data")
        with self.assertRaises(UntisError):
            self.client._api_url("/api/../secret")
        with self.assertRaises(UntisError):
            self.client._api_url("/not-an-api/path")

    def test_timetable_uses_preselected_resource(self):
        self.client.token = self.session.token
        self.client.token_expires_at = 4_000_000_000
        self.client.tenant_id = "42"
        calls = []

        def request(method, path, query=None, body=None, school_year_id=None):
            calls.append((method, path, query))
            if path.endswith("/filter"):
                return {"preSelected": {"id": 123}}
            return {"days": []}

        self.client.request = request
        result = self.client.timetable("2026-09-14", "2026-09-18")

        self.assertEqual(result, {"days": []})
        self.assertEqual(calls[-1][2]["resources"], "123")

    def test_invalid_date_is_rejected_before_request(self):
        with self.assertRaises(UntisError):
            self.client.timetable("14.09.2026", "2026-09-18")

    def test_jsonrpc_restricts_service_and_method_names(self):
        with self.assertRaises(UntisError):
            self.client.jsonrpc("../../evil", "read", [])
        with self.assertRaises(UntisError):
            self.client.jsonrpc("calendarService", "bad-method", [])


class FakeFastMCP:
    def __init__(self, _name):
        pass

    def tool(self):
        return lambda function: function

    def run(self):
        pass


fake_fastmcp = types.ModuleType("fastmcp")
fake_server_package = types.ModuleType("fastmcp.server")
fake_server_module = types.ModuleType("fastmcp.server.server")
fake_server_module.FastMCP = FakeFastMCP
sys.modules["fastmcp"] = fake_fastmcp
sys.modules["fastmcp.server"] = fake_server_package
sys.modules["fastmcp.server.server"] = fake_server_module

import untis_mcp_server as server


class UntisServerTests(unittest.TestCase):
    def test_generic_write_requires_confirmation(self):
        with self.assertRaisesRegex(UntisError, "confirm=true"):
            server.untis_rest("DELETE", "/api/rest/view/v1/messages/1")

    def test_jsonrpc_write_name_requires_confirmation(self):
        with self.assertRaisesRegex(UntisError, "confirm=true"):
            server.untis_jsonrpc("calendarService", "saveEntry", "[]")

    def test_dedicated_homework_read_can_use_post_without_confirmation(self):
        client = Mock()
        client.request.return_value = {"items": []}
        with patch.object(server, "_client", return_value=client):
            result = server.untis_homework('{"start":"2026-09-14"}', 12)

        self.assertEqual(result, {"items": []})
        client.request.assert_called_once_with(
            "POST", "/api/rest/view/v1/classreg/homework/list",
            body={"start": "2026-09-14"}, school_year_id=12,
        )


if __name__ == "__main__":
    unittest.main()
