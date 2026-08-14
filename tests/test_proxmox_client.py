import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
