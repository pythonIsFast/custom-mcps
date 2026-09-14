# 🧩 Custom MCP Servers

[![Build and release MCP executables](https://github.com/pythonIsFast/custom-mcps/actions/workflows/build-inventor-exe.yml/badge.svg)](https://github.com/pythonIsFast/custom-mcps/actions/workflows/build-inventor-exe.yml)

Four local Model Context Protocol servers for controlling **Autodesk Inventor**, managing **Moodle without Web Service tokens**, working with **WebUntis**, and operating **Proxmox VE** through its native API. Build CAD models through Inventor's COM API, work with Moodle and WebUntis through authenticated browser-style sessions, or manage a Proxmox cluster with a scoped API token.

> [!IMPORTANT]
> This repository is experimental. Test write and delete operations on disposable Inventor documents and Moodle courses before using them with important data.

## ✨ What's included?

| Server | What it does | Platform |
| --- | --- | --- |
| [`inventor_mcp_server.py`](./inventor_mcp_server.py) | Controls Autodesk Inventor through its COM API | Windows |
| [`moodle_mcp_server.py`](./moodle_mcp_server.py) | Manages Moodle through a normal login session, internal AJAX calls, and HTML forms | Windows, Linux, WSL |
| [`proxmox_mcp_server.py`](./proxmox_mcp_server.py) | Exposes the complete Proxmox VE JSON API through a secure generic MCP interface | Windows, Linux, WSL |
| [`untis_mcp_server.py`](./untis_mcp_server.py) | Reads and manages WebUntis through its browser login, REST API, and legacy JSON-RPC | Windows, Linux, WSL |
| [`installer_manager.py`](./installer_manager.py) | Installs release builds and configures supported MCP clients through a local HTML UI | Windows, Linux |

## 🚀 Custom MCP Manager

The easiest way to get started is the **Custom MCP Manager**, a local desktop
application built with an HTML interface and a Python bridge.

### Manager features

- Downloads the latest executables directly from GitHub Releases
- Streams downloads with live progress
- Verifies GitHub-provided SHA-256 asset digests
- Compares installed files with the newest available release at startup
- Installs, updates, and uninstalls executables atomically
- Supports a custom installation directory
- Detects Codex, Claude Desktop, Cursor, and Visual Studio Code
- Adds or removes individual MCP entries in selected clients
- Preserves unrelated client settings
- Creates and restores timestamped configuration backups
- Creates Desktop and Start Menu shortcuts
- Opens secure terminal setup for Moodle, WebUntis, and Proxmox VE
- Keeps an in-app activity log for troubleshooting

Download `custom-mcp-manager.exe` on Windows or
`custom-mcp-manager-linux-x64` on Linux from the latest release and run it. No
Python installation is required for either prebuilt manager.

To run it from source:

```powershell
python -m pip install requests pywebview
python installer_manager.py
```

> [!NOTE]
> Restart an MCP client after configuring it so the client discovers the newly
> installed servers.

## 🛠️ Autodesk Inventor MCP

The Inventor server connects an MCP-compatible AI assistant to a running Autodesk Inventor installation. All public tool dimensions use **millimetres**; the server converts them to Inventor's internal centimetres automatically.

### Features

- Create parts, boxes, cylinders, generic sketches, sketch extrusions, revolved profiles, circular or rectangular sweeps, mixed-section lofts, slots, and 3D paths
- Add cuts, holes, counterbores, countersinks, equal/two-distance/distance-angle chamfers, inside/outside/two-sided shells, drafts, and threads
- Mirror bodies and features, and create rectangular or circular patterns
- Read and modify model parameters
- Inspect bodies, faces, edges, features, bounding boxes, mass properties, and iProperties
- Address faces and edges with persistent `geometry_id` handles instead of relying on changing collection indices
- Create assemblies, place components, and list occurrences
- Create drawings and export STEP, STL, DXF, and DWG files
- Save model screenshots from predefined camera orientations

### Requirements

- Windows
- Autodesk Inventor installed
- Python 3
- `mcp[cli]`
- `pywin32`

### Installation

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "mcp[cli]" pywin32
```

Start Autodesk Inventor, then run the server:

```powershell
python inventor_mcp_server.py
```

You can also test it with the MCP Inspector:

```powershell
npx @modelcontextprotocol/inspector python inventor_mcp_server.py
```

### Example requests

```text
Create a 100 × 60 × 20 mm box and add a 10 mm through-hole in its centre.
```

```text
Show me the model's bounding box and mass properties, then export it as STEP.
```

Generic sketch example:

```text
Create a sketch named "Base" on XY with a 40 x 20 mm rectangle and a 6 mm
circle at (20, 10), then extrude the closed profile by 12 mm as a new body.
```

`create_sketch` accepts `line`, `circle`, `rectangle`, `polyline`, and
`polygon` geometry. `list_faces` and `list_face_edges` return persistent
`geometry_id` values. Use `face_id/edge_id` with `add_fillet` or
`add_chamfer`; numeric `F1:E1` references remain available for older
workflows.

Advanced feature examples:

```text
Sweep a 12 mm circle along 0,0,0;40,0,0;40,30,20 with the profile normal
to the path and a 30 degree twist.
```

```text
Create a loft with sections "0|circle|50;30|rectangle|40|25;60|circle|20".
```

```text
List the faces, then remove the selected top face and create a 2 mm shell
directed inside.
```

The `loft` tool also retains its legacy circular syntax such as
`XY:50;30:40;60:30`. Structured section syntax is
`plane|circle|diameter[|centre_x|centre_y]` or
`plane|rectangle|width|height[|centre_x|centre_y]`. The `shell` tool accepts
stable face IDs from `list_faces`. `add_chamfer` supports `distance`,
`two_distances`, and `distance_angle`; asymmetric chamfers can use a stable
`reference_face` ID.

> [!NOTE]
> Inventor COM automation is not thread-safe. The server intentionally performs one operation per tool call on a single thread.

## 🎓 Moodle MCP — no Web Service token required

The Moodle server signs in through Moodle's regular login page and keeps an authenticated session using cookies and Moodle's `sesskey`. It does **not** require administrators to enable Moodle Web Services or create a `wstoken`.

### Features

- Log in through a local GUI or terminal setup
- Diagnose Moodle version, active theme, permissions, course-list source, and compatibility warnings
- List and search courses with pagination for accounts with many enrolments
- Inspect course sections and activities
- Create courses
- Rename, move, show, hide, duplicate, and delete course content
- Create and update pages, URLs, labels, folders, forums, assignments, resources, and quizzes
- List question categories, import question files, browse the question bank, and manage quiz slots
- Create, inspect, export, upload, and edit interactive H5P activities and `.h5p` packages
- Support both Moodle's native `mod_h5pactivity` and the third-party `mod_hvp` module
- Upload files to Moodle's draft area
- Inspect all forms on a page and submit browser-like URL-encoded or multipart forms
- Retry automatically after an expired session

### Requirements

- Python 3
- `fastmcp`
- `requests`
- `beautifulsoup4`
- `keyring` is strongly recommended

### Installation

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "fastmcp>=3,<4" requests beautifulsoup4 keyring
```

Linux and WSL:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "fastmcp>=3,<4" requests beautifulsoup4 keyring
```

### Login setup

The safest interactive setup reads the password without displaying it in the terminal:

```powershell
python moodle_mcp_server.py --setup
```

Alternatively, use environment variables:

```text
MOODLE_URL=https://moodle.example.edu
MOODLE_USER=your-username
MOODLE_PASSWORD=your-password
MOODLE_VERIFY_TLS=1
```

The `moodle_login` MCP tool can open a local Tkinter login window. `moodle_login_manual` is available as a fallback, but its password argument may become part of the AI client's conversation history.

### Example requests

```text
List my Moodle courses and show the sections in course 42.
```

```text
Run moodle_diagnose and explain why my course list may be incomplete.
```

```text
Create a hidden five-section course called "Python Basics".
```

```text
Add a page named "Welcome" to section 1 with a short introduction.
```

### H5P workflow

The Moodle MCP works with Moodle's native `mod_h5pactivity` package field and
can also upload packages through the third-party `mod_hvp` activity module. It
can publish an existing `.h5p` package, inspect its `h5p.json`,
`content/content.json`, library semantics and file list, export it again, and
replace its content or settings.

Relevant tools:

- `moodle_h5p_inspect` — inspect a Base64 package or an existing activity
- `moodle_create_h5p_activity` — upload and publish an interactive H5P activity
- `moodle_update_h5p_activity` — replace its package/content or edit settings
- `moodle_h5p_export` — return the complete package as Base64
- `moodle_create_hvp_activity` — publish a package through third-party `mod_hvp`
- `moodle_update_hvp_package` — safely replace a `mod_hvp` package while preserving form data

For reliable AI-authored content, first inspect an existing activity of the
same H5P content type. Then use it as `template_cmid`: the MCP preserves its
libraries and assets while replacing `h5p.json` and/or
`content/content.json`. A package can also be built without a template, but
its required H5P libraries must already be installed in Moodle or included in
`files_base64_json`. Package paths and JSON are validated locally before the
upload; Moodle remains authoritative for library compatibility and content
validation.

Example requests:

```text
Inspect H5P activity 87, keep its question type and design, replace the
questions with five questions about networking, and publish the result in
section 2 of course 42.
```

```text
Update only the content of H5P activity 87 and keep its libraries, media,
display settings, and title unchanged.
```

> [!WARNING]
> If an OS keyring is unavailable, the current implementation falls back to storing the Moodle password in a local configuration file. Install `keyring` and check the storage location reported during setup.

> [!CAUTION]
> Generic AJAX and form tools are powerful and can change or delete Moodle data. Use a dedicated Moodle account with the minimum required permissions and test against a non-production course first.

## 🏫 WebUntis MCP

The WebUntis server signs in through the normal browser endpoint, exchanges the
cookie session for WebUntis's short-lived REST token, and keeps both secrets
inside the process. It discovers roles and permissions from the token and
supports both the modern REST API and constrained legacy JSON-RPC calls.

### Features

- Read school years, the current user's timetable and selectable resources
- Read incoming/sent messages, drafts, message details and unread status
- Read exams, homework and absences
- Diagnose tenant bootstrap data, menus, roles and API permissions
- Call same-origin REST endpoints through `untis_rest`
- Call validated legacy services through `untis_jsonrpc`
- Require `confirm=true` for generic state-changing requests
- Refresh expiring REST tokens automatically

### Installation and authentication

```bash
python -m pip install "fastmcp>=3,<4" requests keyring
python untis_mcp_server.py --setup
```

Alternatively configure:

```text
UNTIS_URL=https://school.webuntis.com
UNTIS_SCHOOL=internal-school-name
UNTIS_USER=your-username
UNTIS_PASSWORD=your-password
UNTIS_VERIFY_TLS=1
UNTIS_TENANT_ID=optional-override
```

SSO, mandatory password changes and second-factor challenges must currently be
completed in the browser. If no OS keyring is available, setup falls back to a
user-private configuration file and prints a plaintext-storage warning.

### Reconnaissance helper

`untis_inspector.js` is a local, browser-console helper for investigating a
specific school's WebUntis deployment. It
records request paths, methods, field names/types, response schemas and form
structure, but deliberately excludes cookies, header values, request values,
response values and storage values.

1. Open the WebUntis login page, open the browser DevTools **Console**, and
   paste the complete contents of `untis_inspector.js`.
2. Optionally run `__UNTIS_INSPECTOR__.clear()`, then sign in normally. The
   sanitized request metadata survives the login-page navigation in session
   storage. Paste the script again after the navigation and run
   `__UNTIS_INSPECTOR__.download()` to save the JSON result.
3. On the authenticated page, leave the inspector active while opening the
   timetable, substitutions, absences, messages and other relevant workflows.
   Only actions performed while the interceptor is active provide full method,
   request-field and response-schema information.
4. Review the exported JSON before sharing it. Do not perform destructive
   actions merely for inspection. Use `__UNTIS_INSPECTOR__.stop()` to remove
   the interceptors early.

The helper does not capture WebSocket or Service-Worker traffic; such traffic
must be inspected separately in DevTools. It is a reconnaissance tool only and
does not send data to this repository or any third party. Findings extracted
from the public frontend bundles are documented in
[`UNTIS_API_NOTES.md`](./UNTIS_API_NOTES.md).

> [!CAUTION]
> WebUntis can contain minors' personal, attendance, and assessment data. Use a
> least-privilege account, protect local credentials, and review every generic
> write request before setting `confirm=true`.

## 🖥️ Proxmox VE MCP

The Proxmox VE server exposes the live JSON API behind a small set of MCP
tools. `pve_request` can call every HTTP endpoint available to the configured
API token, so the MCP does not become stale when Proxmox adds an endpoint. Use
`pve_api_schema` to inspect live API paths directly on the connected PVE
instance. For directory paths, it lists the child endpoints available to the
configured token.

### Features

- Call every Proxmox VE JSON API endpoint with `pve_request`
- Inspect the live API tree and child endpoints
- Upload files through multipart API endpoints
- List cluster resources and guests with concise convenience tools
- Start, stop, reboot, suspend, and resume QEMU VMs and LXC containers
- Execute confirmed shell commands inside LXC containers through Proxmox's terminal WebSocket
- Read and wait for asynchronous Proxmox tasks (UPIDs)
- Diagnose connectivity, PVE version, and the configured token identity

### Requirements

- Python 3.10 or newer
- `fastmcp>=3,<4`
- `requests`
- `websocket-client`
- Network access to the Proxmox VE API (usually HTTPS port 8006)
- A Proxmox VE API token with deliberately scoped ACLs

### Installation and authentication

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "fastmcp>=3,<4" requests websocket-client
```

Create a dedicated PVE user and API token in the Proxmox UI, then grant it
only the ACLs required for the tasks it should perform. Export its values in
the environment used by your MCP client:

```bash
export PVE_URL="https://pve.example:8006"
export PVE_TOKEN_ID="automation@pve!mcp"
export PVE_TOKEN_SECRET="replace-with-the-token-secret"
export PVE_VERIFY_TLS=1
python proxmox_mcp_server.py
```

Run `python proxmox_mcp_server.py --setup` to validate a URL and token
interactively. It stores them in `~/.proxmox_mcp/config.json` with mode `0600`,
so Claude can start the MCP reliably without depending on a desktop keyring or
session service. Environment variables always override saved credentials.
`PVE_VERIFY_TLS` defaults to `1`; only set it to `0` temporarily when
connecting to a deliberately trusted host with a self-signed certificate.

### Safe operation

Read-only API requests are available immediately. Every request with a
state-changing HTTP method (`POST`, `PUT`, `PATCH`, or `DELETE`) requires
`confirm=true`; uploads and every LXC console command do too. Console execution
provides effective interactive root-shell access inside a container and is much
more powerful than a normal REST endpoint. This is an MCP-level guard, not a
replacement for Proxmox permissions. The API token's ACLs remain the
authoritative limit, and the token needs `VM.Console` for the target container.

```text
Use pve_api_schema for /nodes/pve1/qemu/100, then show the VM configuration.
```

```text
Start VM 100 on pve1 using pve_guest_action with confirm=true, wait for its task, then show its status.
```

```text
Use pve_request to create a snapshot for container 200. Inspect the API path first and ask me for confirmation before sending the request.
```

```text
Run "uname -a" inside LXC container 200 on pve1 with pve_lxc_console_exec.
Show me the exact command first and only continue with confirm=true after I approve it.
```

LXC console execution uses Proxmox's xterm.js terminal protocol rather than a
Guest Agent. It returns the captured terminal output and a shell-derived exit
code. By default it waits until the command completes; set `timeout_seconds`
only when a deliberate overall execution deadline is wanted.
The target container must use console mode `shell`; Proxmox defaults to the
login-based `tty` mode, which cannot safely execute an unattended command. Set
it with `pct set <VMID> --cmode shell` or under the container's Options in the
Proxmox UI. Reading this setting also requires permission to read the container
configuration (normally `VM.Audit`).
Older PVE releases may reject API tokens during the WebSocket upgrade even when
the REST `termproxy` call succeeds; update PVE if that compatibility error is
reported.

## 🔌 MCP client configuration

Add the servers you need to your MCP client's configuration. Replace the example paths with absolute paths on your machine.

```json
{
  "mcpServers": {
    "inventor": {
      "command": "C:/path/to/CustomMCPs/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/CustomMCPs/inventor_mcp_server.py"
      ]
    },
    "moodle": {
      "command": "C:/path/to/CustomMCPs/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/CustomMCPs/moodle_mcp_server.py"
      ]
    },
    "proxmox": {
      "command": "C:/path/to/CustomMCPs/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/CustomMCPs/proxmox_mcp_server.py"
      ],
      "env": {
        "PVE_URL": "https://pve.example:8006",
        "PVE_TOKEN_ID": "automation@pve!mcp",
        "PVE_TOKEN_SECRET": "replace-with-your-token-secret"
      }
    }
  }
}
```

For Linux or WSL, use the virtual environment's `bin/python` path instead:

```text
/absolute/path/to/CustomMCPs/.venv/bin/python
```

> [!TIP]
> A Windows MCP client cannot directly execute a Python interpreter inside WSL without an explicit WSL command. For the Inventor server, use native Windows Python because Autodesk Inventor and its COM API are Windows-only.

## 📦 Optional standalone Inventor executable

The Inventor server can be packaged with PyInstaller:

```powershell
python -m pip install pyinstaller
pyinstaller --onefile --name inventor-mcp-server --hidden-import win32timezone --hidden-import win32com.gen_py inventor_mcp_server.py
```

The executable will be created in the `dist` directory.

### Download prebuilt executables

Whenever a server source file changes on `main`, or when the installer manager
changes, GitHub Actions builds Windows x64 executables for all components plus
Linux x64 executables for Moodle and Proxmox VE, then
creates a new GitHub Release:

- `inventor-mcp-server.exe`
- `moodle-mcp-server.exe`
- `proxmox-mcp-server.exe`
- `custom-mcp-manager.exe`
- `custom-mcp-manager-linux-x64`
- `moodle-mcp-server-linux-x64`
- `proxmox-mcp-server-linux-x64`
- `SHA256SUMS.txt`

Download them from the repository's **Releases** page. Releases use semantic
`v<major>.<minor>.<build>` tags and include automatically generated, categorized
release notes. The same files are also available as a workflow artifact for 30
days.

Verify a downloaded executable against the published checksum file:

```powershell
Get-FileHash .\custom-mcp-manager.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

The workflow can be started manually from **Actions → Build and release MCP
executables → Run workflow**.

## 🧪 Project status and limitations

### Inventor

- Requires a local Autodesk Inventor installation and Windows COM support
- Complex CAD operations depend on the active document and Inventor's feature state
- Validate generated geometry before manufacturing or production use

### Moodle

- Tested against one Moodle installation using the Boost theme and topics course format
- No formal Moodle version compatibility matrix exists yet
- Uses internal AJAX endpoints and HTML forms that may change between Moodle versions or themes
- Direct username/password login does not support SSO or two-factor authentication
- Some activity creation, upload, duplicate, and delete paths remain experimental

## 🗺️ Roadmap

- Add automated tests and reproducible test fixtures
- Introduce dry-run and explicit confirmation modes for destructive Moodle tools
- Harden Moodle URL validation and credential storage
- Add a Moodle compatibility test matrix
- Split shared configuration into installable Python packages
- Add code signing for stronger Windows SmartScreen trust

## 🤝 Contributing

Issues, test reports, and pull requests are welcome. When reporting a problem, please include:

- operating system and Python version
- MCP client
- Autodesk Inventor or Moodle version
- the tool that was called
- the error message with credentials and private course data removed

Please never commit passwords, Moodle session cookies, Web Service tokens, student data, or proprietary CAD files.

## ⚖️ Disclaimer

This is an independent community project and is not affiliated with, endorsed by, or sponsored by Autodesk or Moodle. Autodesk Inventor and Moodle are trademarks of their respective owners.
