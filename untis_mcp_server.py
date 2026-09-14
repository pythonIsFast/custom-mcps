#!/usr/bin/env python3
"""MCP server for WebUntis using its authenticated browser REST session."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

import requests
from fastmcp.server.server import FastMCP

from untis_client import SAFE_METHODS, UntisClient, UntisError, UntisSettings


mcp = FastMCP("webuntis-mcp")
_client_instance: UntisClient | None = None


def _client() -> UntisClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = UntisClient(UntisSettings.from_environment())
    return _client_instance


def _reset_client(settings: UntisSettings | None = None) -> UntisClient:
    global _client_instance
    _client_instance = UntisClient(settings or UntisSettings.from_environment())
    return _client_instance


def _require_confirmation(method: str, confirm: bool) -> None:
    if method.upper() not in SAFE_METHODS and not confirm:
        raise UntisError(
            "This request can change WebUntis data. Review method, path and body, "
            "then call it again with confirm=true."
        )


def _json_object(value: str, argument: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise UntisError(f"{argument} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise UntisError(f"{argument} must be a JSON object.")
    return parsed


def _json_params(value: str) -> list[Any] | dict[str, Any]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise UntisError(f"params_json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, (list, dict)):
        raise UntisError("params_json must be a JSON array or object.")
    return parsed


@mcp.tool()
def untis_login() -> dict[str, Any]:
    """Log in with saved credentials or UNTIS_* environment variables."""
    return _reset_client().login()


@mcp.tool()
def untis_login_manual(
    url: str,
    school: str,
    username: str,
    password: str,
    verify_tls: bool = True,
    tenant_id: str | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """Log in to WebUntis and optionally save credentials for future sessions.

    Prefer ``python untis_mcp_server.py --setup`` because passing a password to
    an MCP tool can place it in conversation history. ``url`` is the school
    server origin, for example https://example.webuntis.com. ``school`` is the
    WebUntis school/tenant name used by the login form.
    """
    settings = UntisSettings(url, school, username, password, verify_tls, tenant_id)
    identity = _reset_client(settings).login()
    identity["credential_storage"] = settings.save() if save else "not saved"
    return identity


@mcp.tool()
def untis_status() -> dict[str, Any]:
    """Check login, detected identity, roles and API permissions without exposing tokens."""
    client = _client()
    if client.token is None:
        client.login()
    return {"status": "authenticated", **client.identity(), "verify_tls": client.settings.verify_tls}


@mcp.tool()
def untis_diagnose() -> dict[str, Any]:
    """Read bootstrap information needed to diagnose this WebUntis tenant."""
    client = _client()
    schoolyears = client.request("GET", "/api/rest/view/v1/schoolyears")
    app_data = client.request("GET", "/api/rest/view/v1/app/data")
    menus = client.request("GET", "/api/rest/view/v1/app/platform-application/menus")
    return {
        "identity": client.identity(),
        "verify_tls": client.settings.verify_tls,
        "schoolyears": schoolyears,
        "app_data": app_data,
        "menus": menus,
    }


@mcp.tool()
def untis_schoolyears() -> Any:
    """List school years visible to the signed-in account."""
    return _client().request("GET", "/api/rest/view/v1/schoolyears")


@mcp.tool()
def untis_timetable(
    start: str,
    end: str,
    resource_type: str = "STUDENT",
    resource_ids: list[int] | None = None,
    timetable_type: str | None = None,
    school_year_id: int | None = None,
) -> Any:
    """Return timetable entries for a date range.

    Dates use YYYY-MM-DD. If resource_ids is omitted, WebUntis's preselected
    resource (normally the current student's own timetable) is used.
    ``resource_type`` commonly is STUDENT, CLASS, TEACHER, ROOM, or RESOURCE;
    availability depends on account permissions.
    """
    return _client().timetable(
        start, end, resource_type, resource_ids, timetable_type, school_year_id
    )


@mcp.tool()
def untis_timetable_filter(
    start: str,
    end: str,
    resource_type: str = "STUDENT",
    timetable_type: str | None = None,
    school_year_id: int | None = None,
) -> Any:
    """List selectable timetable resources such as students, classes, teachers and rooms."""
    query = {"start": start, "end": end, "resourceType": resource_type}
    if timetable_type:
        query["timetableType"] = timetable_type
    return _client().request(
        "GET", "/api/rest/view/v1/timetable/filter", query=query,
        school_year_id=school_year_id,
    )


@mcp.tool()
def untis_messages(folder: str = "incoming") -> Any:
    """List incoming, sent, or draft message summaries."""
    paths = {
        "incoming": "/api/rest/view/v1/messages",
        "sent": "/api/rest/view/v1/messages/sent",
        "drafts": "/api/rest/view/v2/messages/drafts",
    }
    if folder not in paths:
        raise UntisError("folder must be incoming, sent, or drafts.")
    return _client().request("GET", paths[folder])


@mcp.tool()
def untis_message(message_id: int, folder: str = "incoming") -> Any:
    """Read one incoming or sent message, including metadata and attachment information."""
    if folder == "incoming":
        path = f"/api/rest/view/v1/messages/{int(message_id)}"
    elif folder == "sent":
        path = f"/api/rest/view/v1/messages/sent/{int(message_id)}"
    else:
        raise UntisError("folder must be incoming or sent.")
    return _client().request("GET", path)


@mcp.tool()
def untis_exams(
    start: str | None = None,
    end: str | None = None,
    perspective: str = "student",
    school_year_id: int | None = None,
) -> Any:
    """List exams from the student, guardian, or class perspective."""
    if perspective not in {"student", "guardian", "class"}:
        raise UntisError("perspective must be student, guardian, or class.")
    query = {}
    if start:
        query["start"] = start
    if end:
        query["end"] = end
    return _client().request(
        "GET", f"/api/rest/view/v1/exams/for-{perspective}", query=query,
        school_year_id=school_year_id,
    )


@mcp.tool()
def untis_homework(filter_json: str = "{}", school_year_id: int | None = None) -> Any:
    """List homework using WebUntis's JSON filter DTO.

    The exact available filter fields vary by tenant and role. Call
    ``untis_rest(GET, /api/rest/view/v1/classreg/homework/meta)`` first, then
    pass the matching JSON object here.
    """
    return _client().request(
        "POST", "/api/rest/view/v1/classreg/homework/list",
        body=_json_object(filter_json, "filter_json"), school_year_id=school_year_id,
    )


@mcp.tool()
def untis_absences(filter_json: str = "{}", school_year_id: int | None = None) -> Any:
    """List absences using the current v4 JSON filter DTO.

    The exact fields vary by tenant and role. Read
    ``/api/rest/view/v1/classreg/absences/meta`` or inspect the WebUntis form to
    construct the filter. This dedicated tool is read-only despite using POST.
    """
    return _client().request(
        "POST", "/api/rest/view/v4/classreg/absences",
        body=_json_object(filter_json, "filter_json"), school_year_id=school_year_id,
    )


@mcp.tool()
def untis_rest(
    method: str,
    path: str,
    query_json: str = "{}",
    body_json: str = "{}",
    school_year_id: int | None = None,
    confirm: bool = False,
) -> Any:
    """Call a same-origin WebUntis REST endpoint.

    Only ``/api/...`` and ``/WebUntis/api/...`` paths are accepted. POST, PUT,
    PATCH and DELETE require confirm=true. Use dedicated read tools where
    available; this is the forward-compatible escape hatch for tenant-specific
    functions discovered in the frontend.
    """
    _require_confirmation(method, confirm)
    query = _json_object(query_json, "query_json")
    body = None if method.upper() in SAFE_METHODS else _json_object(body_json, "body_json")
    return _client().request(method, path, query=query, body=body,
                             school_year_id=school_year_id)


@mcp.tool()
def untis_jsonrpc(
    service: str,
    method: str,
    params_json: str = "[]",
    confirm: bool = False,
) -> Any:
    """Call one legacy WebUntis JSON-RPC service method.

    Calls whose method name suggests a write operation require confirm=true.
    REST is preferred when the modern frontend exposes the same function.
    """
    if re_search_write_method(method) and not confirm:
        raise UntisError(
            "This JSON-RPC method appears state-changing. Review it and call again with confirm=true."
        )
    return _client().jsonrpc(service, method, _json_params(params_json))


def re_search_write_method(method: str) -> bool:
    lowered = method.casefold()
    return any(word in lowered for word in (
        "add", "create", "save", "update", "delete", "remove", "send",
        "write", "set", "cancel", "book", "register", "confirm", "publish",
    ))


def _setup() -> int:
    url = input("WebUntis server URL (for example https://school.webuntis.com): ").strip()
    school = input("WebUntis school name: ").strip()
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    tenant_id = input("Tenant ID (usually leave empty for auto-detection): ").strip() or None
    verify_tls = input("Verify TLS certificates? [Y/n]: ").strip().lower() not in {"n", "no", "0"}
    settings = UntisSettings(url, school, username, password, verify_tls, tenant_id)
    try:
        identity = UntisClient(settings).login()
    except (UntisError, ValueError, requests.RequestException) as exc:
        print(f"Connection test failed: {exc}", file=sys.stderr)
        return 1
    storage = settings.save()
    print("Connection test succeeded.")
    print(f"School: {identity['school']}; roles: {identity['roles']}")
    print(f"Saved configuration using: {storage}.")
    if "plaintext" in storage:
        print("Warning: install and configure keyring to avoid plaintext password storage.", file=sys.stderr)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="WebUntis MCP server")
    parser.add_argument("--setup", action="store_true", help="validate and save WebUntis credentials")
    args = parser.parse_args()
    if args.setup:
        raise SystemExit(_setup())
    mcp.run()


if __name__ == "__main__":
    main()
