# 🧩 Custom MCP Servers

[![Build and release MCP executables](https://github.com/pythonIsFast/custom-mcps/actions/workflows/build-inventor-exe.yml/badge.svg)](https://github.com/pythonIsFast/custom-mcps/actions/workflows/build-inventor-exe.yml)

Three local Model Context Protocol servers for controlling **Autodesk Inventor**, managing **Moodle without Web Service tokens**, and operating **Proxmox VE** through its native API. Build CAD models through Inventor's COM API, work with Moodle courses through an authenticated browser-style session, or manage a Proxmox cluster with a scoped API token.

> [!IMPORTANT]
> This repository is experimental. Test write and delete operations on disposable Inventor documents and Moodle courses before using them with important data.

## ✨ What's included?

| Server | What it does | Platform |
| --- | --- | --- |
| [`inventor_mcp_server.py`](./inventor_mcp_server.py) | Controls Autodesk Inventor through its COM API | Windows |
| [`moodle_mcp_server.py`](./moodle_mcp_server.py) | Manages Moodle through a normal login session, internal AJAX calls, and HTML forms | Windows, Linux, WSL |
| [`proxmox_mcp_server.py`](./proxmox_mcp_server.py) | Exposes the complete Proxmox VE JSON API through a secure generic MCP interface | Windows, Linux, WSL |
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
- Opens secure terminal setup for Moodle and Proxmox VE
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
- Create and update pages, URLs, labels, folders, forums, assignments, resources, and basic quizzes
- Upload files to Moodle's draft area
- Inspect internal Moodle forms and call AJAX-enabled functions
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

> [!WARNING]
> If an OS keyring is unavailable, the current implementation falls back to storing the Moodle password in a local configuration file. Install `keyring` and check the storage location reported during setup.

> [!CAUTION]
> Generic AJAX and form tools are powerful and can change or delete Moodle data. Use a dedicated Moodle account with the minimum required permissions and test against a non-production course first.

## 🖥️ Proxmox VE MCP

The Proxmox VE server exposes the live JSON API behind a small set of MCP
tools. `pve_request` can call every HTTP endpoint available to the configured
API token, so the MCP does not become stale when Proxmox adds an endpoint. Use
`pve_api_schema` to inspect paths and accepted parameters directly on the
connected PVE instance.

### Features

- Call every Proxmox VE JSON API endpoint with `pve_request`
- Inspect the live API tree and endpoint parameter schemas
- Upload files through multipart API endpoints
- List cluster resources and guests with concise convenience tools
- Start, stop, reboot, suspend, and resume QEMU VMs and LXC containers
- Read and wait for asynchronous Proxmox tasks (UPIDs)
- Diagnose connectivity, PVE version, and the configured token identity

### Requirements

- Python 3.10 or newer
- `fastmcp>=3,<4`
- `requests`
- Network access to the Proxmox VE API (usually HTTPS port 8006)
- A Proxmox VE API token with deliberately scoped ACLs

### Installation and authentication

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "fastmcp>=3,<4" requests
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
`confirm=true`; uploads do too. This is an MCP-level guard, not a replacement
for Proxmox permissions. The API token's ACLs remain the authoritative limit.

```text
Use pve_api_schema for /nodes/pve1/qemu/100, then show the VM configuration.
```

```text
Start VM 100 on pve1 using pve_guest_action with confirm=true, wait for its task, then show its status.
```

```text
Use pve_request to create a snapshot for container 200. Inspect the endpoint schema first and ask me for confirmation before sending the request.
```

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
