"""
Custom MCP Manager
==================

Local HTML desktop UI with a Python bridge for installing the latest
CustomMCPs GitHub release and configuring supported MCP clients.

The application intentionally:
  - downloads assets only from pythonIsFast/custom-mcps releases,
  - verifies GitHub-provided SHA-256 digests when available,
  - replaces executables atomically,
  - preserves unrelated MCP client settings,
  - creates a timestamped backup before changing an existing config.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import webbrowser
from pathlib import Path
from typing import Any

import requests

try:
    import webview
except ImportError:  # Allows unit tests without the desktop dependency.
    webview = None


APP_NAME = "Custom MCP Manager"
REPOSITORY = "pythonIsFast/custom-mcps"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
USER_AGENT = "CustomMCPManager/1.0"

SERVERS = {
    "inventor": {
        "name": "Autodesk Inventor MCP",
        "description": "Create and inspect CAD models through Inventor COM automation.",
        "asset": "inventor-mcp-server.exe",
        "config_name": "custom_inventor",
    },
    "moodle": {
        "name": "Moodle MCP",
        "description": "Manage Moodle through a normal authenticated browser session.",
        "asset": "moodle-mcp-server.exe",
        "config_name": "custom_moodle",
    },
}


def resource_path(*parts: str) -> Path:
    """Resolve bundled PyInstaller assets and normal source-tree assets."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ManagerApi:
    """Python API exposed to the local HTML UI through pywebview."""

    def __init__(
        self,
        *,
        install_dir: Path | None = None,
        home_dir: Path | None = None,
        appdata_dir: Path | None = None,
        session: requests.Session | None = None,
    ):
        local_appdata = os.environ.get("LOCALAPPDATA")
        appdata = os.environ.get("APPDATA")

        self.install_dir = Path(
            install_dir
            or (
                Path(local_appdata) / "CustomMCPs"
                if local_appdata
                else Path.home() / ".custom-mcps"
            )
        )
        self.bin_dir = self.install_dir / "bin"
        self.metadata_path = self.install_dir / "install-state.json"
        self.home_dir = Path(home_dir or Path.home())
        self.appdata_dir = Path(
            appdata_dir
            or (Path(appdata) if appdata else self.home_dir / "AppData" / "Roaming")
        )
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        self.window = None
        self._release: dict[str, Any] | None = None
        self._operation_lock = threading.Lock()

    # --------------------------------------------------------------- UI bridge

    def attach_window(self, window) -> None:
        self.window = window

    def _emit(self, event: str, **data: Any) -> None:
        if self.window is None:
            return
        payload = json.dumps({"event": event, **data}, ensure_ascii=False)
        try:
            self.window.evaluate_js(f"window.managerEvent({payload})")
        except Exception:
            # The page may be closing while a worker finishes.
            pass

    def _log(self, message: str, level: str = "info") -> None:
        self._emit("log", message=message, level=level, timestamp=time.time())

    # ------------------------------------------------------------- State/read API

    def get_state(self) -> dict[str, Any]:
        metadata = self._load_metadata()
        servers = {}
        for server_id, spec in SERVERS.items():
            path = self.bin_dir / spec["asset"]
            installed = path.exists()
            servers[server_id] = {
                **spec,
                "id": server_id,
                "installed": installed,
                "path": str(path),
                "size": path.stat().st_size if installed else 0,
                "sha256": sha256_file(path) if installed else None,
                "release_tag": metadata.get(server_id, {}).get("release_tag"),
            }

        return {
            "app_name": APP_NAME,
            "repository": REPOSITORY,
            "release_page": RELEASE_PAGE,
            "install_dir": str(self.install_dir),
            "servers": servers,
            "clients": self._client_states(),
            "release": self._release,
        }

    def refresh_release(self) -> dict[str, Any]:
        try:
            response = self.session.get(RELEASE_API, timeout=30)
            response.raise_for_status()
            raw = response.json()
            assets = {}
            for asset in raw.get("assets", []):
                name = asset.get("name")
                if name:
                    assets[name] = {
                        "name": name,
                        "size": int(asset.get("size") or 0),
                        "digest": asset.get("digest"),
                        "download_url": asset.get("browser_download_url"),
                    }
            self._release = {
                "tag": raw.get("tag_name"),
                "name": raw.get("name") or raw.get("tag_name"),
                "url": raw.get("html_url") or RELEASE_PAGE,
                "published_at": raw.get("published_at"),
                "assets": assets,
            }
            self._emit("release", release=self._release)
            self._log(f"Latest release loaded: {self._release['tag']}")
            return {"ok": True, "release": self._release}
        except Exception as exc:
            message = f"Could not load GitHub release: {exc}"
            self._log(message, "error")
            return {"ok": False, "error": message}

    # --------------------------------------------------------------- Installation

    def install_server(self, server_id: str) -> dict[str, Any]:
        if server_id not in SERVERS:
            return {"ok": False, "error": f"Unknown server: {server_id}"}
        if not self._operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another installation is already running."}
        try:
            return self._install_server_locked(server_id)
        finally:
            self._operation_lock.release()

    def install_all(self) -> dict[str, Any]:
        if not self._operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another installation is already running."}
        try:
            results = {}
            for server_id in SERVERS:
                result = self._install_server_locked(server_id)
                results[server_id] = result
                if not result.get("ok"):
                    return {
                        "ok": False,
                        "error": result.get("error"),
                        "results": results,
                    }
            return {"ok": True, "results": results, "state": self.get_state()}
        finally:
            self._operation_lock.release()

    def _install_server_locked(self, server_id: str) -> dict[str, Any]:
        spec = SERVERS[server_id]
        if self._release is None:
            release_result = self.refresh_release()
            if not release_result.get("ok"):
                return release_result

        assert self._release is not None
        asset = self._release["assets"].get(spec["asset"])
        if not asset or not asset.get("download_url"):
            message = (
                f"{spec['asset']} is missing from release "
                f"{self._release.get('tag') or '(unknown)'}."
            )
            self._log(message, "error")
            return {"ok": False, "error": message}

        self.bin_dir.mkdir(parents=True, exist_ok=True)
        target = self.bin_dir / spec["asset"]
        expected_digest = self._digest_value(asset.get("digest"))

        if target.exists() and expected_digest:
            current_digest = sha256_file(target)
            if current_digest.lower() == expected_digest.lower():
                self._save_server_metadata(server_id, target, current_digest)
                self._emit(
                    "progress",
                    server=server_id,
                    percent=100,
                    status="Already up to date",
                )
                self._log(f"{spec['name']} is already up to date.")
                return {
                    "ok": True,
                    "status": "current",
                    "path": str(target),
                    "sha256": current_digest,
                }

        temp_path = target.with_suffix(target.suffix + ".download")
        try:
            self._emit(
                "progress",
                server=server_id,
                percent=0,
                status=f"Downloading {spec['asset']}",
            )
            self._log(f"Downloading {spec['asset']}...")
            digest = hashlib.sha256()
            downloaded = 0

            with self.session.get(
                asset["download_url"], stream=True, timeout=(30, 180)
            ) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or asset["size"] or 0)
                with temp_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        percent = int(downloaded * 100 / total) if total else 0
                        self._emit(
                            "progress",
                            server=server_id,
                            percent=min(percent, 99),
                            status=f"Downloading {spec['asset']}",
                            downloaded=downloaded,
                            total=total,
                        )

            actual_digest = digest.hexdigest()
            if expected_digest and actual_digest.lower() != expected_digest.lower():
                raise ValueError(
                    "SHA-256 verification failed. "
                    f"Expected {expected_digest}, got {actual_digest}."
                )

            try:
                os.replace(temp_path, target)
            except PermissionError as exc:
                raise PermissionError(
                    f"Could not replace {target.name}. Close MCP clients using it "
                    "and try again."
                ) from exc

            self._save_server_metadata(server_id, target, actual_digest)
            self._emit(
                "progress",
                server=server_id,
                percent=100,
                status="Installed",
            )
            self._log(f"Installed {spec['name']} to {target}.", "success")
            return {
                "ok": True,
                "status": "installed",
                "path": str(target),
                "sha256": actual_digest,
            }
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            message = f"Installation failed for {spec['name']}: {exc}"
            self._emit(
                "progress",
                server=server_id,
                percent=0,
                status="Failed",
            )
            self._log(message, "error")
            return {"ok": False, "error": message}

    @staticmethod
    def _digest_value(digest: Any) -> str | None:
        if not isinstance(digest, str) or not digest:
            return None
        return digest.split(":", 1)[-1] if ":" in digest else digest

    def _load_metadata(self) -> dict[str, Any]:
        if not self.metadata_path.exists():
            return {}
        try:
            data = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_server_metadata(
        self, server_id: str, target: Path, digest: str
    ) -> None:
        metadata = self._load_metadata()
        metadata[server_id] = {
            "path": str(target),
            "sha256": digest,
            "release_tag": (self._release or {}).get("tag"),
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._atomic_write_text(
            self.metadata_path,
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        )

    # ---------------------------------------------------------- Client configure

    def configure_clients(
        self, client_ids: list[str], server_ids: list[str]
    ) -> dict[str, Any]:
        if not isinstance(client_ids, list) or not client_ids:
            return {"ok": False, "error": "Select at least one MCP client."}
        if not isinstance(server_ids, list) or not server_ids:
            return {"ok": False, "error": "Select at least one MCP server."}

        unknown_clients = set(client_ids) - set(self._client_definitions())
        unknown_servers = set(server_ids) - set(SERVERS)
        if unknown_clients:
            return {
                "ok": False,
                "error": f"Unknown clients: {', '.join(sorted(unknown_clients))}",
            }
        if unknown_servers:
            return {
                "ok": False,
                "error": f"Unknown servers: {', '.join(sorted(unknown_servers))}",
            }

        for server_id in server_ids:
            target = self.bin_dir / SERVERS[server_id]["asset"]
            if not target.exists():
                return {
                    "ok": False,
                    "error": (
                        f"{SERVERS[server_id]['name']} is not installed yet. "
                        "Install it before configuring clients."
                    ),
                }

        configured = []
        backups = []
        try:
            definitions = self._client_definitions()
            for client_id in client_ids:
                definition = definitions[client_id]
                path = definition["path"]
                if definition["format"] == "codex_toml":
                    backup = self._configure_codex(path, server_ids)
                else:
                    backup = self._configure_json_client(
                        path, definition["root_key"], server_ids, client_id
                    )
                configured.append(
                    {
                        "id": client_id,
                        "name": definition["name"],
                        "path": str(path),
                    }
                )
                if backup:
                    backups.append(str(backup))
                self._log(f"Configured {definition['name']}.", "success")
        except Exception as exc:
            message = f"Client configuration failed: {exc}"
            self._log(message, "error")
            return {
                "ok": False,
                "error": message,
                "configured": configured,
                "backups": backups,
            }

        return {
            "ok": True,
            "configured": configured,
            "backups": backups,
            "clients": self._client_states(),
        }

    def _client_definitions(self) -> dict[str, dict[str, Any]]:
        return {
            "codex": {
                "name": "Codex",
                "description": "OpenAI Codex global MCP configuration",
                "path": self.home_dir / ".codex" / "config.toml",
                "format": "codex_toml",
            },
            "claude": {
                "name": "Claude Desktop",
                "description": "Anthropic Claude Desktop configuration",
                "path": self.appdata_dir / "Claude" / "claude_desktop_config.json",
                "format": "json",
                "root_key": "mcpServers",
            },
            "cursor": {
                "name": "Cursor",
                "description": "Cursor global MCP configuration",
                "path": self.home_dir / ".cursor" / "mcp.json",
                "format": "json",
                "root_key": "mcpServers",
            },
            "vscode": {
                "name": "Visual Studio Code",
                "description": "VS Code user-profile MCP configuration",
                "path": self.appdata_dir / "Code" / "User" / "mcp.json",
                "format": "json",
                "root_key": "servers",
            },
        }

    def _client_states(self) -> list[dict[str, Any]]:
        states = []
        for client_id, definition in self._client_definitions().items():
            path = definition["path"]
            states.append(
                {
                    "id": client_id,
                    "name": definition["name"],
                    "description": definition["description"],
                    "path": str(path),
                    "config_exists": path.exists(),
                    "app_detected": self._client_detected(client_id, path),
                    "configured_servers": self._configured_servers(
                        client_id, definition
                    ),
                }
            )
        return states

    def _client_detected(self, client_id: str, config_path: Path) -> bool:
        if config_path.exists() or config_path.parent.exists():
            return True
        if client_id == "codex":
            return shutil.which("codex") is not None
        if client_id == "vscode":
            return shutil.which("code") is not None
        if client_id == "cursor":
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs"
                / "cursor"
                / "Cursor.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Cursor" / "Cursor.exe",
            ]
            return any(path.exists() for path in candidates)
        if client_id == "claude":
            candidate = (
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "AnthropicClaude"
                / "Claude.exe"
            )
            return candidate.exists()
        return False

    def _configured_servers(
        self, client_id: str, definition: dict[str, Any]
    ) -> list[str]:
        path = definition["path"]
        if not path.exists():
            return []
        try:
            if definition["format"] == "codex_toml":
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                entries = data.get("mcp_servers", {})
            else:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                entries = data.get(definition["root_key"], {})
            found = []
            for server_id, spec in SERVERS.items():
                if isinstance(entries, dict) and spec["config_name"] in entries:
                    found.append(server_id)
            return found
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            return []

    def _configure_json_client(
        self,
        path: Path,
        root_key: str,
        server_ids: list[str],
        client_id: str,
    ) -> Path | None:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except ValueError as exc:
                raise ValueError(
                    f"{path} contains invalid JSON and was not changed: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise ValueError(f"{path} must contain a JSON object.")
        else:
            data = {}

        entries = data.setdefault(root_key, {})
        if not isinstance(entries, dict):
            raise ValueError(f"'{root_key}' in {path} must be a JSON object.")

        for server_id in server_ids:
            spec = SERVERS[server_id]
            item = {
                "command": str(self.bin_dir / spec["asset"]),
                "args": [],
            }
            if client_id == "vscode":
                item["type"] = "stdio"
            entries[spec["config_name"]] = item

        backup = self._backup_file(path)
        self._atomic_write_text(
            path, json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        )
        return backup

    def _configure_codex(
        self, path: Path, server_ids: list[str]
    ) -> Path | None:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(
                    f"{path} contains invalid TOML and was not changed: {exc}"
                ) from exc
        else:
            text = ""

        for server_id in server_ids:
            spec = SERVERS[server_id]
            section = f"mcp_servers.{spec['config_name']}"
            pattern = re.compile(
                rf"(?ms)^\[{re.escape(section)}\][^\n]*\n.*?(?=^\[|\Z)"
            )
            text = pattern.sub("", text).rstrip()
            command = json.dumps(str(self.bin_dir / spec["asset"]))
            block = f"[{section}]\ncommand = {command}\nargs = []"
            text = f"{text}\n\n{block}\n" if text else f"{block}\n"

        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(
                f"Generated Codex configuration is invalid; nothing was written: {exc}"
            ) from exc

        backup = self._backup_file(path)
        self._atomic_write_text(path, text)
        return backup

    @staticmethod
    def _backup_file(path: Path) -> Path | None:
        if not path.exists():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        backup = path.with_name(f"{path.name}.backup-{stamp}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        return backup

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)

    # --------------------------------------------------------------- OS actions

    def open_install_folder(self) -> dict[str, Any]:
        try:
            self.install_dir.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(self.install_dir)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.install_dir)])
            else:
                subprocess.Popen(["xdg-open", str(self.install_dir)])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_release_page(self) -> dict[str, Any]:
        try:
            webbrowser.open((self._release or {}).get("url") or RELEASE_PAGE)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def run_moodle_setup(self) -> dict[str, Any]:
        executable = self.bin_dir / SERVERS["moodle"]["asset"]
        if not executable.exists():
            return {"ok": False, "error": "Install Moodle MCP first."}
        try:
            subprocess.Popen(
                [str(executable), "--setup"],
                creationflags=(
                    subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
                ),
            )
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def main() -> None:
    if webview is None:
        raise SystemExit("Missing dependency: pip install pywebview")

    api = ManagerApi()
    ui_path = resource_path("installer_ui", "index.html")
    if not ui_path.exists():
        raise SystemExit(f"UI file not found: {ui_path}")

    window = webview.create_window(
        APP_NAME,
        url=str(ui_path),
        js_api=api,
        width=1180,
        height=760,
        min_size=(980, 650),
        background_color="#090d18",
    )
    api.attach_window(window)
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
