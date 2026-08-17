"""Small, defensive client for the Proxmox VE JSON API."""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlsplit

import requests

try:
    import websocket
except ImportError:  # pragma: no cover - exercised through the runtime error
    websocket = None  # type: ignore[assignment]


API_PREFIX = "/api2/json"
CONFIG_DIR = Path.home() / ".proxmox_mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SUPPORTED_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
)
MAX_CONSOLE_COMMAND_BYTES = 64 * 1024
MAX_CONSOLE_OUTPUT_BYTES = 1024 * 1024
_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1B\][^\x07]*(?:\x07|\x1B\\))|"
    r"(?:\x1B\[[0-?]*[ -/]*[@-~])|"
    r"(?:\x1B[@-_])"
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


@dataclass(frozen=True)
class LxcConsoleResult:
    """Result of a command executed through an LXC terminal proxy."""

    node: str
    vmid: int
    command: str
    output: str
    exit_code: int
    duration_seconds: float


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

    @staticmethod
    def _terminal_input_frame(data: str) -> bytes:
        """Encode terminal input using pve-xtermjs' byte-length framing."""
        payload = data.encode("utf-8")
        return b"0:" + str(len(payload)).encode("ascii") + b":" + payload

    @staticmethod
    def _terminal_bytes(message: Any) -> bytes:
        if isinstance(message, bytes):
            return message
        if isinstance(message, str):
            return message.encode("utf-8")
        raise ProxmoxError(
            f"Proxmox returned an unsupported WebSocket message type: "
            f"{type(message).__name__}."
        )

    @staticmethod
    def _clean_terminal_output(raw_output: bytes) -> str:
        """Make terminal output readable without dropping normal Unicode."""
        text = raw_output.decode("utf-8", errors="replace")
        # A Proxmox LXC PTY can produce CRCRLF (``\r\r\n``), not just the
        # usual CRLF. Collapse any CR run before LF to one logical newline.
        text = re.sub(r"\r+\n", "\n", text).replace("\r", "\n")
        text = _ANSI_ESCAPE_RE.sub("", text)
        text = "".join(
            character
            for character in text
            if character in {"\n", "\t"} or ord(character) >= 32
        )
        return text.strip()

    @staticmethod
    def _validate_lxc_console_arguments(
        node: str,
        vmid: int,
        command: str,
        timeout_seconds: float | None,
    ) -> tuple[str, int, str, float | None]:
        node = str(node).strip()
        if not _NODE_NAME_RE.fullmatch(node):
            raise ProxmoxError(
                "node must start with an alphanumeric character and contain "
                "only letters, numbers, dots, underscores, or hyphens."
            )
        if (
            isinstance(vmid, bool)
            or not isinstance(vmid, int)
            or not 1 <= vmid <= 999_999_999
        ):
            raise ProxmoxError("vmid must be an integer between 1 and 999999999.")
        if not isinstance(command, str) or not command.strip():
            raise ProxmoxError("command must be a non-empty string.")
        if "\x00" in command:
            raise ProxmoxError("command cannot contain NUL bytes.")
        command_size = len(command.encode("utf-8"))
        if command_size > MAX_CONSOLE_COMMAND_BYTES:
            raise ProxmoxError(
                f"command exceeds the {MAX_CONSOLE_COMMAND_BYTES}-byte limit."
            )
        if timeout_seconds is None:
            return node, vmid, command, None
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) < 0.5
        ):
            raise ProxmoxError(
                "timeout_seconds must be None or a finite number of at least 0.5."
            )
        return node, vmid, command, float(timeout_seconds)

    def _lxc_websocket_url(
        self,
        node: str,
        vmid: int,
        port: int,
        ticket: str,
    ) -> str:
        parts = urlsplit(self.settings.url)
        query = urlencode({"port": port, "vncticket": ticket})
        path = self.normalize_path(f"/nodes/{node}/lxc/{vmid}/vncwebsocket")
        return f"wss://{parts.netloc}{API_PREFIX}{path}?{query}"

    def _require_lxc_shell_console(self, node: str, vmid: int) -> None:
        """Reject login-based consoles before sending command input."""
        config = self.request("GET", f"/nodes/{node}/lxc/{vmid}/config")
        if not isinstance(config, Mapping):
            raise ProxmoxError("Proxmox returned an invalid LXC configuration.")
        cmode = config.get("cmode", "tty")
        if cmode != "shell":
            raise ProxmoxError(
                f"LXC container {vmid} uses console mode {cmode!r}. Console "
                "command execution requires cmode='shell' because tty and "
                "console modes may present an interactive login prompt. Set "
                f"it with 'pct set {vmid} --cmode shell' or in the Proxmox "
                "container options, then retry."
            )

    def _open_lxc_terminal_websocket(
        self,
        node: str,
        vmid: int,
        port: int,
        ticket: str,
        timeout_seconds: float | None,
    ) -> Any:
        if websocket is None:
            raise ProxmoxError(
                "LXC console execution requires the 'websocket-client' package."
            )
        ssl_options: dict[str, Any] = {"cert_reqs": ssl.CERT_REQUIRED}
        if not self.settings.verify_tls:
            ssl_options.update(
                {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
            )
        try:
            connect_timeout = self.settings.timeout_seconds
            if timeout_seconds is not None:
                connect_timeout = min(connect_timeout, timeout_seconds)
            return websocket.create_connection(
                self._lxc_websocket_url(node, vmid, port, ticket),
                timeout=connect_timeout,
                header=[
                    "Authorization: "
                    f"PVEAPIToken={self.settings.token_id}={self.settings.token_secret}"
                ],
                origin=f"https://{urlsplit(self.settings.url).netloc}",
                subprotocols=["binary"],
                sslopt=ssl_options,
            )
        except Exception as exc:
            message = str(exc)
            if "401" in message or "403" in message:
                raise ProxmoxError(
                    "Proxmox rejected the LXC console WebSocket upgrade. The "
                    "token needs VM.Console permission and the installed PVE "
                    "version must support API-token authentication for "
                    "vncwebsocket. Older PVE releases only accept user session "
                    f"tickets. Details: {message}"
                ) from exc
            raise ProxmoxError(
                f"Could not open the Proxmox LXC console WebSocket: {message}"
            ) from exc

    def lxc_console_exec(
        self,
        node: str,
        vmid: int,
        command: str,
        timeout_seconds: float | None = None,
    ) -> LxcConsoleResult:
        """Execute one shell command through an LXC xterm.js console.

        Proxmox does not expose a Guest Agent exec endpoint for LXC guests.
        This method therefore creates a short-lived termproxy, authenticates
        its WebSocket protocol, and runs the command through ``/bin/sh``.
        Completion is detected with a random marker assembled from two shell
        strings, preventing the terminal's command echo from matching it. By
        default it waits for that marker without a total execution deadline;
        pass ``timeout_seconds`` to impose one deliberately.
        """
        node, vmid, command, timeout_seconds = self._validate_lxc_console_arguments(
            node, vmid, command, timeout_seconds
        )
        self._require_lxc_shell_console(node, vmid)
        proxy = self.request("POST", f"/nodes/{node}/lxc/{vmid}/termproxy")
        if not isinstance(proxy, Mapping):
            raise ProxmoxError("Proxmox returned an invalid termproxy response.")

        try:
            port = int(proxy["port"])
            ticket = str(proxy["ticket"])
            user = str(proxy["user"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProxmoxError(
                "The termproxy response must contain port, ticket, and user."
            ) from exc
        if not 1 <= port <= 65_535 or not ticket or not user:
            raise ProxmoxError("Proxmox returned invalid termproxy connection data.")

        started_at = time.monotonic()
        deadline = (
            started_at + timeout_seconds if timeout_seconds is not None else None
        )
        connection = self._open_lxc_terminal_websocket(
            node, vmid, port, ticket, timeout_seconds
        )
        authenticated = False
        completed = False
        marker_token = uuid.uuid4().hex
        start_marker = f"__PVE_MCP_START_{marker_token}__"
        start_first = f"__PVE_MCP_START_{marker_token[:16]}"
        start_second = f"{marker_token[16:]}__"
        marker = f"__PVE_MCP_DONE_{marker_token}__"
        marker_first = f"__PVE_MCP_DONE_{marker_token[:16]}"
        marker_second = f"{marker_token[16:]}__"
        quoted_command = "'" + command.replace("'", "'\"'\"'") + "'"
        wrapped_command = (
            f"printf '%s%s\\n' '{start_first}' '{start_second}'; "
            f"/bin/sh -c {quoted_command}; "
            "__pve_mcp_rc=$?; "
            f"printf '\\n%s%s:%s\\n' '{marker_first}' '{marker_second}' "
            '"$__pve_mcp_rc"\n'
        )
        output = bytearray()
        marker_pattern = re.compile(
            re.escape(marker).encode("ascii") + rb":(-?\d+)(?:\r*\n|$)"
        )
        start_pattern = re.compile(
            re.escape(start_marker).encode("ascii") + rb"(?:\r*\n|$)"
        )

        try:
            connection.send_binary(f"{user}:{ticket}\n".encode("utf-8"))
            auth_reply = self._terminal_bytes(connection.recv())
            if not auth_reply.startswith(b"OK"):
                raise ProxmoxError(
                    "The LXC terminal rejected the termproxy authentication ticket."
                )
            authenticated = True

            # A wide PTY keeps echoed wrapper text readable in timeout errors.
            # It is not required for marker detection: real marker output is
            # shorter than a normal terminal line and parsing tolerates the
            # PTY's CRCRLF line endings.
            connection.send_binary(b"1:1000:24:")

            # Note: this LXC console is a genuine interactive PTY (the same
            # kind pve-xtermjs itself talks to). Its terminal driver echoes
            # whatever it receives back on the wire; that echo cannot be
            # reliably suppressed (an earlier attempt at sending 'stty -echo'
            # first only added another echoed line and did not fix anything).
            # Instead of fighting the echo, we send the command once and
            # scan for the LAST occurrence of each marker: the real,
            # executed 'printf' output is always the final occurrence in the
            # stream, after any echoed copies of the input itself.
            connection.send_binary(self._terminal_input_frame(wrapped_command))
            # This is a per-recv poll interval, not an execution deadline.
            connection.settimeout(0.25)

            while deadline is None or time.monotonic() < deadline:
                try:
                    message = connection.recv()
                except Exception as exc:
                    if websocket is not None and isinstance(
                        exc, websocket.WebSocketTimeoutException
                    ):
                        continue
                    raise ProxmoxError(
                        f"The LXC console WebSocket failed while reading output: {exc}"
                    ) from exc
                chunk = self._terminal_bytes(message)
                if not chunk:
                    raise ProxmoxError(
                        "The LXC console WebSocket closed before the command completed."
                    )
                output.extend(chunk)
                if len(output) > MAX_CONSOLE_OUTPUT_BYTES:
                    raise ProxmoxError(
                        f"LXC console output exceeded the "
                        f"{MAX_CONSOLE_OUTPUT_BYTES}-byte safety limit."
                    )
                # Take the LAST match of each marker, not the first: the PTY
                # may echo the input (and therefore the marker text embedded
                # in it) one or more times before the command is actually
                # executed by the shell. Only the final occurrence reflects
                # real execution.
                matches = list(marker_pattern.finditer(output))
                if matches:
                    match = matches[-1]
                    start_matches = list(
                        start_pattern.finditer(output, 0, match.start())
                    )
                    if not start_matches:
                        raise ProxmoxError(
                            "The LXC console returned a completion marker without "
                            "the expected command-start marker."
                        )
                    start_match = start_matches[-1]
                    clean_output = self._clean_terminal_output(
                        output[start_match.end() : match.start()]
                    )
                    completed = True
                    return LxcConsoleResult(
                        node=node,
                        vmid=vmid,
                        command=command,
                        output=clean_output,
                        exit_code=int(match.group(1)),
                        duration_seconds=round(time.monotonic() - started_at, 3),
                    )

            # Reaching this branch is only possible when the caller explicitly
            # requested a total timeout.
            assert timeout_seconds is not None
            partial_output = self._clean_terminal_output(bytes(output))
            detail = (
                f" Partial output: {partial_output[:500]}"
                if partial_output
                else ""
            )
            raise ProxmoxError(
                f"LXC console command timed out after {timeout_seconds} seconds."
                + detail
            )
        finally:
            if authenticated and not completed:
                try:
                    # Send Ctrl+C to interrupt a still-running command before
                    # closing, so a timed-out call does not leave a runaway
                    # process behind in the container.
                    connection.send_binary(self._terminal_input_frame("\x03"))
                except Exception:
                    pass
            try:
                connection.close()
            except Exception:
                pass
