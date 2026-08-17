#!/usr/bin/env python3
"""Expose the complete Proxmox VE JSON API to an MCP client.

Credentials come from PVE environment variables or the protected setup file.
Use a deliberately scoped Proxmox API token.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from dataclasses import asdict
from typing import Any

from proxmox_client import (
    SAFE_METHODS,
    ProxmoxClient,
    ProxmoxError,
    ProxmoxSettings,
)

# Import the concrete module rather than FastMCP's lazy public export. This
# keeps PyInstaller's dependency analysis reliable for standalone executables.
from fastmcp.server.server import FastMCP


mcp = FastMCP("proxmox-ve-mcp")


def _client() -> ProxmoxClient:
    """Create a client lazily so importing needs no credentials."""
    return ProxmoxClient(ProxmoxSettings.from_environment())


def _require_confirmation(method: str, confirm: bool) -> None:
    if method.upper() not in SAFE_METHODS and not confirm:
        raise ProxmoxError(
            "This is a state-changing request. Review the target and call again with confirm=true."
        )


@mcp.tool()
def pve_api_schema(path: str = "/") -> Any:
    """Return the live Proxmox API schema for a path, including valid child endpoints and parameters."""
    return _client().request("GET", path, query={"schema": 1})


@mcp.tool()
def pve_request(
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    confirm: bool = False,
) -> Any:
    """Call any Proxmox VE API endpoint.

    Use pve_api_schema first when parameters are unknown. All non-read-only
    methods require confirm=true, including POST, PUT, PATCH, and DELETE.
    """
    _require_confirmation(method, confirm)
    return _client().request(method, path, query=query, body=body)


@mcp.tool()
def pve_upload_file(
    path: str,
    file_path: str,
    fields: dict[str, Any] | None = None,
    file_field: str = "filename",
    confirm: bool = False,
) -> Any:
    """Upload a local file to a Proxmox multipart API endpoint.

    This reads the specified local file and sends it to Proxmox. It therefore
    always requires confirm=true.
    """
    _require_confirmation("POST", confirm)
    return _client().upload(path, file_path, fields=fields, file_field=file_field)


@mcp.tool()
def pve_lxc_console_exec(
    node: str,
    vmid: int,
    command: str,
    timeout_seconds: float = 15.0,
    confirm: bool = False,
) -> dict[str, Any]:
    """Execute a shell command inside an LXC container through its console.

    This grants effective interactive root-shell access and is substantially
    more powerful than a normal REST call. Every invocation requires an
    explicit confirm=true after the exact node, VMID, and command were
    reviewed. Unlike QEMU guest-agent exec, console execution is terminal
    based; the returned exit code is captured by a shell completion marker.
    """
    _require_confirmation("POST", confirm)
    result = _client().lxc_console_exec(
        node=node,
        vmid=vmid,
        command=command,
        timeout_seconds=timeout_seconds,
    )
    return asdict(result)


@mcp.tool()
def pve_cluster_overview() -> Any:
    """List all cluster resources, including nodes, VMs, containers, and storage."""
    return _client().request("GET", "/cluster/resources")


@mcp.tool()
def pve_list_guests(node: str | None = None, guest_type: str | None = None) -> Any:
    """List QEMU VMs and LXC containers, optionally limited to one node or type."""
    if guest_type is not None and guest_type not in {"qemu", "lxc"}:
        raise ProxmoxError("guest_type must be 'qemu', 'lxc', or omitted.")
    client = _client()
    if node and guest_type:
        return client.request("GET", f"/nodes/{node}/{guest_type}")
    if node:
        qemu = client.request("GET", f"/nodes/{node}/qemu")
        lxc = client.request("GET", f"/nodes/{node}/lxc")
        return qemu + lxc
    result = client.request("GET", "/cluster/resources", query={"type": "vm"})
    if guest_type:
        return [item for item in result if item.get("type") == guest_type]
    return result


@mcp.tool()
def pve_guest_action(
    node: str,
    guest_type: str,
    vmid: int,
    action: str,
    options: dict[str, Any] | None = None,
    confirm: bool = False,
) -> Any:
    """Run a lifecycle action such as start, shutdown, stop, reboot, suspend, or resume."""
    if guest_type not in {"qemu", "lxc"}:
        raise ProxmoxError("guest_type must be 'qemu' or 'lxc'.")
    if action not in {"start", "shutdown", "stop", "reboot", "suspend", "resume"}:
        raise ProxmoxError("Unsupported guest action. Use pve_request for other API actions.")
    _require_confirmation("POST", confirm)
    path = f"/nodes/{node}/{guest_type}/{vmid}/status/{action}"
    return _client().request("POST", path, body=options)


@mcp.tool()
def pve_task_status(node: str, upid: str) -> Any:
    """Return the current status of an asynchronous Proxmox task (UPID)."""
    return _client().task_status(node, upid)


@mcp.tool()
def pve_wait_for_task(
    node: str,
    upid: str,
    timeout_seconds: int = 600,
    poll_seconds: float = 2.0,
) -> Any:
    """Wait for an asynchronous task to finish and return its final status."""
    if not 1 <= timeout_seconds <= 3_600:
        raise ProxmoxError("timeout_seconds must be between 1 and 3600.")
    if not 0.2 <= poll_seconds <= 30:
        raise ProxmoxError("poll_seconds must be between 0.2 and 30.")
    client = _client()
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = client.task_status(node, upid)
        if not isinstance(status, dict):
            raise ProxmoxError("Proxmox returned an invalid task-status response.")
        if status.get("status") == "stopped":
            return status
        if time.monotonic() >= deadline:
            raise ProxmoxError(f"Timed out after {timeout_seconds} seconds waiting for task {upid}.")
        time.sleep(poll_seconds)


@mcp.tool()
def pve_diagnose() -> dict[str, Any]:
    """Check connectivity, the configured token identity, PVE version, and TLS mode."""
    client = _client()
    return {
        "connection": "ok",
        "verify_tls": client.settings.verify_tls,
        "token_id": client.settings.token_id,
        "version": client.request("GET", "/version"),
        "cluster_status": client.request("GET", "/cluster/status"),
    }


def _setup() -> int:
    """Validate credentials interactively and persist them for this user."""
    url = input("Proxmox VE URL (for example https://pve.example:8006): ").strip()
    token_id = input("API token ID (user@realm!token): ").strip()
    token_secret = getpass.getpass("API token secret: ")
    verify_tls = input("Verify TLS certificates? [Y/n]: ").strip().lower() not in {"n", "no", "0"}
    try:
        names = ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN_SECRET", "PVE_VERIFY_TLS")
        previous = {name: os.environ.get(name) for name in names}
        os.environ.update(
            {
                "PVE_URL": url,
                "PVE_TOKEN_ID": token_id,
                "PVE_TOKEN_SECRET": token_secret,
                "PVE_VERIFY_TLS": "1" if verify_tls else "0",
            }
        )
        settings = ProxmoxSettings.from_environment()
        version = ProxmoxClient(settings).request("GET", "/version")
    except (ProxmoxError, ValueError) as exc:
        print(f"Connection test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for name, old_value in previous.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value
    print("Connection test succeeded.")
    print(f"Detected version: {version.get('version', 'unknown')}")
    storage = settings.save()
    print(f"Saved configuration using: {storage}.")
    if not verify_tls:
        print("Warning: TLS certificate verification is disabled for this test.", file=sys.stderr)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Proxmox VE MCP server")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="validate and save credentials in a user-private file",
    )
    args = parser.parse_args()
    if args.setup:
        raise SystemExit(_setup())
    mcp.run()


if __name__ == "__main__":
    main()
