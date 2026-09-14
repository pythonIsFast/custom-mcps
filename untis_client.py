#!/usr/bin/env python3
"""Session-based WebUntis client used by the Untis MCP server."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


CONFIG_DIR = Path.home() / ".untis_mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYRING_SERVICE = "untis_mcp"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class UntisError(RuntimeError):
    """A validated configuration, authentication, or WebUntis API error."""

    def __init__(self, message: str, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


@dataclass(frozen=True)
class UntisSettings:
    url: str
    school: str
    username: str
    password: str
    verify_tls: bool = True
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("UNTIS_URL must be an absolute HTTP(S) URL.")
        if parsed.username or parsed.password:
            raise ValueError("UNTIS_URL must not contain credentials.")
        if parsed.query or parsed.fragment:
            raise ValueError("UNTIS_URL must not contain a query string or fragment.")
        if not self.school.strip() or not self.username.strip() or not self.password:
            raise ValueError("School, username, and password are required.")

    @property
    def origin(self) -> str:
        parsed = urlparse(self.url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def webuntis_url(self) -> str:
        return self.origin + "/WebUntis"

    @classmethod
    def from_environment(cls) -> "UntisSettings":
        env_url = os.environ.get("UNTIS_URL")
        env_school = os.environ.get("UNTIS_SCHOOL")
        env_user = os.environ.get("UNTIS_USER")
        env_password = os.environ.get("UNTIS_PASSWORD")
        if all((env_url, env_school, env_user, env_password)):
            return cls(
                env_url,
                env_school,
                env_user,
                env_password,
                _bool_env("UNTIS_VERIFY_TLS", True),
                os.environ.get("UNTIS_TENANT_ID") or None,
            )

        if not CONFIG_FILE.exists():
            raise UntisError(
                "No WebUntis credentials configured. Run untis_mcp_server.py --setup "
                "or set UNTIS_URL, UNTIS_SCHOOL, UNTIS_USER, and UNTIS_PASSWORD."
            )
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UntisError(f"Could not read {CONFIG_FILE}: {exc}") from exc

        password = None
        if data.get("password_in") == "keyring":
            try:
                import keyring
                password = keyring.get_password(KEYRING_SERVICE, data["username"])
            except Exception as exc:
                raise UntisError(f"Could not read the password from the OS keyring: {exc}") from exc
        else:
            password = data.get("password")
        if not password:
            raise UntisError("The configured WebUntis password is missing.")
        return cls(
            data.get("url", ""), data.get("school", ""), data.get("username", ""),
            password, bool(data.get("verify_tls", True)), data.get("tenant_id") or None,
        )

    def save(self) -> str:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "url": self.url,
            "school": self.school,
            "username": self.username,
            "verify_tls": self.verify_tls,
            "tenant_id": self.tenant_id,
        }
        storage = "user-private config file (plaintext password)"
        try:
            import keyring
            keyring.set_password(KEYRING_SERVICE, self.username, self.password)
            data["password_in"] = "keyring"
            storage = "OS keyring"
        except Exception:
            data["password_in"] = "config"
            data["password"] = self.password
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        return storage


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


class UntisClient:
    """Authenticate through WebUntis and call its same-origin REST/JSON-RPC APIs."""

    def __init__(self, settings: UntisSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.verify = settings.verify_tls
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Custom-Untis-MCP/1.0",
        })
        self.token: str | None = None
        self.token_expires_at = 0
        self.tenant_id = settings.tenant_id
        self.token_claims: dict[str, Any] = {}
        self._rpc_id = 0

    @staticmethod
    def _jwt_claims(token: str) -> dict[str, Any]:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _find_tenant_id(text: str) -> str | None:
        for pattern in (
            r'"tenantId"\s*:\s*"?(\d+)"?',
            r'"tenant"\s*:\s*\{[^{}]*"id"\s*:\s*"?(\d+)"?',
            r'\btenantId\s*[=:]\s*["\']?(\d+)',
        ):
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    def login(self) -> dict[str, Any]:
        login_page = self.session.get(self.settings.webuntis_url + "/", timeout=30)
        login_page.raise_for_status()
        self.tenant_id = self.tenant_id or self._find_tenant_id(login_page.text)

        response = self.session.post(
            self.settings.webuntis_url + "/j_spring_security_check",
            data={
                "school": self.settings.school,
                "j_username": self.settings.username,
                "j_password": self.settings.password,
                "token": "",
                "newPassword": "",
            },
            headers={
                "Referer": login_page.url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )
        response.raise_for_status()
        try:
            result = response.json()
        except requests.JSONDecodeError as exc:
            raise UntisError(
                "WebUntis returned no JSON login result. SSO or a changed login flow may be active."
            ) from exc
        if not isinstance(result, dict):
            raise UntisError("WebUntis returned an invalid login result.")
        state = result.get("state") or result.get("loginState")
        if state != "SUCCESS":
            messages = {
                "TOKEN_REQUIRED": "WebUntis requires a second factor. Complete login in the browser.",
                "MUST_SET_PASSWORD": "WebUntis requires a new password in the browser.",
                "MUST_UPDATE_LEGACY_PASSWORD": "WebUntis requires a password update in the browser.",
                "LOGIN_ERROR": "WebUntis rejected the username or password.",
                "NO_MANDANT": "The configured WebUntis school was not found.",
            }
            raise UntisError(messages.get(str(state), f"WebUntis login failed with state {state!r}."))
        self.refresh_token()
        return self.identity()

    def refresh_token(self) -> None:
        response = self.session.get(self.settings.webuntis_url + "/api/token/new", timeout=30)
        response.raise_for_status()
        try:
            parsed = response.json()
            token = parsed if isinstance(parsed, str) else parsed.get("token")
        except (requests.JSONDecodeError, AttributeError):
            token = response.text.strip().strip('"')
        if not token or token.count(".") < 2:
            raise UntisError("WebUntis did not return a valid REST bearer token.")
        self.token = token
        self.token_claims = self._jwt_claims(token)
        expiry = self.token_claims.get("exp")
        self.token_expires_at = int(expiry) if isinstance(expiry, (int, float)) else time.time() + 60
        if not self.tenant_id:
            for key in ("tenantId", "tenant", "mandantId", "schoolId"):
                value = self.token_claims.get(key)
                if isinstance(value, (str, int)):
                    self.tenant_id = str(value)
                    break

    def _ensure_token(self) -> None:
        if not self.token:
            self.login()
        elif time.time() >= self.token_expires_at - 10:
            self.refresh_token()

    def _api_url(self, path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/"):
            raise UntisError("API path must start with '/'.")
        if "?" in path or "#" in path or "\\" in path or ".." in path.split("/"):
            raise UntisError("API path must not contain queries, fragments, backslashes, or traversal.")
        if path.startswith("/WebUntis/api/"):
            return self.settings.origin + path
        if path.startswith("/api/"):
            return self.settings.webuntis_url + path
        raise UntisError("Only /api/... or /WebUntis/api/... paths are allowed.")

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: Any = None,
        school_year_id: int | None = None,
    ) -> Any:
        method = method.upper()
        if method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}:
            raise UntisError(f"Unsupported HTTP method: {method}")
        self._ensure_token()
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.tenant_id:
            headers["Tenant-Id"] = self.tenant_id
        if school_year_id is not None:
            headers["X-Webuntis-Api-School-Year-Id"] = str(school_year_id)
        response = self.session.request(
            method, self._api_url(path), params=query, json=body,
            headers=headers, timeout=60,
        )
        if response.status_code == 401:
            self.refresh_token()
            headers["Authorization"] = f"Bearer {self.token}"
            response = self.session.request(
                method, self._api_url(path), params=query, json=body,
                headers=headers, timeout=60,
            )
        if not response.ok:
            try:
                details = response.json()
            except requests.JSONDecodeError:
                details = response.text[:500]
            raise UntisError(
                f"WebUntis API returned HTTP {response.status_code} for {method} {path}.",
                response.status_code, details,
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except requests.JSONDecodeError:
            return response.text

    def jsonrpc(self, service: str, method: str, params: list[Any] | dict[str, Any] | None = None) -> Any:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", service):
            raise UntisError("Invalid JSON-RPC service name.")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", method):
            raise UntisError("Invalid JSON-RPC method name.")
        if self.token is None:
            self.login()
        self._rpc_id += 1
        response = self.session.post(
            f"{self.settings.webuntis_url}/jsonrpc_web/{service}",
            json={"id": self._rpc_id, "method": method, "params": params or [], "jsonrpc": "2.0"},
            headers={"X-Requested-With": "XMLHttpRequest"}, timeout=60,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("error"):
            raise UntisError("WebUntis JSON-RPC call failed.", details=result["error"])
        return result.get("result", result.get("data"))

    def identity(self) -> dict[str, Any]:
        claims = self.token_claims
        return {
            "username": self.settings.username,
            "school": self.settings.school,
            "server": self.settings.origin,
            "tenant_id_detected": bool(self.tenant_id),
            "roles": claims.get("roles", []),
            "permissions": claims.get("permissions", claims.get("apiPermissions", [])),
            "user_id": claims.get("userId"),
            "person_id": claims.get("personId"),
        }

    def timetable(
        self,
        start: str,
        end: str,
        resource_type: str = "STUDENT",
        resource_ids: list[int] | None = None,
        timetable_type: str | None = None,
        school_year_id: int | None = None,
    ) -> Any:
        _validate_date(start)
        _validate_date(end)
        ids = list(resource_ids or [])
        if not ids:
            filters = self.request(
                "GET", "/api/rest/view/v1/timetable/filter",
                query={"start": start, "end": end, "resourceType": resource_type,
                       **({"timetableType": timetable_type} if timetable_type else {})},
                school_year_id=school_year_id,
            )
            selected = filters.get("preSelected") if isinstance(filters, dict) else None
            if isinstance(selected, dict) and selected.get("id") is not None:
                ids = [int(selected["id"])]
        query: dict[str, Any] = {
            "start": start, "end": end, "resourceType": resource_type,
        }
        if ids:
            query["resources"] = ",".join(str(value) for value in ids)
        if timetable_type:
            query["timetableType"] = timetable_type
        return self.request(
            "GET", "/api/rest/view/v1/timetable/entries", query=query,
            school_year_id=school_year_id,
        )


def _validate_date(value: str) -> None:
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise UntisError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc
