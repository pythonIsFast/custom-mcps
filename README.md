# 🧩 Custom MCP Servers

[![Build and release MCP executables](https://github.com/pythonIsFast/custom-mcps/actions/workflows/build-inventor-exe.yml/badge.svg)](https://github.com/pythonIsFast/custom-mcps/actions/workflows/build-inventor-exe.yml)

Two local Model Context Protocol servers for controlling **Autodesk Inventor** and managing **Moodle without Web Service tokens**. Build CAD models through Inventor's COM API or work with Moodle courses through an authenticated browser-style session.

> [!IMPORTANT]
> This repository is experimental. Test write and delete operations on disposable Inventor documents and Moodle courses before using them with important data.

## ✨ What's included?

| Server | What it does | Platform |
| --- | --- | --- |
| [`inventor_mcp_server.py`](./inventor_mcp_server.py) | Controls Autodesk Inventor through its COM API | Windows |
| [`moodle_mcp_server.py`](./moodle_mcp_server.py) | Manages Moodle through a normal login session, internal AJAX calls, and HTML forms | Windows, Linux, WSL |
| [`installer_manager.py`](./installer_manager.py) | Installs release builds and configures supported MCP clients through a local HTML UI | Windows |

## 🚀 Custom MCP Manager

The easiest way to get started is the **Custom MCP Manager**, a local desktop
application built with an HTML interface and a Python bridge.

### Manager features

- Downloads the latest executables directly from GitHub Releases
- Streams downloads with live progress
- Verifies GitHub-provided SHA-256 asset digests
- Installs files atomically in `%LOCALAPPDATA%\CustomMCPs\bin`
- Detects Codex, Claude Desktop, Cursor, and Visual Studio Code
- Configures selected clients and servers with one click
- Preserves unrelated client settings
- Creates timestamped backups before changing existing configurations
- Opens Moodle's secure terminal login setup
- Keeps an in-app activity log for troubleshooting

Download `custom-mcp-manager.exe` from the latest release and run it. No Python
installation is required for the prebuilt manager.

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

- Create parts, boxes, cylinders, revolved profiles, sweeps, lofts, slots, and 3D paths
- Add cuts, holes, counterbores, countersinks, chamfers, shells, drafts, and threads
- Mirror bodies and features, and create rectangular or circular patterns
- Read and modify model parameters
- Inspect bodies, faces, edges, features, bounding boxes, mass properties, and iProperties
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

> [!NOTE]
> Inventor COM automation is not thread-safe. The server intentionally performs one operation per tool call on a single thread.

## 🎓 Moodle MCP — no Web Service token required

The Moodle server signs in through Moodle's regular login page and keeps an authenticated session using cookies and Moodle's `sesskey`. It does **not** require administrators to enable Moodle Web Services or create a `wstoken`.

### Features

- Log in through a local GUI or terminal setup
- List courses and inspect sections and activities
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
Create a hidden five-section course called "Python Basics".
```

```text
Add a page named "Welcome" to section 1 with a short introduction.
```

> [!WARNING]
> If an OS keyring is unavailable, the current implementation falls back to storing the Moodle password in a local configuration file. Install `keyring` and check the storage location reported during setup.

> [!CAUTION]
> Generic AJAX and form tools are powerful and can change or delete Moodle data. Use a dedicated Moodle account with the minimum required permissions and test against a non-production course first.

## 🔌 MCP client configuration

Add one or both servers to your MCP client's configuration. Replace the example paths with absolute paths on your machine.

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

Whenever `inventor_mcp_server.py` or `moodle_mcp_server.py` changes on `main`,
or when the installer manager changes, GitHub Actions builds all three Windows
x64 executables and creates a new GitHub
Release:

- `inventor-mcp-server.exe`
- `moodle-mcp-server.exe`
- `custom-mcp-manager.exe`

Download them from the repository's **Releases** page. Each release uses a
unique `build-<run number>-<commit>` tag. The same files are also available as a
workflow artifact for 30 days.

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
- Add CI checks, releases, and prebuilt artifacts

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
