#!/usr/bin/env python3
"""
Moodle MCP-Server
=================

Stellt Claude Werkzeuge bereit, um mit einer Moodle-Instanz zu arbeiten -
ueber Moodles internen AJAX-Endpoint und normale HTML-Formulare
(Session-basiert, keine Web-Service-Tokens noetig).

Verifiziert gegen die Instanz vom 30.07.2026 (Boost-Theme, Format
"topics"): Login, Kursliste, Kursstruktur via core_courseformat_get_state
und Kurs-Erstellung funktionieren.


INSTALLATION
------------
    pip install "fastmcp>=3,<4" requests beautifulsoup4
    pip install keyring          # optional, fuer sichere Passwortablage

⚠️ Es gibt ZWEI Bibliotheken mit der Klasse FastMCP: die eingefrorene 1.0
im Paket 'mcp' und das eigenstaendige, aktiv entwickelte 'fastmcp'.
Dieses Skript versucht zuerst 'fastmcp', dann den SDK-Fallback.


EINTRAG IN CLAUDE DESKTOP (claude_desktop_config.json)
------------------------------------------------------
    {
      "mcpServers": {
        "moodle": {
          "command": "/pfad/zu/venv/bin/python",
          "args": ["/pfad/zu/moodle_mcp.py"]
        }
      }
    }

Absolute Pfade nutzen. Unter WSL zeigt Claude Desktop (Windows) NICHT
automatisch auf den WSL-Python - siehe Hinweise unten.


LOGIN-KONZEPT
-------------
Zwei Wege, beide erzeugen dieselbe gespeicherte Konfiguration:

  A) Werkzeug 'moodle_login'  -> oeffnet ein Tkinter-Fenster.
     Das Fenster laeuft als EIGENER Subprozess, weil tk.mainloop()
     blockiert und Tkinter den Haupt-Thread braucht. Wuerde es im
     Server-Prozess laufen, stuende der MCP-Server still.
     ⚠️ Braucht ein Display. Unter WSL heisst das WSLg (Windows 11)
     oder einen X-Server. Ist keins da, schlaegt es sauber fehl und
     verweist auf Weg B.

  B) Werkzeug 'moodle_login_manual' -> Zugangsdaten direkt als
     Parameter. Funktioniert immer, aber das Passwort laeuft dabei
     durch den Chatverlauf.

Ablage der Zugangsdaten:
  - URL + Benutzername:  ~/.moodle_mcp/config.json  (Rechte 0600)
  - Passwort:            OS-Keyring, falls 'keyring' installiert ist,
                         sonst ⚠️ IM KLARTEXT in derselben Datei.
                         Das Werkzeug sagt dir, welcher Weg genutzt wurde.
  Alternativ Umgebungsvariablen (haben Vorrang, nichts wird gespeichert):
    MOODLE_URL, MOODLE_USER, MOODLE_PASSWORD, MOODLE_VERIFY_TLS=0|1


GRENZEN
-------
- Der AJAX-Endpoint stellt nur ajax-freigeschaltete Funktionen bereit.
  core_course_get_contents, core_course_get_categories und
  core_webservice_get_site_info antworten mit 'servicenotavailable'.
- Aktivitaeten/Inhalte ANLEGEN ist noch nicht enthalten (laeuft ueber
  /course/modedit.php, braucht pro Modultyp eigene Felder).
- Nicht offiziell dokumentiert: kann bei Moodle-Updates brechen.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------- MCP-Import

_MCP_FLAVOR = None
try:                                     # bevorzugt: eigenstaendiges fastmcp
    from fastmcp import FastMCP
    _MCP_FLAVOR = "fastmcp (standalone)"
except ImportError:
    try:                                 # Fallback: in-SDK FastMCP 1.0
        from mcp.server.fastmcp import FastMCP
        _MCP_FLAVOR = "mcp.server.fastmcp (in-SDK 1.0)"
    except ImportError:
        sys.exit('Fehlt: pip install "fastmcp>=3,<4"')


# ----------------------------------------------------------------- Logging

def log(*a):
    """MCP laeuft ueber stdio - Ausgaben MUESSEN nach stderr gehen."""
    print("[moodle-mcp]", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------- Credential-Ablage

CONFIG_DIR = Path.home() / ".moodle_mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYRING_SERVICE = "moodle_mcp"

try:
    import keyring
    _HAS_KEYRING = True
except ImportError:
    keyring = None
    _HAS_KEYRING = False


def save_credentials(url, username, password, verify_tls=True):
    """Speichert Zugangsdaten. Gibt zurueck, wo das Passwort landete."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {
        "url": url.rstrip("/"),
        "username": username,
        "verify_tls": bool(verify_tls),
        "gespeichert": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    wo = None
    if _HAS_KEYRING:
        try:
            keyring.set_password(KEYRING_SERVICE, username, password)
            # Sofort zurueckpruefen: manche Backends (z.B. Secret Service
            # unter WSL ohne persistenten Daemon) melden set_password als
            # erfolgreich, liefern die Daten aber in einem neuen Prozess
            # nicht mehr zurueck. Ohne diese Probe wuerde das erst beim
            # naechsten MCP-Aufruf auffallen.
            geprueft = keyring.get_password(KEYRING_SERVICE, username)
            if geprueft == password:
                cfg["passwort_in"] = "keyring"
                wo = "OS-Keyring"
            else:
                log("Keyring-Rueckprobe fehlgeschlagen (gespeichert, aber "
                    "nicht wieder lesbar) - weiche auf Datei aus.")
        except Exception as e:
            log(f"Keyring fehlgeschlagen ({e}) - weiche auf Datei aus.")

    if wo is None:
        cfg["password"] = password
        cfg["passwort_in"] = "datei_klartext"
        wo = f"KLARTEXT in {CONFIG_FILE} (Rechte 0600)"

    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    return wo


def load_credentials():
    """Env-Variablen haben Vorrang, dann die Config-Datei."""
    env_url = os.environ.get("MOODLE_URL")
    if env_url:
        return {
            "url": env_url.rstrip("/"),
            "username": os.environ.get("MOODLE_USER", ""),
            "password": os.environ.get("MOODLE_PASSWORD", ""),
            "verify_tls": os.environ.get("MOODLE_VERIFY_TLS", "1") != "0",
            "quelle": "Umgebungsvariablen",
        }

    if not CONFIG_FILE.exists():
        return None

    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Config unlesbar: {e}")
        return None

    pw = cfg.get("password", "")
    if not pw and _HAS_KEYRING and cfg.get("username"):
        try:
            pw = keyring.get_password(KEYRING_SERVICE, cfg["username"]) or ""
        except Exception as e:
            log(f"Keyring-Lesefehler: {e}")

    return {
        "url": cfg.get("url", ""),
        "username": cfg.get("username", ""),
        "password": pw,
        "verify_tls": cfg.get("verify_tls", True),
        "quelle": f"{CONFIG_FILE} (Passwort: {cfg.get('passwort_in', '?')})",
    }


def clear_credentials():
    entfernt = []
    cfg = None
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        CONFIG_FILE.unlink()
        entfernt.append(str(CONFIG_FILE))
    if cfg and _HAS_KEYRING and cfg.get("username"):
        try:
            keyring.delete_password(KEYRING_SERVICE, cfg["username"])
            entfernt.append("Keyring-Eintrag")
        except Exception:
            pass
    return entfernt


# ------------------------------------------------------ Tkinter-Loginfenster

def run_login_gui(vorbelegte_url=""):
    """
    Laeuft in einem EIGENEN Prozess (--login-gui) und schreibt das
    Ergebnis als JSON auf stdout. Nie im Serverprozess aufrufen.
    """
    import tkinter as tk
    from tkinter import ttk

    ergebnis = {}

    root = tk.Tk()
    root.title("Moodle-Anmeldung")
    root.resizable(False, False)

    rahmen = ttk.Frame(root, padding=16)
    rahmen.grid()

    ttk.Label(rahmen, text="Moodle-Anmeldung", font=("", 13, "bold")) \
        .grid(row=0, column=0, columnspan=2, pady=(0, 12))

    ttk.Label(rahmen, text="Moodle-URL:").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
    e_url = ttk.Entry(rahmen, width=36)
    e_url.grid(row=1, column=1, pady=4)
    e_url.insert(0, vorbelegte_url)

    ttk.Label(rahmen, text="Benutzername:").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
    e_user = ttk.Entry(rahmen, width=36)
    e_user.grid(row=2, column=1, pady=4)

    ttk.Label(rahmen, text="Passwort:").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=4)
    e_pw = ttk.Entry(rahmen, width=36, show="\u2022")
    e_pw.grid(row=3, column=1, pady=4)

    v_tls = tk.BooleanVar(value=True)
    ttk.Checkbutton(rahmen, text="TLS-Zertifikat pruefen (bei http/Selbstsigniert abwaehlen)",
                    variable=v_tls).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    hinweis = ttk.Label(rahmen, text="", foreground="#a00", wraplength=320)
    hinweis.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def speichern():
        url, user, pw = e_url.get().strip(), e_user.get().strip(), e_pw.get()
        if not url or not user or not pw:
            hinweis.config(text="Bitte alle drei Felder ausfuellen.")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        ergebnis.update({"url": url, "username": user, "password": pw,
                         "verify_tls": v_tls.get()})
        root.destroy()

    def abbrechen():
        ergebnis.update({"abgebrochen": True})
        root.destroy()

    knoepfe = ttk.Frame(rahmen)
    knoepfe.grid(row=6, column=0, columnspan=2, pady=(14, 0), sticky="e")
    ttk.Button(knoepfe, text="Abbrechen", command=abbrechen).grid(row=0, column=0, padx=4)
    ttk.Button(knoepfe, text="Anmelden", command=speichern).grid(row=0, column=1, padx=4)

    root.bind("<Return>", lambda _e: speichern())
    root.bind("<Escape>", lambda _e: abbrechen())
    e_url.focus_set()

    # Fenster nach vorne holen
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))

    root.mainloop()

    if not ergebnis:
        ergebnis = {"abgebrochen": True}
    print(json.dumps(ergebnis))


def start_login_gui_subprocess(vorbelegte_url="", timeout=300):
    """Startet das Fenster als Subprozess und liest das JSON-Ergebnis."""
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError(
            "Kein Display gefunden (DISPLAY/WAYLAND_DISPLAY leer). Unter WSL "
            "braucht das Fenster WSLg (Windows 11) oder einen X-Server. "
            "Nutze stattdessen das Werkzeug 'moodle_login_manual'."
        )

    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--login-gui", vorbelegte_url],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        tipp = ("Fehlt tkinter? Unter Debian/Ubuntu: sudo apt install python3-tk"
                if sys.platform.startswith("linux") else
                "Unter Windows ist tkinter normalerweise im Standard-"
                "Python-Installer enthalten (python.org). Falls eine "
                "Minimal-/Embeddable-Distribution genutzt wird, fehlt es dort.")
        raise RuntimeError(
            f"Login-Fenster beendet mit Code {proc.returncode}. "
            f"stderr: {(proc.stderr or '').strip()[:400]}  {tipp}"
        )
    try:
        return json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(
            f"Konnte Antwort des Fensters nicht lesen. stdout: "
            f"{(proc.stdout or '')[:300]}"
        )


# ------------------------------------------------------------ Moodle-Client

class MoodleSession:
    """Session-basierter Moodle-Zugriff. Logik verifiziert in v2 des CLI."""

    def __init__(self, base_url, verify_tls=True):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.verify = verify_tls
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) MoodleMCP/1.0",
        })
        if not verify_tls:
            requests.packages.urllib3.disable_warnings()
        self.sesskey = None
        self.userid = None

    # -- Login --

    def login(self, username, password):
        login_url = f"{self.base}/login/index.php"
        r = self.s.get(login_url, timeout=30)
        r.raise_for_status()
        tok = BeautifulSoup(r.text, "html.parser").find("input", {"name": "logintoken"})

        payload = {"username": username, "password": password, "anchor": ""}
        if tok:
            payload["logintoken"] = tok.get("value", "")

        r = self.s.post(login_url, data=payload, timeout=30,
                        headers={"Referer": login_url})
        r.raise_for_status()
        self._extract_sesskey(r.text)
        if not self.sesskey:
            self._extract_sesskey(self.s.get(f"{self.base}/my/", timeout=30).text)
        return self.sesskey is not None

    def _extract_sesskey(self, html):
        m = re.search(r'"sesskey"\s*:\s*"([^"]+)"', html)
        if m:
            self.sesskey = m.group(1)
        m = re.search(r'"userId"\s*:\s*(\d+)', html)
        if m:
            self.userid = int(m.group(1))

    def refresh_sesskey(self):
        self._extract_sesskey(self.s.get(f"{self.base}/my/", timeout=30).text)

    # -- AJAX --

    def ajax(self, methodname, args):
        if not methodname:
            raise ValueError("Leerer Methodenname.")
        if not self.sesskey:
            raise RuntimeError("Nicht angemeldet (kein sesskey).")

        r = self.s.post(
            f"{self.base}/lib/ajax/service.php",
            params={"sesskey": self.sesskey, "info": methodname},
            json=[{"index": 0, "methodname": methodname, "args": args}],
            timeout=60, headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        try:
            result = r.json()
        except ValueError:
            raise RuntimeError("Antwort war kein JSON: " + r.text[:300])

        entry = result[0] if isinstance(result, list) and result else result
        if isinstance(entry, dict) and entry.get("error"):
            exc = entry.get("exception") or {}
            code = exc.get("errorcode") or entry.get("errorcode") or "?"
            msg = exc.get("message") or entry.get("error") or ""
            if code == "servicenotavailable":
                msg += (" [Diese Funktion ist nicht ajax-freigeschaltet und "
                        "ueber diesen Endpoint nicht erreichbar.]")
            raise MoodleAjaxError(code, msg)
        return entry.get("data") if isinstance(entry, dict) else result

    # -- Kurse --

    def list_courses(self, search="", limit=25, offset=0):
        try:
            limit = int(limit)
            offset = int(offset)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit und offset muessen ganze Zahlen sein.") from exc
        if not 1 <= limit <= 100:
            raise ValueError("limit muss zwischen 1 und 100 liegen.")
        if offset < 0:
            raise ValueError("offset darf nicht negativ sein.")
        search = str(search or "").strip()

        try:
            args = {
                "offset": offset,
                "limit": limit,
                "classification": "all",
                "sort": "fullname",
            }
            if search:
                args["searchvalue"] = search
            data = self.ajax(
                "core_course_get_enrolled_courses_by_timeline_classification",
                args,
            )
            if isinstance(data, dict):
                raw_courses = data.get("courses", [])
                out = [
                    {
                        "id": c.get("id"),
                        "fullname": c.get("fullname"),
                        "shortname": c.get("shortname"),
                        "kategorie": c.get("coursecategory"),
                        "sichtbar": c.get("visible"),
                    }
                    for c in raw_courses
                    if isinstance(c, dict)
                ]
                next_offset = data.get("nextoffset")
                if isinstance(next_offset, int):
                    next_offset = next_offset if next_offset > offset else None
                else:
                    next_offset = (
                        offset + len(out) if len(out) >= limit else None
                    )
                result = {
                    "kurse": out,
                    "anzahl": len(out),
                    "offset": offset,
                    "limit": limit,
                    "naechster_offset": next_offset,
                    "weitere_vorhanden": next_offset is not None,
                    "suche": search or None,
                    "quelle": "AJAX (Timeline, paginiert)",
                }
                if next_offset is not None:
                    result["hinweis"] = (
                        "Weitere Kurse sind vorhanden. moodle_list_courses "
                        f"mit offset={next_offset} aufrufen."
                    )
                return result
        except Exception as e:
            log(f"Kursliste via AJAX fehlgeschlagen: {e}")

        courses = self._courses_html(search=search)
        page = courses[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(courses) else None
        return {
            "kurse": page,
            "anzahl": len(page),
            "offset": offset,
            "limit": limit,
            "naechster_offset": next_offset,
            "weitere_vorhanden": next_offset is not None,
            "suche": search or None,
            "quelle": "HTML-Fallback (Meine Kurse/Dashboard)",
            "hinweis": (
                "Der AJAX-Endpunkt war nicht verfuegbar. Der HTML-Fallback "
                "kann bei dynamisch geladenen Moodle-Dashboards unvollstaendig "
                "sein."
            ),
        }

    def _courses_html(self, search=""):
        found, seen = [], set()
        search_folded = search.casefold()
        pages = (
            (
                "/my/courses.php",
                (
                    '[data-region="course-content"]',
                    '[data-region="courses-view"]',
                    ".block_myoverview",
                    "main",
                ),
            ),
            ("/my/", (".block_myoverview", '[data-region="course-content"]')),
            ("/course/index.php", ("main",)),
            ("/course/management.php", ("main",)),
        )
        for path, scope_selectors in pages:
            try:
                r = self.s.get(self.base + path, timeout=30)
                r.raise_for_status()
            except Exception:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            scopes = []
            for selector in scope_selectors:
                scopes.extend(soup.select(selector))
            if not scopes and path == "/my/courses.php":
                scopes = [soup]

            before = len(found)
            for scope in scopes:
                for a in scope.select('a[href*="/course/view.php?id="]'):
                    m = re.search(r"[?&]id=(\d+)", a.get("href", ""))
                    name = " ".join(a.get_text(" ", strip=True).split())
                    if (
                        not m
                        or not name
                        or m.group(1) in seen
                        or m.group(1) == "1"
                        or (search_folded and search_folded not in name.casefold())
                    ):
                        continue
                    seen.add(m.group(1))
                    found.append(
                        {
                            "id": int(m.group(1)),
                            "fullname": name,
                            "shortname": "",
                            "kategorie": "",
                        }
                    )
            if found:
                # Dashboard pages contain enrolled courses. Do not mix them
                # with the site-wide public course catalogue.
                if path.startswith("/my/") and len(found) > before:
                    break
                if path.startswith("/course/"):
                    break
        return found

    # -- Struktur --

    def course_state(self, courseid):
        data = self.ajax("core_courseformat_get_state", {"courseid": int(courseid)})
        if isinstance(data, str):
            try:
                return json.loads(data)
            except ValueError:
                return {"_nicht_parsebar": data[:3000]}
        return data

    def course_structure(self, courseid):
        try:
            st = self.course_state(courseid)
            secs = self._sections_from_state(st)
            if secs:
                return {"abschnitte": secs, "quelle": "core_courseformat_get_state"}
        except Exception as e:
            log(f"State-Endpoint fehlgeschlagen: {e}")
        return {"abschnitte": self._sections_from_html(courseid),
                "quelle": "HTML-Parsing"}

    @staticmethod
    def _sections_from_state(st):
        if not isinstance(st, dict):
            return []
        raw_secs = st.get("section") or st.get("sections") or []
        raw_cms = st.get("cm") or st.get("cms") or []
        if not isinstance(raw_secs, list) or not raw_secs:
            return []

        cm_by_id = {}
        for cm in raw_cms if isinstance(raw_cms, list) else []:
            if isinstance(cm, dict) and cm.get("id") is not None:
                cm_by_id[str(cm["id"])] = {
                    "name": cm.get("name") or cm.get("title") or "?",
                    "modul": cm.get("module") or cm.get("modname"),
                    "sichtbar": cm.get("visible"),
                }

        out = []
        for s in raw_secs:
            if not isinstance(s, dict):
                continue
            cmlist = s.get("cmlist") or s.get("cmids") or []
            acts = []
            for cid in cmlist if isinstance(cmlist, list) else []:
                info = cm_by_id.get(str(cid), {})
                acts.append({"cmid": cid,
                             "name": info.get("name", "(unbekannt)"),
                             "modul": info.get("modul")})
            out.append({
                # section_id ist die DB-ID -> die braucht rename_section
                "section_id": s.get("id"),
                "nummer": s.get("number", s.get("section")),
                "name": s.get("title") or s.get("name") or "(ohne Namen)",
                "aktivitaeten": acts,
            })
        return out

    def _sections_from_html(self, courseid):
        r = self.s.get(f"{self.base}/course/view.php",
                       params={"id": courseid}, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for li in soup.select('[data-for="section"]'):
            acts = []
            for cm in li.select('[data-for="cmitem"]'):
                nm = cm.select_one(".instancename") or cm.select_one(".activityname a")
                acts.append({"cmid": cm.get("data-id"),
                             "name": nm.get_text(strip=True) if nm else "?"})
            out.append({"section_id": li.get("data-id"),
                        "nummer": li.get("data-number"),
                        "name": li.get("data-sectionname") or "(ohne Namen)",
                        "aktivitaeten": acts})
        return out

    # -- Umbenennen --

    def rename_section(self, section_id, new_name,
                       component="format_topics", itemtype="sectionnamenl"):
        return self.ajax("core_update_inplace_editable", {
            "component": component, "itemtype": itemtype,
            "itemid": int(section_id), "value": new_name,
        })

    def rename_activity(self, cmid, new_name):
        return self.ajax("core_update_inplace_editable", {
            "component": "core_course", "itemtype": "activityname",
            "itemid": int(cmid), "value": new_name,
        })

    # -- Kurs erstellen --

    def create_course(self, fullname, shortname, categoryid=1, summary="",
                      numsections=5, course_format="topics", visible=1):
        """
        Submit-Buttons duerfen NICHT mitgesendet werden - ausser dem
        gewuenschten ('saveanddisplay'). 'updatecourseformat' waere sonst
        ein No-Submit-Button und Moodle rendert nur das Formular neu.
        """
        edit_url = f"{self.base}/course/edit.php"
        r = self.s.get(edit_url, params={"category": categoryid}, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        form = (soup.find("form", id=re.compile(r"^mform1"))
                or soup.find("form", action=re.compile(r"edit\.php"))
                or soup.find("form"))
        if form is None:
            raise RuntimeError("Kein Formular auf /course/edit.php - fehlende "
                               "Rechte oder Session abgelaufen.")

        SKIP = {"submit", "button", "image", "reset", "file"}
        data = {}
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue
            if el.name == "input":
                itype = (el.get("type") or "text").lower()
                if itype in SKIP:
                    continue
                if itype in ("checkbox", "radio") and not el.has_attr("checked"):
                    continue
                data[name] = el.get("value", "")
            elif el.name == "select":
                opt = el.find("option", selected=True) or el.find("option")
                val = opt.get("value", "") if opt else ""
                if name.endswith("[]") and not val:
                    continue
                data[name] = val
            else:
                data[name] = el.get_text()

        data.update({
            "fullname": fullname, "shortname": shortname,
            "category": str(categoryid),
            "summary_editor[text]": summary, "summary_editor[format]": "1",
            "format": course_format, "numsections": str(numsections),
            "visible": str(visible),
            "saveanddisplay": "Save and display",
        })

        resp = self.s.post(edit_url, data=data, timeout=60,
                           headers={"Referer": r.url})
        resp.raise_for_status()

        m = re.search(r"/course/view\.php\?id=(\d+)", resp.url)
        if m:
            cid = int(m.group(1))
            return {"kurs_id": cid, "url": f"{self.base}/course/view.php?id={cid}"}

        rsoup = BeautifulSoup(resp.text, "html.parser")
        errs = []
        for sel in (".invalid-feedback", ".form-control-feedback",
                    ".alert-danger", ".is-invalid"):
            for e in rsoup.select(sel):
                t = e.get_text(strip=True)
                if t:
                    errs.append(t)
        raise RuntimeError(
            "Kurs wurde nicht angelegt. Antwort-URL: " + resp.url
            + " | Formularfehler: "
            + ("; ".join(dict.fromkeys(errs))[:400] or "keine erkannt")
            + " | Haeufigste Ursache: Kurzname bereits vergeben."
        )

    # ------------------------------------------------------- Aktivitaeten --
    #
    # Feldnamen verifiziert per Formular-Dump (Inspektor v2, 30.07.2026)
    # gegen page/resource/folder/url/label/forum/assign/quiz. Nicht
    # verifiziert: ob das Anlegen tatsaechlich zum gewuenschten Aktivitaets-
    # typ fuehrt (nur die Formularstruktur wurde gelesen, kein echter
    # Submit getestet). Erste Nutzung entsprechend pruefen.

    _ACTIVITY_MODULE_IDS = {
        # 'module' ist die interne Modul-ID des Typs. Aus dem Formular-Dump
        # kennen wir nur 'page' (id=15) sicher. Die anderen werden deshalb
        # NICHT per fester ID, sondern indem das Formular selbst geladen
        # und sein 'module'-Feld ausgelesen wird - robuster gegen
        # abweichende IDs je Moodle-Installation.
    }

    def _load_modedit_form(self, activity_type: str, course_id: int,
                           section_number: int):
        """Laedt das Anlege-Formular (add=...) - siehe _load_modedit_form_raw."""
        return self._load_modedit_form_raw({
            "add": activity_type, "course": course_id,
            "section": section_number, "return": 0,
        }, kontext=f"Typ '{activity_type}'")

    def _load_modedit_edit_form(self, cmid: int):
        """
        Laedt das Bearbeiten-Formular (update=cmid) - im Unterschied zum
        Anlegen sind hier ALLE Felder bereits mit den AKTUELLEN Werten der
        Aktivitaet vorausgefuellt (Name, Inhalt, Draft-Datei-Referenzen
        etc.), inklusive 'update'-Hidden-Feld statt 'add'/'course'/
        'section'. Nur die vom Aufrufer gewuenschten Felder werden
        anschliessend ueberschrieben, der Rest bleibt unveraendert
        erhalten - so werden bestehende Einstellungen nicht versehentlich
        zurueckgesetzt.
        """
        return self._load_modedit_form_raw({"update": cmid},
                                           kontext=f"cmid={cmid}")

    def _load_modedit_form_raw(self, query_params: dict, kontext: str):
        """
        Gemeinsame Kernlogik fuer beide Modi. 'completion*'-Felder werden
        bewusst NICHT aus den Formular-Defaults uebernommen: Live-Test
        zeigte, dass Moodles Standard-Vorauswahl bei Abschlussverfolgung/
        Bewertung in Widerspruch geraten kann (z.B. Forum: 'Require grade
        can't be enabled for Rating because grading by Rating is not
        enabled.'). Ohne diese Felder nutzt Moodle seine eigenen sicheren
        internen Defaults bzw. - im Bearbeiten-Modus - die bereits
        gespeicherten Werte bleiben unangetastet, da wir sie schlicht
        nicht mit senden (Moodle behandelt fehlende Felder beim Update
        nicht als 'loeschen').
        """
        r = self.s.get(f"{self.base}/course/modedit.php", params=query_params,
                       timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        form = (soup.find("form", id=re.compile(r"^mform1"))
                or soup.find("form", action=re.compile(r"modedit\.php")))
        if form is None:
            raise RuntimeError(
                f"Kein Formular gefunden ({kontext}). Aktivitaet vorhanden? "
                "Rechte ausreichend?"
            )

        hidden = {}
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name or name.startswith("completion"):
                continue
            if el.name == "input":
                itype = (el.get("type") or "text").lower()
                if itype in ("submit", "button", "image", "reset", "file"):
                    continue
                if itype in ("checkbox", "radio") and not el.has_attr("checked"):
                    continue
                hidden[name] = el.get("value", "")
            elif el.name == "select":
                opt = el.find("option", selected=True) or el.find("option")
                val = opt.get("value", "") if opt else ""
                if name.endswith("[]") and not val:
                    continue
                hidden[name] = val
            else:
                hidden[name] = el.get_text()

        return soup, form, hidden

    def create_activity(self, course_id: int, section_number: int,
                        activity_type: str, name: str, intro: str = "",
                        extra_fields: dict = None):
        """
        Legt eine Aktivitaet an. extra_fields ueberschreibt/ergaenzt Felder,
        die je nach Typ noetig sind (z.B. {'externalurl': 'https://...'}
        bei 'url', oder {'page[text]': '<p>Inhalt</p>'} bei 'page').

        Fuer 'resource'/'folder' muss vorher upload_file() aufgerufen und
        dessen draft_itemid als extra_fields={'files': <itemid>} genutzt
        werden - siehe dort.
        """
        extra_fields = extra_fields or {}
        edit_url = f"{self.base}/course/modedit.php"
        _, form, data = self._load_modedit_form(activity_type, course_id,
                                                 section_number)

        data["name"] = name
        if "introeditor[text]" in data:
            data["introeditor[text]"] = intro
            data.setdefault("introeditor[format]", "1")
        data.update(extra_fields)
        data["submitbutton"] = "Save and display"

        resp = self.s.post(edit_url, data=data, timeout=60,
                           headers={"Referer": edit_url})
        resp.raise_for_status()

        m = re.search(r"/(?:mod/\w+/view|course/view)\.php\?id=(\d+)", resp.url)
        rsoup = BeautifulSoup(resp.text, "html.parser")
        errs = [e.get_text(strip=True) for e in
                rsoup.select(".invalid-feedback, .alert-danger, .is-invalid")
                if e.get_text(strip=True)]

        if not m and not errs:
            # Manchmal landet man auf course/view.php ohne id= im Modulpfad -
            # dann anhand der Kurs-URL pruefen, ob wir dort sind.
            if f"/course/view.php?id={course_id}" in resp.url:
                m = True

        if not m:
            raise RuntimeError(
                f"Aktivitaet '{activity_type}' vermutlich nicht angelegt. "
                f"Antwort-URL: {resp.url} | Fehler: "
                + ("; ".join(dict.fromkeys(errs))[:400] or "keine erkannt")
            )

        return {"antwort_url": resp.url,
                "hinweis": "Redirect erhalten - im Browser pruefen, ob die "
                           "Aktivitaet wie erwartet aussieht."}

    def update_activity(self, cmid: int, name: str = None, intro: str = None,
                        extra_fields: dict = None):
        """
        Bearbeitet eine bestehende Aktivitaet inhaltlich. Nur die
        uebergebenen Parameter werden geaendert, alles andere bleibt wie
        gespeichert (Formular wird mit aktuellen Werten vorausgefuellt
        geladen, siehe _load_modedit_edit_form).

        name: neuer Titel, oder None um ihn unveraendert zu lassen.
        intro: neue Kurzbeschreibung (introeditor[text]), oder None.
        extra_fields: typspezifische Felder ueberschreiben, z.B.
          {'page[text]': '<p>Neuer Inhalt</p>'} bei einer Seite, oder
          {'externalurl': 'https://...'} bei einem Link.
        """
        extra_fields = extra_fields or {}
        edit_url = f"{self.base}/course/modedit.php"
        _, form, data = self._load_modedit_edit_form(cmid)

        if name is not None:
            data["name"] = name
        if intro is not None and "introeditor[text]" in data:
            data["introeditor[text]"] = intro
            data.setdefault("introeditor[format]", "1")
        data.update(extra_fields)
        data["submitbutton"] = "Save and display"

        resp = self.s.post(edit_url, data=data, timeout=60,
                           headers={"Referer": edit_url})
        resp.raise_for_status()

        m = re.search(r"/(?:mod/\w+/view|course/view)\.php\?id=(\d+)", resp.url)
        rsoup = BeautifulSoup(resp.text, "html.parser")
        errs = [e.get_text(strip=True) for e in
                rsoup.select(".invalid-feedback, .alert-danger, .is-invalid")
                if e.get_text(strip=True)]

        if not m:
            raise RuntimeError(
                f"Aktivitaet cmid={cmid} vermutlich nicht aktualisiert. "
                f"Antwort-URL: {resp.url} | Fehler: "
                + ("; ".join(dict.fromkeys(errs))[:400] or "keine erkannt")
            )

        return {"antwort_url": resp.url,
                "hinweis": "Redirect erhalten - im Browser pruefen, ob die "
                           "Aenderung wie erwartet uebernommen wurde."}

    # --------------------------------------------------------- Generisch --
    #
    # Fuer Formulare ausserhalb von course/modedit.php (z.B. Fragenbank,
    # Einschreibung, Kategorien). Gleiche Grundidee: Formular mit aktuellen
    # Defaults laden, gezielt ueberschreiben, absenden. Nur Pfade auf der
    # eigenen Instanz erlaubt (kein SSRF auf fremde Hosts).

    def _resolve_url(self, url_or_path: str) -> str:
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            if not url_or_path.startswith(self.base):
                raise ValueError(
                    f"URL '{url_or_path}' liegt ausserhalb dieser Moodle-"
                    f"Instanz ({self.base}) - abgelehnt."
                )
            return url_or_path
        return self.base.rstrip("/") + "/" + url_or_path.lstrip("/")

    def fetch_form(self, url_or_path: str, params: dict = None,
                  form_selector: str = None, exclude_prefixes=()):
        """
        Laedt eine beliebige Seite dieser Moodle-Instanz und liest das
        (erste passende) Formular aus: alle Feldnamen, Typen, aktuelle
        Werte, Optionen bei Selects, Pflichtfelder, Submit-Buttons.

        form_selector: optionaler CSS-Selektor, falls mehrere Formulare
          auf der Seite stehen (z.B. 'form#questionform').
        exclude_prefixes: Feldnamen mit diesen Praefixen werden aus der
          zurueckgegebenen Default-Belegung ausgeschlossen (z.B.
          ('completion',) wie beim Aktivitaeten-Formular).
        """
        url = self._resolve_url(url_or_path)
        r = self.s.get(url, params=params or {}, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        form = (soup.select_one(form_selector) if form_selector else None) \
            or soup.find("form", id=re.compile(r"^mform1")) \
            or soup.find("form")
        if form is None:
            raise RuntimeError(f"Kein Formular gefunden auf {r.url}")

        felder = []
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name or any(name.startswith(p) for p in exclude_prefixes):
                continue
            eintrag = {"tag": el.name, "name": name,
                      "type": el.get("type") if el.name == "input" else None}
            if el.name == "select":
                eintrag["optionen"] = [
                    {"value": o.get("value", ""), "text": o.get_text(strip=True),
                     "ausgewaehlt": o.has_attr("selected")}
                    for o in el.find_all("option")
                ]
            elif (el.get("type") or "").lower() in ("checkbox", "radio"):
                eintrag["value"] = el.get("value", "")
                eintrag["checked"] = el.has_attr("checked")
            else:
                eintrag["value"] = (el.get_text() if el.name == "textarea"
                                    else el.get("value", ""))
            eintrag["required"] = (el.has_attr("required")
                                   or el.get("aria-required") == "true")
            felder.append(eintrag)

        return {
            "endgueltige_url": r.url,
            "form_action": form.get("action"),
            "form_method": form.get("method", "get"),
            "form_id": form.get("id"),
            "felder": felder,
        }

    def submit_form(self, action_url: str, data: dict, referer: str = None):
        """
        Sendet Formulardaten per POST an eine beliebige Seite dieser
        Instanz. Gibt die Antwort-URL (fuer Redirect-Erkennung) sowie
        etwaige sichtbare Fehlertexte zurueck.
        """
        url = self._resolve_url(action_url)
        headers = {"Referer": referer or url}
        r = self.s.post(url, data=data, timeout=60, headers=headers)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        errs = [e.get_text(strip=True) for e in
                soup.select(".invalid-feedback, .alert-danger, .is-invalid, .error")
                if e.get_text(strip=True)]
        return {
            "antwort_url": r.url,
            "fehlertexte": list(dict.fromkeys(errs))[:10],
            "html_laenge": len(r.text),
        }

    def upload_draft_file(self, filename: str, content_bytes: bytes,
                          course_context_id: int = None,
                          repo_id_hint: int = None):
        """
        Laedt eine Datei in den eigenen Draft-Bereich und gibt die
        draft_itemid zurueck, die anschliessend als extra_fields={'files':
        itemid} bei create_activity('resource'/'folder', ...) genutzt wird.

        Die numerische repo_id des "Upload a file"-Plugins steht nirgendwo
        offensichtlich im HTML/JS (Live-Check bestaetigt: weder inline
        eingebettet noch in der Repository-Verwaltungsseite als ID
        sichtbar - nur Plugin-Typen, keine Instanz-IDs). Deshalb wird sie
        hier automatisch ermittelt: 'Upload a file' ist der einzige
        Repository-Typ, der einen direkten Upload ohne Login/OAuth
        akzeptiert - die erste repo_id, die den Test-Upload nicht mit
        einem klaren Auth-/Existenzfehler ablehnt, wird verwendet und
        fuer die Session zwischengespeichert.
        """
        if repo_id_hint is not None:
            kandidaten = [repo_id_hint]
        elif getattr(self, "_upload_repo_id", None) is not None:
            kandidaten = [self._upload_repo_id]
        else:
            kandidaten = list(range(1, 21))

        ctx = course_context_id or 1
        letzter_fehler = None

        for repo_id in kandidaten:
            itemid = int(time.time() * 1000) % (2**31)
            files = {"repo_upload_file": (filename, content_bytes,
                                          "application/octet-stream")}
            data = {
                "itemid": str(itemid), "sesskey": self.sesskey,
                "savepath": "/", "title": filename,
                "ctx_id": str(ctx), "action": "upload",
                "repo_id": str(repo_id),
            }
            try:
                r = self.s.post(f"{self.base}/repository/repository_ajax.php",
                                params={"action": "upload"}, data=data,
                                files=files, timeout=60)
                r.raise_for_status()
                result = r.json()
            except Exception as e:
                letzter_fehler = f"repo_id={repo_id}: {e}"
                continue

            if isinstance(result, dict) and result.get("error"):
                letzter_fehler = f"repo_id={repo_id}: {result.get('errorcode')} - {result.get('error')}"
                continue

            self._upload_repo_id = repo_id
            return {"draft_itemid": itemid, "repo_id_verwendet": repo_id,
                   "rohantwort": result}

        raise RuntimeError(
            "Kein Repository fuer Upload gefunden (IDs 1-20 durchprobiert). "
            f"Letzter Fehler: {letzter_fehler}. Moeglich: 'Upload a file' "
            "ist deaktiviert, oder die ID liegt ausserhalb 1-20 - dann "
            "repo_id_hint mit einer hoeheren Zahl versuchen."
        )

    def edit_module_action(self, cmid: int, action: str):
        """
        action in {'hide','show','stealth','duplicate','delete'} - Funktion
        existiert bestaetigt (Sondierung, Inspektor v2), welche Aktionen
        genau unterstuetzt werden ist NICHT einzeln verifiziert.
        """
        return self.ajax("core_course_edit_module", {"id": int(cmid),
                                                      "action": action})

    def edit_section_action(self, section_id: int, action: str):
        """action in {'hide','show','delete','setmarker'} - siehe Hinweis
        bei edit_module_action."""
        return self.ajax("core_course_edit_section", {"id": int(section_id),
                                                       "action": action})

    def move_activity(self, cmid: int, target_section_id: int, course_id: int):
        """
        core_courseformat_update_course, action=cm_move - verifiziert per
        Live-Test (30.07.2026): cmid landete korrekt in der Ziel-Section,
        Rueckgabe zeigte sectionid/cmlist aktualisiert.
        Der fruehere Versuch ueber core_course_move_module(cmid, sectionid)
        schlug mit invalidrecordunknown fehl - falscher Parametersatz.
        """
        return self.ajax("core_courseformat_update_course", {
            "action": "cm_move", "courseid": int(course_id),
            "ids": [int(cmid)], "targetsectionid": int(target_section_id),
        })

    def whoami(self):
        r = self.s.get(f"{self.base}/my/", timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        return {
            "url": self.base,
            "userid": self.userid,
            "seitentitel": soup.title.get_text(strip=True) if soup.title else "?",
            "angemeldet": bool(self.sesskey),
        }


class MoodleAjaxError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


# ------------------------------------------------------- Session-Verwaltung

_session = None
_session_creds = None

SESSION_ABGELAUFEN = {"invalidsesskey", "servicerequireslogin",
                      "requireloginerror", "sessionerror", "loggedinnot"}


def get_session(force_relogin=False):
    """Baut die Session bei Bedarf auf und meldet sich erneut an."""
    global _session, _session_creds

    creds = load_credentials()
    if not creds or not creds.get("url") or not creds.get("username"):
        raise RuntimeError(
            "Keine Zugangsdaten gespeichert. Rufe zuerst 'moodle_login' auf "
            "(oeffnet ein Fenster) oder 'moodle_login_manual'."
        )
    if not creds.get("password"):
        raise RuntimeError(
            "Benutzername und URL sind bekannt, aber kein Passwort abrufbar. "
            "Bitte erneut anmelden ('moodle_login')."
        )

    if _session is not None and not force_relogin and _session_creds == (
            creds["url"], creds["username"]):
        return _session

    s = MoodleSession(creds["url"], creds.get("verify_tls", True))
    if not s.login(creds["username"], creds["password"]):
        raise RuntimeError(
            "Anmeldung fehlgeschlagen (kein sesskey). Falsche Zugangsdaten, "
            "oder die Instanz nutzt SSO/2FA - damit funktioniert dieser "
            "Ansatz nicht."
        )
    _session = s
    _session_creds = (creds["url"], creds["username"])
    log(f"Angemeldet als {creds['username']} (userid={s.userid})")
    return s


def with_retry(fn):
    """
    Fuehrt fn(session) aus. Bei abgelaufener Session einmal neu anmelden
    und wiederholen - Moodle-Sessions laufen ab, das darf den Nutzer
    nicht treffen.
    """
    s = get_session()
    try:
        return fn(s)
    except MoodleAjaxError as e:
        if e.code in SESSION_ABGELAUFEN:
            log(f"Session abgelaufen ({e.code}) - melde neu an.")
            return fn(get_session(force_relogin=True))
        raise
    except RuntimeError as e:
        if "abgelaufen" in str(e) or "Rechte" in str(e):
            log("Vermute abgelaufene Session - melde neu an.")
            return fn(get_session(force_relogin=True))
        raise


# ------------------------------------------------------------ MCP-Werkzeuge

mcp = FastMCP("moodle")


@mcp.tool()
def moodle_login(vorbelegte_url: str = "") -> dict:
    """Oeffnet ein lokales Anmeldefenster fuer Moodle und speichert die
    Zugangsdaten auf diesem Rechner. Nutze dies, wenn der Nutzer sich
    anmelden moechte, ohne sein Passwort in den Chat zu schreiben.
    Braucht eine grafische Oberflaeche; scheitert das, nutze
    moodle_login_manual.

    vorbelegte_url: optionale Vorbelegung des URL-Felds.
    """
    global _session
    res = start_login_gui_subprocess(vorbelegte_url)
    if res.get("abgebrochen"):
        return {"status": "abgebrochen",
                "hinweis": "Der Nutzer hat das Fenster geschlossen."}

    s = MoodleSession(res["url"], res.get("verify_tls", True))
    if not s.login(res["username"], res["password"]):
        return {"status": "fehlgeschlagen",
                "hinweis": "Anmeldung bei Moodle nicht erfolgreich. Zugangs"
                           "daten pruefen. Nichts wurde gespeichert. Bei "
                           "SSO/2FA funktioniert dieser Ansatz nicht."}

    wo = save_credentials(res["url"], res["username"], res["password"],
                          res.get("verify_tls", True))
    _session = None      # erzwingt frischen Aufbau ueber get_session()
    return {"status": "angemeldet", "url": res["url"],
            "benutzer": res["username"], "userid": s.userid,
            "passwort_gespeichert_in": wo}


@mcp.tool()
def moodle_login_manual(url: str, username: str, password: str,
                        verify_tls: bool = True) -> dict:
    """Meldet sich mit direkt uebergebenen Zugangsdaten bei Moodle an und
    speichert sie lokal. Nur nutzen, wenn moodle_login (Fenster) nicht
    funktioniert - hier laeuft das Passwort durch den Chatverlauf.

    url: z.B. http://192.168.1.10:8081
    verify_tls: bei http oder selbstsigniertem Zertifikat auf false setzen.
    """
    global _session
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    s = MoodleSession(url, verify_tls)
    if not s.login(username, password):
        return {"status": "fehlgeschlagen",
                "hinweis": "Kein sesskey erhalten. Zugangsdaten pruefen. "
                           "Nichts gespeichert."}
    wo = save_credentials(url, username, password, verify_tls)
    _session = None
    return {"status": "angemeldet", "url": url, "benutzer": username,
            "userid": s.userid, "passwort_gespeichert_in": wo}


@mcp.tool()
def moodle_status() -> dict:
    """Zeigt, ob Zugangsdaten gespeichert sind und ob die Verbindung zur
    Moodle-Instanz funktioniert."""
    creds = load_credentials()
    if not creds:
        return {"angemeldet": False,
                "hinweis": "Keine Zugangsdaten gespeichert. 'moodle_login' "
                           "oeffnet ein Anmeldefenster.",
                "keyring_verfuegbar": _HAS_KEYRING,
                "mcp_variante": _MCP_FLAVOR}
    try:
        s = get_session()
        info = s.whoami()
        info.update({"credential_quelle": creds["quelle"],
                     "keyring_verfuegbar": _HAS_KEYRING,
                     "mcp_variante": _MCP_FLAVOR})
        return info
    except Exception as e:
        return {"angemeldet": False, "fehler": str(e),
                "credential_quelle": creds.get("quelle")}


@mcp.tool()
def moodle_logout() -> dict:
    """Loescht die lokal gespeicherten Moodle-Zugangsdaten."""
    global _session, _session_creds
    _session = None
    _session_creds = None
    entfernt = clear_credentials()
    return {"status": "abgemeldet",
            "entfernt": entfernt or ["nichts vorhanden"]}


@mcp.tool()
def moodle_list_courses(search: str = "", limit: int = 25,
                        offset: int = 0) -> dict:
    """Listet die Kurse der Moodle-Instanz mit ID, Name, Kurzname und
    Kategorie. Die ID brauchst du fuer alle kursbezogenen Werkzeuge.

    Die Ausgabe ist paginiert, damit auch Konten mit vielen Kursen stabil
    funktionieren. Standardmaessig werden 25 Kurse geliefert. Falls
    'weitere_vorhanden' wahr ist, rufe das Werkzeug erneut mit dem Wert aus
    'naechster_offset' auf. Mit search kann serverseitig nach Kursnamen
    gesucht werden; limit darf zwischen 1 und 100 liegen.
    """
    return with_retry(lambda s: s.list_courses(search, limit, offset))


@mcp.tool()
def moodle_course_structure(course_id: int) -> dict:
    """Zeigt Abschnitte und Aktivitaeten eines Kurses.

    Wichtig: 'section_id' ist die Datenbank-ID des Abschnitts und wird von
    moodle_rename_section erwartet - nicht die Feldnummer 'nummer'.
    """
    return with_retry(lambda s: s.course_structure(course_id))


@mcp.tool()
def moodle_create_course(fullname: str, shortname: str, category_id: int = 1,
                         summary: str = "", num_sections: int = 5,
                         course_format: str = "topics",
                         visible: bool = True) -> dict:
    """Erstellt einen neuen Kurs und gibt dessen ID und URL zurueck.

    shortname muss instanzweit eindeutig sein, sonst schlaegt es fehl.
    course_format: 'topics' (Standard, Themenabschnitte), 'weeks'
    (Wochenabschnitte), 'social' (nur Forum, keine Abschnitte),
    'singleactivity' (eine Aktivitaet). Fuer Kurse mit Inhalten 'topics'.
    """
    if course_format not in ("topics", "weeks", "social", "singleactivity"):
        return {"fehler": f"Unbekanntes Format '{course_format}'. Erlaubt: "
                          "topics, weeks, social, singleactivity."}
    return with_retry(lambda s: s.create_course(
        fullname, shortname, category_id, summary, num_sections,
        course_format, 1 if visible else 0))


@mcp.tool()
def moodle_rename_section(section_id: int, new_name: str,
                          course_format: str = "format_topics") -> dict:
    """Benennt einen Kursabschnitt um.

    section_id ist die Datenbank-ID aus moodle_course_structure (Feld
    'section_id'), NICHT die Abschnittsnummer. course_format muss zum
    Kursformat passen: 'format_topics' oder 'format_weeks'.
    """
    return {"ergebnis": with_retry(
        lambda s: s.rename_section(section_id, new_name, course_format))}


@mcp.tool()
def moodle_rename_activity(cmid: int, new_name: str) -> dict:
    """Benennt eine Aktivitaet um. cmid stammt aus moodle_course_structure."""
    return {"ergebnis": with_retry(lambda s: s.rename_activity(cmid, new_name))}


@mcp.tool()
def moodle_create_activity(course_id: int, section_number: int,
                           activity_type: str, name: str, intro: str = "",
                           extra_fields_json: str = "{}") -> dict:
    """Legt eine Aktivitaet in einem Kursabschnitt an.

    ⚠️ Formularfelder sind gegen diese Instanz verifiziert (Inspektor v2),
    ein tatsaechlicher Submit ist aber NICHT End-zu-Ende getestet - nach
    dem ersten Einsatz im Browser pruefen, ob die Aktivitaet korrekt ist.

    activity_type: 'page', 'url', 'label', 'folder', 'forum', 'assign',
      'resource' (Datei - braucht vorher moodle_upload_file), 'quiz'
      (nur Grundeinstellungen, keine Fragen).
    section_number: die Abschnitts-NUMMER (0, 1, 2, ...), NICHT die
      section_id aus moodle_course_structure.
    extra_fields_json: typspezifische Pflicht-/Zusatzfelder als JSON, z.B.
      '{"externalurl": "https://example.com"}' bei 'url', oder
      '{"page[text]": "<p>Inhalt</p>"}' bei 'page', oder
      '{"files": 123456}' bei 'resource'/'folder' (itemid aus
      moodle_upload_file).
    """
    try:
        extra = json.loads(extra_fields_json)
    except ValueError as e:
        return {"fehler": f"extra_fields_json ist kein gueltiges JSON: {e}"}

    bekannte_typen = {"page", "url", "label", "folder", "forum", "assign",
                      "resource", "quiz"}
    if activity_type not in bekannte_typen:
        return {"fehler": f"Unbekannter/ungetesteter Typ '{activity_type}'. "
                          f"Gegen diese Instanz geprueft: {sorted(bekannte_typen)}. "
                          "Andere Typen ggf. ueber moodle_raw_ajax/eigenen "
                          "Formular-Dump erforschen."}

    return with_retry(lambda s: s.create_activity(
        course_id, section_number, activity_type, name, intro, extra))


@mcp.tool()
def moodle_update_activity(cmid: int, name: str = None, intro: str = None,
                           extra_fields_json: str = "{}") -> dict:
    """Bearbeitet den Inhalt einer bestehenden Aktivitaet, ohne sie neu
    anzulegen. Nur die uebergebenen Parameter werden geaendert - alles
    andere (Einstellungen, nicht genannte Felder) bleibt wie gespeichert,
    da das Formular mit den aktuellen Werten vorausgefuellt geladen und
    nur gezielt ueberschrieben wird.

    cmid: die Aktivitaet, die geaendert werden soll (aus
      moodle_course_structure).
    name: neuer Titel, weglassen um ihn unveraendert zu lassen.
    intro: neue Kurzbeschreibung, weglassen um sie unveraendert zu lassen.
    extra_fields_json: typspezifische Inhaltsfelder ueberschreiben, z.B.
      '{"page[text]": "<p>Neuer Inhalt</p>"}' bei einer Seite, oder
      '{"externalurl": "https://neue-adresse.de"}' bei einem Link.
    """
    try:
        extra = json.loads(extra_fields_json)
    except ValueError as e:
        return {"fehler": f"extra_fields_json ist kein gueltiges JSON: {e}"}

    return with_retry(lambda s: s.update_activity(cmid, name, intro, extra))


@mcp.tool()
def moodle_upload_file(filename: str, content_base64: str,
                       repo_id_hint: int = None) -> dict:
    """Laedt eine Datei in den eigenen Draft-Bereich hoch und gibt eine
    draft_itemid zurueck, die bei moodle_create_activity (Typ 'resource'
    oder 'folder') als extra_fields_json='{"files": <itemid>}' verwendet
    wird.

    Die repo_id des "Upload a file"-Plugins wird automatisch ermittelt
    (IDs 1-20 werden mit dem echten Upload durchprobiert, die erste
    erfolgreiche wird fuer die restliche Session gemerkt). Falls das
    fehlschlaegt, repo_id_hint mit einer hoeheren Zahl versuchen.

    content_base64: Dateiinhalt Base64-kodiert.
    """
    import base64
    try:
        raw = base64.b64decode(content_base64)
    except Exception as e:
        return {"fehler": f"content_base64 ist kein gueltiges Base64: {e}"}
    try:
        return with_retry(lambda s: s.upload_draft_file(
            filename, raw, repo_id_hint=repo_id_hint))
    except Exception as e:
        return {"fehler": str(e),
                "hinweis": "Upload-Mechanismus unverifiziert - ggf. mit "
                           "DevTools einen echten Datei-Upload im Browser "
                           "beobachten und Feldnamen abgleichen."}


@mcp.tool()
def moodle_edit_module(cmid: int, action: str) -> dict:
    """Fuehrt eine Verwaltungsaktion auf einer Aktivitaet aus.

    action: 'hide', 'show', 'stealth', 'duplicate' oder 'delete'.
    ⚠️ Funktion existiert bestaetigt (Sondierung), aber nicht jede
    einzelne Aktion wurde real getestet - insbesondere 'delete' und
    'duplicate' vor echtem Einsatz auf Testkurs pruefen.
    """
    erlaubt = {"hide", "show", "stealth", "duplicate", "delete"}
    if action not in erlaubt:
        return {"fehler": f"Unbekannte Aktion '{action}'. Erlaubt: {sorted(erlaubt)}"}
    return {"ergebnis": with_retry(lambda s: s.edit_module_action(cmid, action))}


@mcp.tool()
def moodle_edit_section(section_id: int, action: str) -> dict:
    """Fuehrt eine Verwaltungsaktion auf einem Kursabschnitt aus.

    action: 'hide', 'show', 'delete' oder 'setmarker' (als aktuellen
    Abschnitt markieren). section_id ist die Datenbank-ID aus
    moodle_course_structure. ⚠️ 'delete' vor echtem Einsatz auf
    Testkurs pruefen - loescht auch enthaltene Aktivitaeten.
    """
    erlaubt = {"hide", "show", "delete", "setmarker"}
    if action not in erlaubt:
        return {"fehler": f"Unbekannte Aktion '{action}'. Erlaubt: {sorted(erlaubt)}"}
    return {"ergebnis": with_retry(lambda s: s.edit_section_action(section_id, action))}


@mcp.tool()
def moodle_move_activity(cmid: int, target_section_id: int, course_id: int) -> dict:
    """Verschiebt eine Aktivitaet in einen anderen Abschnitt (auch
    innerhalb desselben Kurses). target_section_id ist die Datenbank-ID
    aus moodle_course_structure, nicht die Abschnittsnummer. course_id
    ist die Kurs-ID (fuer diese Moodle-Version zwingend erforderlich,
    per Live-Test verifiziert)."""
    return {"ergebnis": with_retry(
        lambda s: s.move_activity(cmid, target_section_id, course_id))}


@mcp.tool()
def moodle_raw_ajax(methodname: str, args_json: str = "{}") -> dict:
    """Ruft eine beliebige Methode von Moodles internem AJAX-Endpoint auf.
    Zum Erforschen von Funktionen, die noch kein eigenes Werkzeug haben.

    Nur ajax-freigeschaltete Methoden funktionieren; reine
    Web-Service-Funktionen antworten mit 'servicenotavailable'.
    args_json: Argumente als JSON-Objekt, z.B. '{"courseid": 2}'.
    """
    try:
        args = json.loads(args_json)
    except ValueError as e:
        return {"fehler": f"args_json ist kein gueltiges JSON: {e}"}
    return {"ergebnis": with_retry(lambda s: s.ajax(methodname, args))}


@mcp.tool()
def moodle_fetch_form(url_or_path: str, params_json: str = "{}",
                      form_selector: str = None,
                      exclude_prefixes_json: str = "[]") -> dict:
    """Laedt eine beliebige Seite dieser Moodle-Instanz und liest das
    Formular aus (Feldnamen, Typen, aktuelle Werte, Optionen,
    Pflichtfelder). Zum Erforschen von Formularen, fuer die es noch kein
    eigenes Werkzeug gibt (z.B. Fragenbank, Einschreibung, Kategorien).

    url_or_path: absoluter Pfad oder volle URL auf dieser Instanz, z.B.
      '/question/edit.php' oder '/user/index.php'.
    form_selector: CSS-Selektor, falls mehrere Formulare auf der Seite
      stehen, z.B. 'form#questionform'.
    exclude_prefixes_json: Feldnamen-Praefixe, die aus der Ausgabe
      ausgeschlossen werden sollen, als JSON-Liste, z.B. '["completion"]'.
    """
    try:
        params = json.loads(params_json)
        exclude = tuple(json.loads(exclude_prefixes_json))
    except ValueError as e:
        return {"fehler": f"Ungueltiges JSON: {e}"}
    try:
        return with_retry(lambda s: s.fetch_form(
            url_or_path, params, form_selector, exclude))
    except Exception as e:
        return {"fehler": str(e)}


@mcp.tool()
def moodle_submit_form(action_url: str, data_json: str,
                       referer: str = None) -> dict:
    """Sendet Formulardaten per POST an eine beliebige Seite dieser
    Moodle-Instanz. Nur Pfade auf der eigenen Instanz erlaubt.

    action_url: Ziel-URL/Pfad, meist aus moodle_fetch_form uebernommen.
    data_json: alle Formularfelder als JSON-Objekt, typischerweise die
      Werte aus moodle_fetch_form uebernommen und gezielt ueberschrieben.
    referer: optional, meist die zuvor mit moodle_fetch_form geladene URL.
    """
    try:
        data = json.loads(data_json)
    except ValueError as e:
        return {"fehler": f"data_json ist kein gueltiges JSON: {e}"}
    try:
        return with_retry(lambda s: s.submit_form(action_url, data, referer))
    except Exception as e:
        return {"fehler": str(e)}


# ------------------------------------------------------------- Terminal-Setup

def run_setup_cli():
    """
    Einmalige Anmeldung im Terminal - unabhaengig von Claude und ohne
    Display. Das Passwort wird per getpass eingelesen (nicht angezeigt,
    nicht in der Shell-History) und danach nur noch gespeichert.
    """
    import getpass

    print("=== Moodle-MCP: Anmeldung einrichten ===")
    vorhanden = load_credentials()
    if vorhanden:
        print(f"Bereits gespeichert: {vorhanden['username']} @ "
              f"{vorhanden['url']}  (Quelle: {vorhanden['quelle']})")
        if input("Ueberschreiben? [j/N]: ").strip().lower() != "j":
            print("Abgebrochen, nichts geaendert.")
            return

    url = input("Moodle-URL: ").strip()
    if not url:
        print("Abgebrochen.")
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    user = input("Benutzername: ").strip()
    pw = getpass.getpass("Passwort (wird nicht angezeigt): ")
    verify = input("TLS-Zertifikat pruefen? [J/n]: ").strip().lower() != "n"

    print("\nTeste Anmeldung ...")
    s = MoodleSession(url, verify)
    try:
        ok = s.login(user, pw)
    except Exception as e:
        print(f"[!] Verbindungsfehler: {e}")
        print("    Nichts gespeichert.")
        return

    if not ok:
        print("[!] Anmeldung fehlgeschlagen (kein sesskey). Zugangsdaten "
              "pruefen. Bei SSO/2FA funktioniert dieser Ansatz nicht.")
        print("    Nichts gespeichert.")
        return

    wo = save_credentials(url, user, pw, verify)
    print(f"[+] Anmeldung erfolgreich (userid={s.userid}).")
    print(f"[+] Passwort gespeichert in: {wo}")
    if not _HAS_KEYRING:
        print("    Tipp: 'pip install keyring' legt es stattdessen in den "
              "OS-Schluesselbund statt im Klartext.")
    elif "KLARTEXT" in wo:
        print("    Hinweis: keyring ist installiert, aber die sofortige "
              "Rueckprobe ist fehlgeschlagen (Backend unter WSL ohne "
              "persistenten Secret-Service-Daemon?). Deshalb Klartext-"
              "Ablage als sicherer Fallback statt eines kaputten Zustands.")

    # Sofortige End-zu-End-Probe: exakt das, was moodle_status gleich tut.
    print("\nPruefe, ob die gespeicherten Daten wieder ladbar sind ...")
    probe = load_credentials()
    if probe and probe.get("password") == pw:
        print("[+] OK - Zugangsdaten sind vollstaendig ladbar.")
    else:
        print("[!] Zugangsdaten sind NICHT vollstaendig ladbar - das haette "
              "'moodle_status' als Fehler gezeigt. Bitte melden.")

    print("\nFertig. Claude kann die Moodle-Werkzeuge jetzt nutzen.")


# ------------------------------------------------------------------ Einstieg

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--login-gui":
        run_login_gui(sys.argv[2] if len(sys.argv) > 2 else "")
    elif len(sys.argv) > 1 and sys.argv[1] in ("--setup", "--login"):
        run_setup_cli()
    else:
        log(f"Starte MCP-Server ({_MCP_FLAVOR}), "
            f"Keyring: {'ja' if _HAS_KEYRING else 'nein'}")
        mcp.run()
