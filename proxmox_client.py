"""Small, defensive client for the Proxmox VE JSON API."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import requests


API_PREFIX = "/api2/json"
CONFIG_DIR = Path.home() / ".proxmox_mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SUPPORTED_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
)


class ProxmoxError(RuntimeError):
    """Raised when Proxmox rejects a request or returns an invalid response."""


@dataclass(frozen=True)
class ProxmoxSettings:
    """Connection settings loaded from the process environment."""

    url: str
    token_id: str
    token_secret: str
    verify_tls: bool = True
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls) -> "ProxmoxSettings":
        names = ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN_SECRET")
        provided = {name: os.environ.get(name) for name in names}
        if any(provided.values()) and not all(provided.values()):
            missing = [name for name, value in provided.items() if not value]
            raise ProxmoxError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        if all(provided.values()):
            values = {
                "url": provided["PVE_URL"],
                "token_id": provided["PVE_TOKEN_ID"],
                "token_secret": provided["PVE_TOKEN_SECRET"],
                "verify_tls": os.environ.get("PVE_VERIFY_TLS", "1"),
            }
        else:
            values = cls._load_saved_values()

        raw_url = str(values["url"]).rstrip("/")
        parts = urlsplit(raw_url)
        if parts.scheme != "https" or not parts.netloc:
            raise ProxmoxError("PVE_URL must be an absolute HTTPS URL.")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ProxmoxError(
                "PVE_URL must not contain credentials, query parameters, or fragments."
            )

        verify_tls = str(values["verify_tls"]).strip() not in {
            "0",
            "false",
            "False",
        }
        timeout = float(os.environ.get("PVE_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ProxmoxError("PVE_TIMEOUT_SECONDS must be greater than zero.")

        return cls(
            url=raw_url,
            token_id=str(values["token_id"]),
            token_secret=str(values["token_secret"]),
            verify_tls=verify_tls,
            timeout_seconds=timeout,
        )

    @staticmethod
    def _load_saved_values() -> dict[str, str | bool]:
        if not CONFIG_FILE.exists():
            raise ProxmoxError(
                "Set PVE_URL, PVE_TOKEN_ID, and PVE_TOKEN_SECRET, or run the server with --setup."
            )
        try:
            values = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProxmoxError(f"Could not read {CONFIG_FILE}: {exc}") from exc
        if not isinstance(values, dict):
            raise ProxmoxError(f"{CONFIG_FILE} must contain a JSON object.")

        token_id = values.get("token_id")
        token_secret = values.get("token_secret")
        if not all((values.get("url"), token_id, token_secret)):
            raise ProxmoxError(
                "Saved Proxmox configuration is incomplete. Run --setup again "
                "to migrate credentials from the old keyring-based format."
            )
        return {
            "url": str(values["url"]),
            "token_id": str(token_id),
            "token_secret": str(token_secret),
            "verify_tls": bool(values.get("verify_tls", True)),
        }

    def save(self) -> str:
        """Persist credentials in a user-private file independent of desktop services."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        values: dict[str, str | bool] = {
            "url": self.url,
            "token_id": self.token_id,
            "token_secret": self.token_secret,
            "verify_tls": self.verify_tls,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "secret_storage": "protected file",
        }

        CONFIG_FILE.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        return f"protected file ({CONFIG_FILE})"


class ProxmoxClient:
    """Authenticated client that keeps all requests inside one PVE API root."""

    def __init__(
        self,
        settings: ProxmoxSettings,
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": (
                    f"PVEAPIToken={settings.token_id}={settings.token_secret}"
                ),
            }
        )

    @staticmethod
    def normalize_path(path: str) -> str:
        """Accept only an API-relative path and prevent URL/path escape."""
        if not isinstance(path, str) or not path.strip():
            raise ProxmoxError("path must be a non-empty API-relative path.")
        raw = path.strip()
        if "?" in raw or "#" in raw:
            raise ProxmoxError("Pass query parameters separately; path cannot contain '?' or '#'.")
        if "://" in raw or raw.startswith("//"):
            raise ProxmoxError("path must not contain a URL.")
        segments = [segment for segment in raw.split("/") if segment]
        if any(segment in {".", ".."} for segment in segments):
            raise ProxmoxError("path cannot contain '.' or '..' segments.")
        return "/" + "/".join(quote(segment, safe="@!$,:=+-._~") for segment in segments)

    def api_url(self, path: str) -> str:
        return f"{self.settings.url}{API_PREFIX}{self.normalize_path(path)}"

    @staticmethod
    def _normalise_parameters(
        values: Mapping[str, Any] | None,
    ) -> list[tuple[str, str]] | None:
        if values is None:
            return None
        if not isinstance(values, Mapping):
            raise ProxmoxError("query and body must be JSON objects.")
        parameters: list[tuple[str, str]] = []
        for key, value in values.items():
            if not isinstance(key, str) or not key:
                raise ProxmoxError("Parameter names must be non-empty strings.")
            sequence = value if isinstance(value, list) else [value]
            for item in sequence:
                if item is None:
                    continue
                if isinstance(item, bool):
                    parameters.append((key, "1" if item else "0"))
                elif isinstance(item, (str, int, float)):
                    parameters.append((key, str(item)))
                else:
                    parameters.append((key, json.dumps(item, separators=(",", ":"))))
        return parameters

    @staticmethod
    def _response_payload(response: requests.Response) -> Any:
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProxmoxError("Proxmox returned invalid JSON.") from exc
            return payload.get("data", payload) if isinstance(payload, dict) else payload
        return response.text

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                errors = payload.get("errors")
                if isinstance(errors, dict):
                    return "; ".join(f"{key}: {value}" for key, value in errors.items())
                if payload.get("message"):
                    return str(payload["message"])
        except ValueError:
            pass
        return response.text.strip()[:1_000] or response.reason

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        method = method.upper().strip()
        if method not in SUPPORTED_METHODS:
            raise ProxmoxError(f"Unsupported HTTP method: {method}")
        try:
            response = self.session.request(
                method,
                self.api_url(path),
                params=self._normalise_parameters(query),
                data=self._normalise_parameters(body),
                timeout=self.settings.timeout_seconds,
                verify=self.settings.verify_tls,
            )
        except requests.RequestException as exc:
            raise ProxmoxError(f"Could not reach Proxmox VE: {exc}") from exc
        if not response.ok:
            raise ProxmoxError(
                f"Proxmox VE returned HTTP {response.status_code}: {self._error_message(response)}"
            )
        return self._response_payload(response)

    def upload(
        self,
        path: str,
        file_path: str,
        *,
        fields: Mapping[str, Any] | None = None,
        file_field: str = "filename",
    ) -> Any:
        source = Path(file_path).expanduser()
        if not source.is_file():
            raise ProxmoxError("file_path must point to a readable regular file.")
        if not file_field or not isinstance(file_field, str):
            raise ProxmoxError("file_field must be a non-empty string.")
        try:
            with source.open("rb") as handle:
                response = self.session.post(
                    self.api_url(path),
                    data=self._normalise_parameters(fields),
                    files={file_field: (source.name, handle)},
                    timeout=self.settings.timeout_seconds,
                    verify=self.settings.verify_tls,
                )
        except (OSError, requests.RequestException) as exc:
            raise ProxmoxError(f"Could not upload file: {exc}") from exc
        if not response.ok:
            raise ProxmoxError(
                f"Proxmox VE returned HTTP {response.status_code}: {self._error_message(response)}"
            )
        return self._response_payload(response)

    def task_status(self, node: str, upid: str) -> Any:
        return self.request("GET", f"/nodes/{node}/tasks/{upid}/status")
