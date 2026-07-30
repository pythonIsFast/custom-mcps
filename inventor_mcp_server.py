"""
Inventor MCP Server
====================
Ein minimaler Model-Context-Protocol-Server, der Autodesk Inventor
ueber die COM-API fernsteuert. Gedacht als Ausgangspunkt zum Erweitern.

Voraussetzungen (nur Windows):
    - Autodesk Inventor installiert und GEOEFFNET
    - pip install "mcp[cli]" pywin32

Start zum Testen:
    python inventor_mcp_server.py
oder mit dem MCP-Inspector:
    npx @modelcontextprotocol/inspector python inventor_mcp_server.py

Einbindung in Claude Desktop (claude_desktop_config.json):
    {
      "mcpServers": {
        "inventor-mcp": {
          "command": "python",
          "args": ["C:/pfad/zu/inventor_mcp_server.py"]
        }
      }
    }

Wichtige Hinweise:
    - Inventor rechnet intern in ZENTIMETERN. Alle Tools nehmen mm entgegen
      und rechnen intern um (mm / 10 = cm).
    - Ein Vorgang pro Tool-Aufruf. Kein Batching -> sonst Absturzgefahr.
    - COM ist nicht thread-sicher: alle Zugriffe laufen ueber denselben Thread.
"""

from __future__ import annotations

import math
import sys

# --- MCP SDK ---------------------------------------------------------------
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write(
        "Fehlt: MCP-SDK. Installiere mit:  pip install \"mcp[cli]\"\n"
    )
    raise

# --- Inventor COM (nur auf Windows verfuegbar) -----------------------------
try:
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore
    import win32com.client.gencache  # type: ignore
    _HAS_WIN32 = True
except ImportError:
    # Erlaubt Import/Syntaxpruefung auf Nicht-Windows-Systemen.
    _HAS_WIN32 = False

# Modulweiter Cache fuer das per gencache erzeugte Konstanten-Modul.
# Wird erst in _get_app() befuellt, weil dafuer eine laufende COM-Verbindung
# noetig ist. WICHTIG: Wir raten hier KEINE Enum-Zahlen mehr (z.B.
# kNewBodyOperation, kPositiveExtentDirection). Stattdessen liest gencache
# die echten Werte direkt aus Inventors Typbibliothek. Das vermeidet genau
# die Klasse von Fehlern, die vorher zu 'DISP_E_EXCEPTION' gefuehrt hat.
_const = None


mcp = FastMCP("inventor-mcp")

# Modulweiter Cache der Inventor-Applikationsinstanz.
_app = None


# ---------------------------------------------------------------------------
# Interne Hilfsfunktionen
# ---------------------------------------------------------------------------
def _mm_to_cm(value_mm: float) -> float:
    """Inventor-interne Einheit ist cm."""
    return value_mm / 10.0


def _detect_cut_direction(comp_def):
    """
    Erkennt die Richtung des ersten Volumenkoerpers anhand der Bounding-Box
    und liefert die passende ExtendDirection-Konstante fuer Schnitte/Bohrungen,
    die von der XY-Ebene aus gehen.

    Rueckgabe:
        kNegativeExtentDirection  wenn der Koerper ueberwiegend in Z+ liegt
                                  (Schnitt muss nach unten gehen)
        kPositiveExtentDirection  wenn der Koerper ueberwiegend in Z- liegt
                                  (Schnitt muss nach oben gehen)
        kSymmetricExtentDirection wenn der Koerper beidseitig liegt
    """
    if comp_def.SurfaceBodies.Count == 0:
        return _const.kNegativeExtentDirection

    body = comp_def.SurfaceBodies.Item(1)
    try:
        Box = body.RangeBox
        z_min = Box.MinPoint.Z
        z_max = Box.MaxPoint.Z
    except Exception:
        return _const.kNegativeExtentDirection

    tol = 1e-6
    if z_min >= -tol and z_max > tol:
        # Koerper liegt ueberwiegend in Z+ (typisch: direction="positive")
        return _const.kNegativeExtentDirection
    elif z_max <= tol and z_min < -tol:
        # Koerper liegt ueberwiegend in Z- (typisch: direction="negative")
        return _const.kPositiveExtentDirection
    elif z_min < -tol and z_max > tol:
        # Koerper beidseitig (typisch: direction="symmetric")
        return _const.kSymmetricExtentDirection
    else:
        # Koerper liegt auf Z=0 oder ist sehr flach
        return _const.kNegativeExtentDirection


def _resolve_work_plane(comp_def, plane: str):
    """
    Loest eine Ebenen-Angabe in ein WorkPlane-Objekt auf.
    Unterstuetzt:
      - "XY", "XZ", "YZ" (Standard-Ebenen)
      - "XZ:30" (Ebene mit Offset in mm von der Basisebene)
      - "Work Plane1" (benannte Ebene)
    """
    plane = plane.strip()
    if ":" in plane:
        parts = plane.split(":", 1)
        base = parts[0].strip().upper()
        offset_mm = float(parts[1].strip())
        base_map = {"XY": 3, "XZ": 2, "YZ": 1}
        if base not in base_map:
            raise ValueError(f"Unbekannte Basisebene: '{base}'. Erlaubt: XY, XZ, YZ.")
        return comp_def.WorkPlanes.AddByPlaneAndOffset(
            comp_def.WorkPlanes.Item(base_map[base]),
            _mm_to_cm(offset_mm),
        )

    plane_upper = plane.upper()
    if plane_upper in ("XY", "XZ", "YZ"):
        base_map = {"XY": 3, "XZ": 2, "YZ": 1}
        return comp_def.WorkPlanes.Item(base_map[plane_upper])

    # Als benannte Ebene versuchen.
    try:
        return comp_def.WorkPlanes.Item(plane)
    except Exception:
        raise ValueError(
            f"Ebene '{plane}' nicht erkannt. "
            "Erlaubt: 'XY', 'XZ', 'YZ', 'XZ:30' (Offset), 'Work Plane1'."
        )


def _get_app():
    """
    Verbindet sich mit einer laufenden Inventor-Instanz und sorgt dafuer,
    dass win32com.client.constants mit den ECHTEN Enum-Werten aus Inventors
    Typbibliothek befuellt wird (frueher Bindung / gencache).
    Faellt zurueck auf das Starten einer neuen Instanz, falls keine laeuft.
    """
    global _app, _const
    if not _HAS_WIN32:
        raise RuntimeError(
            "pywin32 nicht verfuegbar - dieser Server laeuft nur auf Windows."
        )

    if _app is not None:
        return _app

    pythoncom.CoInitialize()
    try:
        # Bereits laufende Instanz bevorzugen, aber ueber gencache binden,
        # damit die Typbibliothek (und damit die Konstanten) geladen wird.
        _app = win32com.client.gencache.EnsureDispatch("Inventor.Application")
    except pythoncom.com_error:
        # Keine laufende Instanz gefunden -> neu starten.
        _app = win32com.client.gencache.EnsureDispatch("Inventor.Application")
        _app.Visible = True

    # win32com.client.constants wird durch EnsureDispatch global befuellt.
    _const = win32com.client.constants
    return _app


def _require_part_document(app):
    """
    Stellt sicher, dass das aktive Dokument ein Bauteil (Part) ist, UND
    castet es explizit auf PartDocument.

    Grund: app.ActiveDocument liefert bei frueher Bindung (gencache) ein
    generisches 'Document'-Objekt zurueck. Nur ein 'PartDocument' hat die
    Eigenschaft 'ComponentDefinition'. Ohne diesen Cast schlaegt jeder
    Zugriff auf ComponentDefinition mit
    "'...Document instance...' object has no attribute 'ComponentDefinition'"
    fehl.
    """
    doc = app.ActiveDocument
    if doc is None:
        raise RuntimeError("Kein aktives Dokument in Inventor geoeffnet.")
    if doc.DocumentType != _const.kPartDocumentObject:
        raise RuntimeError(
            "Aktives Dokument ist kein Bauteil (Part). "
            "Bitte zuerst 'create_part' aufrufen oder ein .ipt oeffnen."
        )
    # Expliziter Cast auf PartDocument - das behebt den fehlenden Zugriff
    # auf ComponentDefinition.
    part_doc = win32com.client.CastTo(doc, "PartDocument")
    if part_doc is None:
        raise RuntimeError(
            "Cast auf PartDocument fehlgeschlagen. Moeglicherweise ist die "
            "gencache-Typbibliothek veraltet - Ordner "
            "%TEMP%/gen_py loeschen und Inventor/Skript neu starten."
        )
    return part_doc


def _get_translator(app, *keywords):
    """
    Sucht ein Uebersetzer-Add-in ueber seinen Anzeigenamen, statt eine fest
    verdrahtete CLSID zu verwenden. Robuster, weil GUIDs zwischen Versionen
    abweichen koennen. 'keywords' sind Teilstrings, die alle (case-insensitiv)
    im Namen vorkommen muessen, z. B. _get_translator(app, "STL").
    """
    keywords_low = [k.lower() for k in keywords]
    for addin in app.ApplicationAddIns:
        try:
            name = (addin.DisplayName or "").lower()
        except Exception:
            continue
        if all(k in name for k in keywords_low):
            return _cast_translator(addin)
    raise RuntimeError(
        "Kein passendes Uebersetzer-Add-in gefunden fuer: "
        + ", ".join(keywords)
    )


def _cast_translator(addin):
    """
    Castet ein generisches ApplicationAddIn auf TranslatorAddIn.
    Noetig, weil das generische Objekt (z.B. aus einer Iteration ueber
    app.ApplicationAddIns oder aus ItemById) keine SaveCopyAs-Methode hat -
    die existiert nur auf dem TranslatorAddIn-Interface. Gleiche Fehler-
    klasse wie beim PartDocument-Cast weiter oben.
    """
    translator = win32com.client.CastTo(addin, "TranslatorAddIn")
    if translator is None:
        raise RuntimeError(
            "Cast auf TranslatorAddIn fehlgeschlagen fuer Add-in "
            f"'{getattr(addin, 'DisplayName', '?')}'."
        )
    return translator


def _export_via_translator(app, doc, addin, file_path):
    """Gemeinsame Export-Logik fuer Translator-Add-ins (STEP/STL/DXF ...)."""
    context = app.TransientObjects.CreateTranslationContext()
    context.Type = _const.kFileBrowseIOMechanism
    options = app.TransientObjects.CreateNameValueMap()
    data_medium = app.TransientObjects.CreateDataMedium()
    data_medium.FileName = file_path
    addin.SaveCopyAs(doc, context, options, data_medium)


def _all_edges(app, comp_def):
    """
    Sammelt alle Kanten des ersten Volumenkoerpers in einer EdgeCollection.
    Genutzt von Fillet/Chamfer, da wir keine interaktive Kantenauswahl haben.
    """
    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError(
            "Kein Volumenkoerper vorhanden. Erst z. B. 'create_box' aufrufen."
        )
    body = comp_def.SurfaceBodies.Item(1)
    edge_coll = app.TransientObjects.CreateEdgeCollection()
    for edge in body.Edges:
        edge_coll.Add(edge)
    return edge_coll


def _filtered_edges(app, comp_def, which):
    """
    Liefert eine EdgeCollection, gefiltert nach Lage (statt interaktiver
    Auswahl). 'which' ist eines von:
        "all"      - alle Kanten
        "top"      - nur Kanten ganz oben (max Z)
        "bottom"   - nur Kanten ganz unten (min Z)
        "vertical" - nur senkrechte Kanten (Z aendert sich entlang der Kante)

    Filtert ueber die Z-Koordinaten der Kanten-Endpunkte. Gekruemmte Kanten
    ohne Endpunkte (z. B. volle Kreise) landen nur bei "all".
    """
    which = (which or "all").lower()
    if which == "all":
        return _all_edges(app, comp_def)

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError(
            "Kein Volumenkoerper vorhanden. Erst z. B. 'create_box' aufrufen."
        )
    body = comp_def.SurfaceBodies.Item(1)

    def _z_range(edge):
        """(z_start, z_stop) oder None, falls keine Endpunkte vorhanden."""
        try:
            z1 = edge.StartVertex.Point.Z
            z2 = edge.StopVertex.Point.Z
            return z1, z2
        except Exception:
            return None

    tol = 1e-4
    zs = []
    for edge in body.Edges:
        zr = _z_range(edge)
        if zr:
            zs.extend(zr)
    if not zs:
        raise RuntimeError(
            "Keine geraden Kanten mit Endpunkten gefunden - fuer diesen "
            "Koerper ist nur 'all' moeglich."
        )
    z_max, z_min = max(zs), min(zs)

    coll = app.TransientObjects.CreateEdgeCollection()
    for edge in body.Edges:
        zr = _z_range(edge)
        if zr is None:
            continue
        z1, z2 = zr
        if which == "top" and abs(z1 - z_max) < tol and abs(z2 - z_max) < tol:
            coll.Add(edge)
        elif which == "bottom" and abs(z1 - z_min) < tol and abs(z2 - z_min) < tol:
            coll.Add(edge)
        elif which == "vertical" and abs(z1 - z2) > tol:
            coll.Add(edge)

    if coll.Count == 0:
        raise RuntimeError(
            f"Keine Kanten fuer Filter '{which}' gefunden."
        )
    return coll


# ---------------------------------------------------------------------------
# Skizzen-Bestimmung: Bemassungen (DimensionConstraints) und geometrische
# Abhaengigkeiten (GeometricConstraints), damit Skizzen VOLL BESTIMMT sind.
# ---------------------------------------------------------------------------
def _project_origin(sketch, comp_def):
    """
    Projiziert den Modell-Ursprung (WorkPoints.Item(1)) in die Skizze und
    liefert den zugehoerigen SketchPoint.

    WICHTIG: Wir nutzen NICHT den Rueckgabewert von AddByProjectingEntity
    direkt (der ist ein generisches COM-Objekt und mehrfacher Cast-Versuch
    darauf schlug fehl). Stattdessen rufen wir die Methode nur fuer ihren
    Seiteneffekt auf und holen den neu erzeugten Punkt danach aus der
    sketch.SketchPoints-Collection - die ist stark typisiert und liefert
    direkt einen echten SketchPoint, ganz ohne Cast. Gleiches Muster wie in
    den offiziellen Autodesk-Beispielen (Punkt wird ueber SketchPoints.Item
    nach dem Projizieren abgeholt, nicht ueber den Rueckgabewert).
    """
    origin_wp = comp_def.WorkPoints.Item(1)
    count_before = sketch.SketchPoints.Count
    sketch.AddByProjectingEntity(origin_wp)
    count_after = sketch.SketchPoints.Count

    if count_after > count_before:
        # Neuer Punkt wurde angehaengt - das ist unser Ursprungspunkt.
        return sketch.SketchPoints.Item(count_after)

    # Fallback: nach ReferencedEntity suchen (z. B. falls der Punkt schon
    # vorher projiziert war und kein neuer entstand).
    for sp in sketch.SketchPoints:
        try:
            if sp.ReferencedEntity is not None:
                return sp
        except Exception:
            continue

    raise RuntimeError("Projizierter Ursprungspunkt nicht gefunden.")


def _as_sketch_line(entity):
    """
    Castet ein generisches SketchEntity (z.B. aus einer
    SketchEntitiesEnumerator wie dem Rueckgabewert von
    AddAsTwoPointRectangle) auf SketchLine. Noetig, weil das generische
    Objekt keine StartSketchPoint/EndSketchPoint-Eigenschaften hat - gleiche
    Fehlerklasse wie beim PartDocument- und TranslatorAddIn-Cast.
    """
    line = win32com.client.CastTo(entity, "SketchLine")
    if line is None:
        raise RuntimeError("Cast auf SketchLine fehlgeschlagen.")
    return line


def _nearest_sketch_point(rect_lines, x_cm, y_cm, tol=1e-6):
    """Sucht unter den Eckpunkten der Rechteck-Linien den Punkt, der (x,y)
    am naechsten liegt."""
    best, best_d = None, None
    for i in range(1, rect_lines.Count + 1):
        line = _as_sketch_line(rect_lines.Item(i))
        for sp in (line.StartSketchPoint, line.EndSketchPoint):
            g = sp.Geometry
            d = (g.X - x_cm) ** 2 + (g.Y - y_cm) ** 2
            if best_d is None or d < best_d:
                best_d, best = d, sp
    return best


def _points_already_merged(p1, p2, tol=1e-6):
    """
    Prueft, ob zwei SketchPoints geometrisch identisch sind. Inventor
    verschmilzt einen projizierten Ursprungspunkt automatisch mit einem
    bereits vorhandenen Skizzenpunkt an derselben Stelle - dann sind beide
    de facto derselbe Punkt, und eine explizite Koinzidenz-Bedingung
    zwischen ihnen ist ungueltig (E_INVALIDARG) und unnoetig.
    """
    try:
        return (
            abs(p1.Geometry.X - p2.Geometry.X) < tol
            and abs(p1.Geometry.Y - p2.Geometry.Y) < tol
        )
    except Exception:
        return False


def _try_step(beschreibung, fn):
    """
    Fuehrt einen einzelnen Constrain-Schritt aus und haengt bei einem Fehler
    die Beschreibung an, damit wir in der Rueckmeldung sehen, welcher
    Teilschritt (Ursprung projizieren, Bemassung X, Bemassung Y, ...)
    fehlgeschlagen ist - statt nur eines nichtssagenden COM-Fehlercodes.
    """
    try:
        return fn()
    except Exception as exc:
        raise RuntimeError(f"[{beschreibung}] {exc}") from exc


def _constrain_rectangle(app, sketch, comp_def, rect_lines,
                         x_cm, y_cm, w_cm, h_cm):
    """
    Versucht, ein Rechteck voll zu bestimmen: Lage der unteren linken Ecke
    (Koinzidenz zum Ursprung bei Offset 0, sonst Bemassung) plus Breite/Hoehe.
    Rueckgabe: "voll" oder "teilweise" (Best-Effort, Geometrie bleibt intakt).
    """
    tg = app.TransientGeometry
    geo = sketch.GeometricConstraints
    dim = sketch.DimensionConstraints

    origin_sp = _try_step(
        "Ursprung projizieren", lambda: _project_origin(sketch, comp_def)
    )
    corner = _try_step(
        "Eckpunkt suchen",
        lambda: _nearest_sketch_point(rect_lines, x_cm, y_cm),
    )
    at_origin = abs(x_cm) < 1e-6 and abs(y_cm) < 1e-6

    status = "voll"

    # --- Lage fixieren ---
    if at_origin:
        def _do_coincident():
            if _points_already_merged(corner, origin_sp):
                return  # Bereits durch Verschmelzung fixiert - nichts zu tun.
            try:
                geo.AddCoincident(corner, origin_sp)
            except Exception as exc:
                raise RuntimeError(
                    f"{exc!r} | corner_pytype={type(corner).__name__} "
                    f"origin_pytype={type(origin_sp).__name__}"
                ) from exc

        _try_step("Koinzidenz Ecke-Ursprung", _do_coincident)
    else:
        # Position ueber Bemassung vom Ursprung; Null-Offsets lassen 1 DOF
        # offen -> dann "teilweise".
        if abs(x_cm) > 1e-6:
            _try_step(
                "Bemassung X-Position",
                lambda: dim.AddTwoPointDistance(
                    origin_sp, corner, _const.kHorizontalDim,
                    tg.CreatePoint2d(x_cm / 2.0, y_cm - 0.5),
                ),
            )
        else:
            status = "teilweise"
        if abs(y_cm) > 1e-6:
            _try_step(
                "Bemassung Y-Position",
                lambda: dim.AddTwoPointDistance(
                    origin_sp, corner, _const.kVerticalDim,
                    tg.CreatePoint2d(x_cm - 0.5, y_cm / 2.0),
                ),
            )
        else:
            status = "teilweise"

    # --- Groesse bemassen (Breite + Hoehe) ---
    # Untere Kante (horizontal) und eine Seitenkante (vertikal) finden.
    horiz_line = vert_line = None
    for i in range(1, rect_lines.Count + 1):
        line = _as_sketch_line(rect_lines.Item(i))
        p1, p2 = line.StartSketchPoint.Geometry, line.EndSketchPoint.Geometry
        if abs(p1.Y - p2.Y) < 1e-6 and horiz_line is None:
            horiz_line = line
        elif abs(p1.X - p2.X) < 1e-6 and vert_line is None:
            vert_line = line

    if horiz_line is not None:
        _try_step(
            "Bemassung Breite",
            lambda: dim.AddTwoPointDistance(
                horiz_line.StartSketchPoint, horiz_line.EndSketchPoint,
                _const.kHorizontalDim,
                tg.CreatePoint2d(x_cm + w_cm / 2.0, y_cm - 1.0),
            ),
        )
    else:
        status = "teilweise"
    if vert_line is not None:
        _try_step(
            "Bemassung Hoehe",
            lambda: dim.AddTwoPointDistance(
                vert_line.StartSketchPoint, vert_line.EndSketchPoint,
                _const.kVerticalDim,
                tg.CreatePoint2d(x_cm - 1.0, y_cm + h_cm / 2.0),
            ),
        )
    else:
        status = "teilweise"

    return status


def _constrain_circle(app, sketch, comp_def, circle, cx_cm, cy_cm, diameter_cm):
    """
    Versucht, einen Kreis voll zu bestimmen: Mittelpunktlage + Durchmesser.
    Rueckgabe: "voll" oder "teilweise".
    """
    tg = app.TransientGeometry
    geo = sketch.GeometricConstraints
    dim = sketch.DimensionConstraints

    origin_sp = _try_step(
        "Ursprung projizieren", lambda: _project_origin(sketch, comp_def)
    )
    center = circle.CenterSketchPoint
    status = "voll"

    if abs(cx_cm) < 1e-6 and abs(cy_cm) < 1e-6:
        def _do_coincident_circle():
            if _points_already_merged(center, origin_sp):
                return
            geo.AddCoincident(center, origin_sp)

        _try_step("Koinzidenz Mittelpunkt-Ursprung", _do_coincident_circle)
    else:
        if abs(cx_cm) > 1e-6:
            _try_step(
                "Bemassung X-Position",
                lambda: dim.AddTwoPointDistance(
                    origin_sp, center, _const.kHorizontalDim,
                    tg.CreatePoint2d(cx_cm / 2.0, cy_cm - 0.5),
                ),
            )
        else:
            status = "teilweise"
        if abs(cy_cm) > 1e-6:
            _try_step(
                "Bemassung Y-Position",
                lambda: dim.AddTwoPointDistance(
                    origin_sp, center, _const.kVerticalDim,
                    tg.CreatePoint2d(cx_cm - 0.5, cy_cm / 2.0),
                ),
            )
        else:
            status = "teilweise"

    _try_step(
        "Bemassung Durchmesser",
        lambda: dim.AddDiameter(
            circle, tg.CreatePoint2d(cx_cm + diameter_cm, cy_cm)
        ),
    )
    return status


def _safe_constrain(fn):
    """
    Fuehrt eine Constrain-Funktion aus, ohne dass ein Fehler die Geometrie-
    Erzeugung stoppt. Rueckgabe: Statuswort fuer die Nutzer-Rueckmeldung.
    """
    try:
        return fn()
    except Exception as exc:  # bewusst breit: Bemassen ist Best-Effort
        return f"nicht bestimmt ({exc})"


# ---------------------------------------------------------------------------
# MCP-Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def ping() -> str:
    """Prueft die Verbindung zu Inventor und gibt die Version zurueck."""
    app = _get_app()
    return f"Verbunden mit Inventor {app.SoftwareVersion.DisplayName}"


@mcp.tool()
def create_part(name: str = "Bauteil1") -> str:
    """
    Erstellt ein neues, leeres Bauteil-Dokument (.ipt) und macht es aktiv.

    Args:
        name: Anzeigename des neuen Bauteils.
    """
    app = _get_app()
    doc_type = _const.kPartDocumentObject
    template = app.FileManager.GetTemplateFile(doc_type)
    doc = app.Documents.Add(doc_type, template, True)
    try:
        doc.DisplayName = name
    except Exception:
        pass  # DisplayName ist nicht immer setzbar vor dem Speichern.
    return f"Neues Bauteil '{name}' erstellt."


@mcp.tool()
def create_box(
    length_mm: float,
    width_mm: float,
    height_mm: float,
    direction: str = "positive",
) -> str:
    """
    Erstellt eine rechteckige Box durch Skizze auf der XY-Ebene + Extrusion.

    Args:
        length_mm: Laenge in X-Richtung (mm).
        width_mm:  Breite in Y-Richtung (mm).
        height_mm: Hoehe der Extrusion in Z-Richtung (mm).
        direction: Extrusionsrichtung: "positive" (Z+, Standard),
                   "negative" (Z-), "symmetric" (zentriert).
    """
    if min(length_mm, width_mm, height_mm) <= 0:
        raise ValueError("Alle Masse muessen groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    # Dokument explizit aktivieren - vermeidet Faelle, in denen ein anderes
    # Fenster/Dokument in Inventor gerade im Vordergrund ist.
    doc.Activate()

    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    length = _mm_to_cm(length_mm)
    width = _mm_to_cm(width_mm)
    height = _mm_to_cm(height_mm)

    def _step(beschreibung, fn):
        """Fuehrt einen einzelnen COM-Aufruf aus und gibt bei Fehlern
        genau an, in welchem Schritt es geknallt hat - statt eines
        nichtssagenden generischen COM-Fehlers."""
        try:
            return fn()
        except pythoncom.com_error as exc:
            raise RuntimeError(
                f"Fehler bei Schritt '{beschreibung}': {exc}"
            ) from exc

    # Skizze auf der XY-Ebene (WorkPlanes.Item(3) entspricht in den
    # offiziellen Autodesk-Beispielen durchgaengig der XY-Basisebene).
    sketch = _step(
        "Skizze anlegen",
        lambda: comp_def.Sketches.Add(comp_def.WorkPlanes.Item(3)),
    )

    # Rechteck ueber zwei Diagonalpunkte.
    p1 = tg.CreatePoint2d(0, 0)
    p2 = tg.CreatePoint2d(length, width)
    rect_lines = _step(
        "Rechteck zeichnen",
        lambda: sketch.SketchLines.AddAsTwoPointRectangle(p1, p2),
    )

    # Skizze voll bestimmen (Bemassung + Abhaengigkeiten).
    constrain_status = _safe_constrain(
        lambda: _constrain_rectangle(
            app, sketch, comp_def, rect_lines, 0, 0, length, width
        )
    )

    # Profil aus der geschlossenen Skizze.
    profile = _step("Profil erzeugen", lambda: sketch.Profiles.AddForSolid())

    # Extrusionsdefinition: neuer Koerper, Distanz = height.
    # _const liefert die ECHTEN Werte aus Inventors Typbibliothek statt
    # geratener Zahlen.
    ext_def = _step(
        "Extrusionsdefinition erzeugen",
        lambda: comp_def.Features.ExtrudeFeatures.CreateExtrudeDefinition(
            profile, _const.kNewBodyOperation
        ),
    )
    dir_enum = _parse_direction(direction)
    _step(
        "Extrusionsdistanz setzen",
        lambda: ext_def.SetDistanceExtent(height, dir_enum),
    )
    _step(
        "Extrusion ausfuehren",
        lambda: comp_def.Features.ExtrudeFeatures.Add(ext_def),
    )

    _rename_box_params(comp_def, length_mm, width_mm, height_mm)

    return (
        f"Box erstellt: {length_mm} x {width_mm} x {height_mm} mm "
        f"(L x B x H), Richtung '{direction}'. Skizze: {constrain_status} bestimmt."
    )


@mcp.tool()
def export_step(file_path: str) -> str:
    """
    Exportiert das aktive Bauteil als STEP-Datei.

    Args:
        file_path: Vollstaendiger Zielpfad, z. B. C:/temp/teil.step
    """
    app = _get_app()
    doc = _require_part_document(app)

    # Erst ueber den Namen suchen (robust), sonst per bekannter CLSID.
    try:
        step_addin = _get_translator(app, "step")
    except RuntimeError:
        raw_addin = app.ApplicationAddIns.ItemById(
            "{90AF7F40-0C01-11D5-8E83-0010B541CD80}"
        )
        step_addin = _cast_translator(raw_addin)
    _export_via_translator(app, doc, step_addin, file_path)
    return f"STEP-Export gespeichert: {file_path}"


@mcp.tool()
def get_properties() -> dict:
    """
    Liest zentrale iProperties (Design Tracking) des aktiven Dokuments aus.
    """
    app = _get_app()
    doc = app.ActiveDocument
    if doc is None:
        raise RuntimeError("Kein aktives Dokument geoeffnet.")

    # "Design Tracking Properties"
    prop_set = doc.PropertySets.Item("Design Tracking Properties")
    wanted = ("Part Number", "Description", "Material", "Mass")
    result = {}
    for name in wanted:
        try:
            result[name] = prop_set.Item(name).Value
        except Exception:
            result[name] = None
    result["DisplayName"] = doc.DisplayName
    return result


# ===========================================================================
# MODELLIERUNG
# ===========================================================================

def _parse_direction(direction: str):
    """Wandelt Richtungs-String in Inventor-ExtentDirection-Konstante um."""
    d = direction.lower().strip()
    if d in ("positive", "pos", "up"):
        return _const.kPositiveExtentDirection
    elif d in ("negative", "neg", "down"):
        return _const.kNegativeExtentDirection
    elif d in ("symmetric", "sym", "both"):
        return _const.kSymmetricExtentDirection
    raise ValueError(
        f"Unbekannte Richtung '{direction}'. Erlaubt: 'positive', 'negative', 'symmetric'."
    )


def _rename_cylinder_params(comp_def, diameter_mm, height_mm):
    """Benennt die Parameter des Zylinders in lesbare Namen um."""
    diameter_cm = _mm_to_cm(diameter_mm)
    height_cm = _mm_to_cm(height_mm)
    for p in comp_def.Parameters:
        try:
            val = p.Value
            name_lower = p.Name.lower()
            if abs(val - diameter_cm) < 0.01 and name_lower.startswith("d"):
                p.Name = "Durchmesser"
            elif abs(val - height_cm) < 0.01 and name_lower.startswith("d"):
                p.Name = "Hoehe"
        except Exception:
            continue


def _rename_box_params(comp_def, length_mm, width_mm, height_mm):
    """Benennt die Parameter der Box in lesbare Namen um."""
    length_cm = _mm_to_cm(length_mm)
    width_cm = _mm_to_cm(width_mm)
    height_cm = _mm_to_cm(height_mm)
    for p in comp_def.Parameters:
        try:
            val = p.Value
            name_lower = p.Name.lower()
            if abs(val - length_cm) < 0.01 and name_lower.startswith("d"):
                p.Name = "Laenge"
            elif abs(val - width_cm) < 0.01 and name_lower.startswith("d"):
                p.Name = "Breite"
            elif abs(val - height_cm) < 0.01 and name_lower.startswith("d"):
                p.Name = "Hoehe"
        except Exception:
            continue


def _rename_cut_params(comp_def, length_mm, width_mm, depth_mm):
    """Benennt die Parameter der Tasche in lesbare Namen um."""
    length_cm = _mm_to_cm(length_mm)
    width_cm = _mm_to_cm(width_mm)
    depth_cm = _mm_to_cm(depth_mm)
    for p in comp_def.Parameters:
        try:
            val = p.Value
            name_lower = p.Name.lower()
            if name_lower.startswith("d"):
                if abs(val - length_cm) < 0.01:
                    p.Name = "Tasche_Laenge"
                elif abs(val - width_cm) < 0.01:
                    p.Name = "Tasche_Breite"
                elif abs(val - depth_cm) < 0.01:
                    p.Name = "Tasche_Tiefe"
        except Exception:
            continue


@mcp.tool()
def create_cylinder(
    diameter_mm: float,
    height_mm: float,
    direction: str = "positive",
) -> str:
    """
    Erstellt einen Zylinder: Kreis-Skizze auf der XY-Ebene + Extrusion.

    Args:
        diameter_mm: Durchmesser (mm).
        height_mm:   Hoehe der Extrusion (mm).
        direction:   Extrusionsrichtung: "positive" (Z+, Standard),
                     "negative" (Z-), "symmetric" (zentriert).
    """
    if diameter_mm <= 0 or height_mm <= 0:
        raise ValueError("Durchmesser und Hoehe muessen groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    radius = _mm_to_cm(diameter_mm) / 2.0
    height = _mm_to_cm(height_mm)

    sketch = comp_def.Sketches.Add(comp_def.WorkPlanes.Item(3))
    center = tg.CreatePoint2d(0, 0)
    circle = sketch.SketchCircles.AddByCenterRadius(center, radius)
    constrain_status = _safe_constrain(
        lambda: _constrain_circle(
            app, sketch, comp_def, circle, 0, 0, radius * 2.0
        )
    )
    profile = sketch.Profiles.AddForSolid()

    ext_def = comp_def.Features.ExtrudeFeatures.CreateExtrudeDefinition(
        profile, _const.kNewBodyOperation
    )
    dir_enum = _parse_direction(direction)
    ext_def.SetDistanceExtent(height, dir_enum)
    comp_def.Features.ExtrudeFeatures.Add(ext_def)

    _rename_cylinder_params(comp_def, diameter_mm, height_mm)

    return (
        f"Zylinder erstellt: Durchmesser {diameter_mm} mm, Hoehe {height_mm} "
        f"mm, Richtung '{direction}'. Skizze: {constrain_status} bestimmt."
    )


@mcp.tool()
def extrude_cut(
    length_mm: float,
    width_mm: float,
    depth_mm: float,
    x_mm: float = 0.0,
    y_mm: float = 0.0,
    plane: str = "XY",
    start_offset_mm: float = 0.0,
    direction: str = "auto",
) -> str:
    """
    Schneidet eine rechteckige Tasche aus dem bestehenden Koerper (Material
    abtragen). Skizze auf einer beliebigen Ebene mit optionalem Offset.

    Args:
        length_mm:      Laenge des Rechtecks in X (mm).
        width_mm:       Breite des Rechtecks in Y (mm).
        depth_mm:       Schnitttiefe (mm).
        x_mm:           X-Position der linken unteren Ecke (mm, Standard 0).
        y_mm:           Y-Position der linken unteren Ecke (mm, Standard 0).
        plane:          Skizzebene: "XY" (Standard), "XZ", "YZ",
                        oder "XZ:30" (Ebene mit Offset in mm).
        start_offset_mm: Start-Offset der Extrusion von der Skizzebene (mm).
                         Positive Werte = in Extrusionsrichtung, negative = zurueck.
        direction:      "auto" (Standard) = Richtung aus Koerper ableiten,
                        "positive", "negative", "symmetric".
    """
    if min(length_mm, width_mm, depth_mm) <= 0:
        raise ValueError("Laenge, Breite und Tiefe muessen groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError(
            "Kein Koerper zum Schneiden vorhanden. Erst z. B. 'create_box'."
        )

    x = _mm_to_cm(x_mm)
    y = _mm_to_cm(y_mm)
    length = _mm_to_cm(length_mm)
    width = _mm_to_cm(width_mm)
    depth = _mm_to_cm(depth_mm)
    start_offset = _mm_to_cm(start_offset_mm)

    # Skizzebene bestimmen.
    work_plane = _resolve_work_plane(comp_def, plane)

    sketch = comp_def.Sketches.Add(work_plane)
    p1 = tg.CreatePoint2d(x, y)
    p2 = tg.CreatePoint2d(x + length, y + width)
    rect_lines = sketch.SketchLines.AddAsTwoPointRectangle(p1, p2)
    constrain_status = _safe_constrain(
        lambda: _constrain_rectangle(
            app, sketch, comp_def, rect_lines, x, y, length, width
        )
    )
    profile = sketch.Profiles.AddForSolid()

    ext_def = comp_def.Features.ExtrudeFeatures.CreateExtrudeDefinition(
        profile, _const.kCutOperation
    )

    # Richtung bestimmen.
    if direction.lower() == "auto":
        cut_dir = _detect_cut_direction(comp_def)
    else:
        cut_dir = _parse_direction(direction)

    # Offset + Tiefe setzen.
    if start_offset != 0:
        ext_def.SetDistanceExtentStart(start_offset)
    ext_def.SetDistanceExtent(depth, cut_dir)

    comp_def.Features.ExtrudeFeatures.Add(ext_def)

    _rename_cut_params(comp_def, length_mm, width_mm, depth_mm)

    return (
        f"Tasche geschnitten: {length_mm} x {width_mm} mm, "
        f"Tiefe {depth_mm} mm, Ebene '{plane}', "
        f"Offset {start_offset_mm} mm, Position ({x_mm}, {y_mm}). "
        f"Skizze: {constrain_status} bestimmt."
    )


@mcp.tool()
def add_hole(
    x_mm: float,
    y_mm: float,
    diameter_mm: float,
    depth_mm: float = 0.0,
) -> str:
    """
    Fuegt eine Bohrung an Position (x, y) auf der XY-Ebene hinzu.

    Args:
        x_mm:        X-Position des Bohrungszentrums (mm).
        y_mm:        Y-Position des Bohrungszentrums (mm).
        diameter_mm: Bohrungsdurchmesser (mm).
        depth_mm:    Bohrtiefe (mm). 0 = Durchgangsbohrung (durch alles).
    """
    if diameter_mm <= 0:
        raise ValueError("Durchmesser muss groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    # Skizze mit einem Mittelpunkt fuer die Bohrung.
    sketch = comp_def.Sketches.Add(comp_def.WorkPlanes.Item(3))
    hole_centers = app.TransientObjects.CreateObjectCollection()
    cx, cy = _mm_to_cm(x_mm), _mm_to_cm(y_mm)
    pt = sketch.SketchPoints.Add(tg.CreatePoint2d(cx, cy))
    hole_centers.Add(pt)

    # Lage des Bohrungsmittelpunkts bestimmen (Bemassung/Koinzidenz).
    def _constrain_point():
        geo = sketch.GeometricConstraints
        dim = sketch.DimensionConstraints
        origin_sp = _try_step(
            "Ursprung projizieren", lambda: _project_origin(sketch, comp_def)
        )
        status = "voll"
        if abs(cx) < 1e-6 and abs(cy) < 1e-6:
            def _do_coincident_hole():
                if _points_already_merged(pt, origin_sp):
                    return
                geo.AddCoincident(pt, origin_sp)

            _try_step("Koinzidenz Mittelpunkt-Ursprung", _do_coincident_hole)
        else:
            if abs(cx) > 1e-6:
                _try_step(
                    "Bemassung X-Position",
                    lambda: dim.AddTwoPointDistance(
                        origin_sp, pt, _const.kHorizontalDim,
                        tg.CreatePoint2d(cx / 2.0, cy - 0.5),
                    ),
                )
            else:
                status = "teilweise"
            if abs(cy) > 1e-6:
                _try_step(
                    "Bemassung Y-Position",
                    lambda: dim.AddTwoPointDistance(
                        origin_sp, pt, _const.kVerticalDim,
                        tg.CreatePoint2d(cx - 0.5, cy / 2.0),
                    ),
                )
            else:
                status = "teilweise"
        return status

    constrain_status = _safe_constrain(_constrain_point)

    diameter = _mm_to_cm(diameter_mm)
    hole_features = comp_def.Features.HoleFeatures
    hole_dir = _detect_cut_direction(comp_def)

    if depth_mm <= 0:
        # Durchgangsbohrung.
        hole_features.AddDrilledByThroughAllExtent(
            hole_centers, diameter, hole_dir
        )
        art = "Durchgangsbohrung"
    else:
        hole_features.AddDrilledByDistanceExtent(
            hole_centers,
            diameter,
            _mm_to_cm(depth_mm),
            hole_dir,
        )
        art = f"Bohrung (Tiefe {depth_mm} mm)"

    return (
        f"{art} erstellt: Durchmesser {diameter_mm} mm bei ({x_mm}, {y_mm}). "
        f"Skizze: {constrain_status} bestimmt."
    )


@mcp.tool()
def add_counterbore(
    x_mm: float,
    y_mm: float,
    diameter_mm: float,
    cbore_diameter_mm: float,
    cbore_depth_mm: float,
    depth_mm: float = 0.0,
) -> str:
    """
    Fuegt eine Gegenbohrung (Counterbore) an Position (x, y) auf der
    XY-Ebene hinzu.

    Args:
        x_mm:             X-Position des Bohrungszentrums (mm).
        y_mm:             Y-Position des Bohrungszentrums (mm).
        diameter_mm:      Durchmesser der Bohrung (mm).
        cbore_diameter_mm: Durchmesser der Gegenbohrung (mm).
        cbore_depth_mm:   Tiefe der Gegenbohrung (mm).
        depth_mm:         Gesamt-Tiefe (mm). 0 = Durchgang.
    """
    if min(diameter_mm, cbore_diameter_mm, cbore_depth_mm) <= 0:
        raise ValueError("Durchmesser und Tiefen muessen groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    sketch = comp_def.Sketches.Add(comp_def.WorkPlanes.Item(3))
    hole_centers = app.TransientObjects.CreateObjectCollection()
    cx, cy = _mm_to_cm(x_mm), _mm_to_cm(y_mm)
    pt = sketch.SketchPoints.Add(tg.CreatePoint2d(cx, cy))
    hole_centers.Add(pt)

    constrain_status = _safe_constrain(
        lambda: _constrain_hole_point(sketch, comp_def, pt, cx, cy, tg)
    )

    diameter = _mm_to_cm(diameter_mm)
    cbore_dia = _mm_to_cm(cbore_diameter_mm)
    cbore_depth = _mm_to_cm(cbore_depth_mm)
    hole_dir = _detect_cut_direction(comp_def)

    if depth_mm <= 0:
        comp_def.Features.HoleFeatures.AddCBoreByThroughAllExtent(
            hole_centers, diameter, cbore_dia, cbore_depth
        )
        art = "Gegenbohrung (Durchgang)"
    else:
        comp_def.Features.HoleFeatures.AddCBoreByDistanceExtent2(
            hole_centers, diameter, _mm_to_cm(depth_mm),
            hole_dir, cbore_dia, cbore_depth,
        )
        art = f"Gegenbohrung (Tiefe {depth_mm} mm)"

    return (
        f"{art}: D {diameter_mm} mm, Gegenbohrung D {cbore_diameter_mm} x "
        f"{cbore_depth_mm} mm bei ({x_mm}, {y_mm}). "
        f"Skizze: {constrain_status} bestimmt."
    )


@mcp.tool()
def add_countersink(
    x_mm: float,
    y_mm: float,
    diameter_mm: float,
    csink_diameter_mm: float,
    csink_angle_deg: float = 90.0,
    depth_mm: float = 0.0,
) -> str:
    """
    Fuegt eine Senkkopfbohrung (Countersink) an Position (x, y) auf der
    XY-Ebene hinzu.

    Args:
        x_mm:              X-Position des Bohrungszentrums (mm).
        y_mm:              Y-Position des Bohrungszentrums (mm).
        diameter_mm:       Durchmesser der Bohrung (mm).
        csink_diameter_mm: Durchmesser der Senkung (mm).
        csink_angle_deg:   Senkwinkel in Grad (Standard 90).
        depth_mm:          Gesamt-Tiefe (mm). 0 = Durchgang.
    """
    if min(diameter_mm, csink_diameter_mm) <= 0:
        raise ValueError("Durchmesser muessen groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    sketch = comp_def.Sketches.Add(comp_def.WorkPlanes.Item(3))
    hole_centers = app.TransientObjects.CreateObjectCollection()
    cx, cy = _mm_to_cm(x_mm), _mm_to_cm(y_mm)
    pt = sketch.SketchPoints.Add(tg.CreatePoint2d(cx, cy))
    hole_centers.Add(pt)

    constrain_status = _safe_constrain(
        lambda: _constrain_hole_point(sketch, comp_def, pt, cx, cy, tg)
    )

    diameter = _mm_to_cm(diameter_mm)
    csink_dia = _mm_to_cm(csink_diameter_mm)
    csink_angle_rad = math.radians(csink_angle_deg)
    hole_dir = _detect_cut_direction(comp_def)

    if depth_mm <= 0:
        comp_def.Features.HoleFeatures.AddCSinkByThroughAllExtent(
            hole_centers, diameter, csink_dia, csink_angle_rad,
        )
        art = "Senkkopfbohrung (Durchgang)"
    else:
        comp_def.Features.HoleFeatures.AddCSinkByDistanceExtent2(
            hole_centers, diameter, _mm_to_cm(depth_mm),
            hole_dir, csink_dia, csink_angle_rad,
        )
        art = f"Senkkopfbohrung (Tiefe {depth_mm} mm)"

    return (
        f"{art}: D {diameter_mm} mm, Senkung D {csink_diameter_mm} mm "
        f"({csink_angle_deg} Grad) bei ({x_mm}, {y_mm}). "
        f"Skizze: {constrain_status} bestimmt."
    )


def _constrain_hole_point(sketch, comp_def, pt, cx_cm, cy_cm, tg):
    """Hilfsfunktion: Bemassung eines Bohrungsmittelpunkts."""
    geo = sketch.GeometricConstraints
    dim = sketch.DimensionConstraints
    origin_sp = _project_origin(sketch, comp_def)
    status = "voll"
    if abs(cx_cm) < 1e-6 and abs(cy_cm) < 1e-6:
        if not _points_already_merged(pt, origin_sp):
            geo.AddCoincident(pt, origin_sp)
    else:
        if abs(cx_cm) > 1e-6:
            dim.AddTwoPointDistance(
                origin_sp, pt, _const.kHorizontalDim,
                tg.CreatePoint2d(cx_cm / 2.0, cy_cm - 0.5),
            )
        else:
            status = "teilweise"
        if abs(cy_cm) > 1e-6:
            dim.AddTwoPointDistance(
                origin_sp, pt, _const.kVerticalDim,
                tg.CreatePoint2d(cx_cm - 0.5, cy_cm / 2.0),
            )
        else:
            status = "teilweise"
    return status
    """
    Verrundet Kanten des ersten Volumenkoerpers mit gegebenem Radius.

    Args:
        radius_mm: Verrundungsradius (mm).
        edges:     Welche Kanten: "all" (Standard), "top", "bottom" oder
                   "vertical".
    """
    if radius_mm <= 0:
        raise ValueError("Radius muss groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    edge_coll = _filtered_edges(app, comp_def, edges)
    comp_def.Features.FilletFeatures.AddSimple(edge_coll, _mm_to_cm(radius_mm))
    return (
        f"Verrundung ({edges}) angewendet: Radius {radius_mm} mm "
        f"auf {edge_coll.Count} Kante(n)."
    )


@mcp.tool()
def add_chamfer(distance_mm: float, edges: str = "all") -> str:
    """
    Fast Kanten des ersten Volumenkoerpers mit gegebener Distanz.

    Args:
        distance_mm: Fasenbreite (mm).
        edges:       Welche Kanten: "all" (Standard), "top", "bottom" oder
                     "vertical".
    """
    if distance_mm <= 0:
        raise ValueError("Distanz muss groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    edge_coll = _filtered_edges(app, comp_def, edges)
    comp_def.Features.ChamferFeatures.AddUsingDistance(
        edge_coll, _mm_to_cm(distance_mm), True
    )
    return (
        f"Fase ({edges}) angewendet: Distanz {distance_mm} mm "
        f"auf {edge_coll.Count} Kante(n)."
    )


# ===========================================================================
# SHELL, DRAFT, MIRROR
# ===========================================================================
@mcp.tool()
def shell(wall_thickness_mm: float, remove_faces: str = "") -> str:
    """
    Schoepft den Koerper (aushoehlen) mit gleichmaessiger Wandstaerke.

    Args:
        wall_thickness_mm: Wandstaerke in mm.
        remove_faces:      Kommagetrennte Indizes von Flaechen, die entfernt
                           werden sollen ( offen lassen = Hohlkoerper ).
                           Beispiel: "1,3" entfernt Flaeche 1 und 3.
    """
    if wall_thickness_mm <= 0:
        raise ValueError("Wandstaerke muss groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError("Kein Volumenkoerper vorhanden.")

    thickness = _mm_to_cm(wall_thickness_mm)

    # Optionale Faces zum Entfernen.
    face_coll = None
    if remove_faces.strip():
        face_coll = app.TransientObjects.CreateFaceCollection()
        body = comp_def.SurfaceBodies.Item(1)
        for idx_str in remove_faces.split(","):
            idx = int(idx_str.strip())
            if idx < 1 or idx > body.Faces.Count:
                raise ValueError(f"Flaechen-Index {idx} ungueltig.")
            face_coll.Add(body.Faces.Item(idx))

    shell_def = comp_def.Features.ShellFeatures.CreateShellDefinition(
        face_coll, thickness
    )
    comp_def.Features.ShellFeatures.Add(shell_def)

    return (
        f"Shell erstellt: Wandstaerke {wall_thickness_mm} mm"
        + (f", {len(remove_faces.split(','))} Flaeche(n) entfernt." if remove_faces.strip() else " (Hohlkoerper).")
    )


@mcp.tool()
def draft(
    face_indices: str,
    angle_deg: float,
    pull_direction: str = "Z",
    fixed_edge_index: int = 0,
) -> str:
    """
    Setzt Verjuengung (Draft) auf Flaechen fuer Formteile/Guss.

    Args:
        face_indices:    Kommagetrennte Indizes der zu verjuengenden Flaechen.
                         Beispiel: "2,4,6"
        angle_deg:       Verjuengungswinkel in Grad.
        pull_direction:  Zugrichtung: "X", "Y" oder "Z" (Standard "Z").
                         Oder ein Arbeitsflaechen-Name wie "Work Plane1".
        fixed_edge_index: Index der festen Kante (0 = keine, wird ignoriert).
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError("Kein Volumenkoerper vorhanden.")

    body = comp_def.SurfaceBodies.Item(1)

    # Zu verjuengende Flaechen sammeln.
    face_coll = app.TransientObjects.CreateFaceCollection()
    for idx_str in face_indices.split(","):
        idx = int(idx_str.strip())
        if idx < 1 or idx > body.Faces.Count:
            raise ValueError(f"Flaechen-Index {idx} ungueltig.")
        face_coll.Add(body.Faces.Item(idx))

    # Zugrichtung bestimmen.
    pd = pull_direction.strip().upper()
    if pd in ("X", "Y", "Z"):
        axis_map = {"X": 1, "Y": 2, "Z": 3}
        pull_obj = comp_def.WorkAxes.Item(axis_map[pd])
    else:
        try:
            pull_obj = comp_def.WorkPlanes.Item(pull_direction.strip())
        except Exception:
            raise ValueError(f"Zugrichtung '{pull_direction}' nicht erkannt.")

    # Draft-Definition erstellen.
    draft_def = comp_def.Features.FaceDraftFeatures.CreateFaceDraftDefinition()
    draft_def.SetFixedPlane(face_coll, pull_obj, math.radians(angle_deg))

    comp_def.Features.FaceDraftFeatures.Add(draft_def)

    return (
        f"Draft erstellt: {len(face_indices.split(','))} Flaeche(n), "
        f"Winkel {angle_deg} Grad, Zugrichtung '{pull_direction}'."
    )


@mcp.tool()
def mirror_feature(feature_name: str, plane: str = "XY") -> str:
    """
    Spiegelt ein Feature ueber eine Arbeitsflaeche.

    Args:
        feature_name: Name des Features (z. B. "Extrusion1").
        plane:        Spiegelebene: "XY", "YZ", "XZ" oder Name wie "Work Plane1".
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    feat, typ = _find_feature(comp_def, feature_name)
    if feat is None:
        raise RuntimeError(
            f"Feature '{feature_name}' nicht gefunden. "
            "Namen mit 'list_features' pruefen."
        )

    # Spiegelebene bestimmen.
    pl = plane.strip().upper()
    if pl in ("XY", "YZ", "XZ"):
        plane_map = {"XY": 3, "YZ": 1, "XZ": 2}
        mirror_plane = comp_def.WorkPlanes.Item(plane_map[pl])
    else:
        try:
            mirror_plane = comp_def.WorkPlanes.Item(plane.strip())
        except Exception:
            raise ValueError(f"Spiegelebene '{plane}' nicht gefunden.")

    # Feature-Collection fuer Spiegelung.
    feat_coll = app.TransientObjects.CreateObjectCollection()
    feat_coll.Add(feat)

    mirror_def = comp_def.Features.MirrorFeatures.CreateDefinition(
        feat_coll, mirror_plane
    )
    comp_def.Features.MirrorFeatures.AddByDefinition(mirror_def)

    return (
        f"Feature '{feature_name}' ({typ}) ueber '{plane}' gespiegelt."
    )


@mcp.tool()
def mirror_body(plane: str = "XY") -> str:
    """
    Spiegelt den gesamten Volumenkoerper ueber eine Arbeitsflaeche.
    Erzeugt einen neuen Koerper (kNewBodyOperation).

    Args:
        plane: Spiegelebene: "XY", "YZ", "XZ" oder Name wie "Work Plane1".
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError("Kein Volumenkoerper vorhanden.")

    # Spiegelebene bestimmen.
    pl = plane.strip().upper()
    if pl in ("XY", "YZ", "XZ"):
        plane_map = {"XY": 3, "YZ": 1, "XZ": 2}
        mirror_plane = comp_def.WorkPlanes.Item(plane_map[pl])
    else:
        try:
            mirror_plane = comp_def.WorkPlanes.Item(plane.strip())
        except Exception:
            raise ValueError(f"Spiegelebene '{plane}' nicht gefunden.")

    # Koerper in Collection fuer Spiegelung.
    body_coll = app.TransientObjects.CreateObjectCollection()
    body_coll.Add(comp_def.SurfaceBodies.Item(1))

    mirror_def = comp_def.Features.MirrorFeatures.CreateDefinition(
        body_coll, mirror_plane
    )
    comp_def.Features.MirrorFeatures.AddByDefinition(mirror_def)

    return f"Koerper ueber '{plane}' gespiegelt. Neuer Koerper erstellt."


# ===========================================================================
# PARAMETRIK
# ===========================================================================
@mcp.tool()
def get_parameters() -> list:
    """
    Listet alle Modell-Parameter des aktiven Bauteils mit Name, Ausdruck
    und Wert (in cm/rad, Inventor-intern) auf.
    """
    app = _get_app()
    doc = _require_part_document(app)
    comp_def = doc.ComponentDefinition

    params = []
    for p in comp_def.Parameters:
        try:
            params.append(
                {
                    "name": p.Name,
                    "expression": p.Expression,
                    "value": p.Value,
                    "unit": p.Units,
                }
            )
        except Exception:
            continue
    return params


@mcp.tool()
def set_parameter(name: str, expression: str) -> str:
    """
    Setzt einen benannten Parameter ueber einen Ausdruck und aktualisiert
    das Modell. Beispiel: set_parameter("d1", "50 mm").

    Args:
        name:       Parametername (siehe 'get_parameters').
        expression: Neuer Ausdruck inkl. Einheit, z. B. "50 mm" oder "30 deg".
    """
    app = _get_app()
    doc = _require_part_document(app)
    comp_def = doc.ComponentDefinition

    try:
        param = comp_def.Parameters.Item(name)
    except Exception:
        raise RuntimeError(
            f"Parameter '{name}' nicht gefunden. Verfuegbare Namen via "
            "'get_parameters' abrufen."
        )

    param.Expression = expression
    doc.Update()
    return f"Parameter '{name}' auf '{expression}' gesetzt und Modell aktualisiert."


# ===========================================================================
# DATEI & EXPORT
# ===========================================================================
@mcp.tool()
def save_document(file_path: str = "") -> str:
    """
    Speichert das aktive Dokument. Ohne Pfad wird 'Save' verwendet (nur wenn
    das Dokument bereits einen Speicherort hat), sonst 'SaveAs'.

    Args:
        file_path: Optionaler Zielpfad, z. B. C:/temp/teil.ipt
    """
    app = _get_app()
    doc = app.ActiveDocument
    if doc is None:
        raise RuntimeError("Kein aktives Dokument geoeffnet.")

    if file_path:
        # Zweiter Parameter False = normal speichern (nicht nur als Kopie).
        doc.SaveAs(file_path, False)
        return f"Dokument gespeichert unter: {file_path}"

    doc.Save()
    return "Dokument gespeichert."


@mcp.tool()
def open_document(file_path: str) -> str:
    """
    Oeffnet ein bestehendes Dokument (.ipt / .iam / .idw) und macht es aktiv.

    Args:
        file_path: Vollstaendiger Pfad zur Datei.
    """
    app = _get_app()
    doc = app.Documents.Open(file_path, True)
    try:
        display = doc.DisplayName
    except Exception:
        display = file_path
    return f"Dokument geoeffnet: {display}"


@mcp.tool()
def export_stl(file_path: str) -> str:
    """
    Exportiert das aktive Bauteil als STL-Datei (fuer 3D-Druck).

    Args:
        file_path: Vollstaendiger Zielpfad, z. B. C:/temp/teil.stl
    """
    app = _get_app()
    doc = _require_part_document(app)
    addin = _get_translator(app, "stl")
    _export_via_translator(app, doc, addin, file_path)
    return f"STL-Export gespeichert: {file_path}"


@mcp.tool()
def export_dxf(file_path: str) -> str:
    """
    Exportiert als DXF. Hinweis: DXF eignet sich vor allem fuer 2D bzw.
    Blech-Abwicklungen (Flat Pattern). Bei reinen 3D-Volumenteilen ohne
    Abwicklung ist das Ergebnis ggf. leer.

    Args:
        file_path: Vollstaendiger Zielpfad, z. B. C:/temp/teil.dxf
    """
    app = _get_app()
    doc = _require_part_document(app)
    addin = _get_translator(app, "dxf")
    _export_via_translator(app, doc, addin, file_path)
    return f"DXF-Export gespeichert: {file_path}"


# ===========================================================================
# AUSLESEN
# ===========================================================================
@mcp.tool()
def get_mass_properties() -> dict:
    """
    Liefert Masse (kg), Volumen (cm^3), Oberflaeche (cm^2) und Schwerpunkt
    (mm) des aktiven Bauteils.
    """
    app = _get_app()
    doc = _require_part_document(app)
    comp_def = doc.ComponentDefinition
    mp = comp_def.MassProperties

    com = mp.CenterOfMass  # Point in cm
    return {
        "mass_kg": mp.Mass,          # Inventor liefert Masse in kg
        "volume_cm3": mp.Volume,     # cm^3
        "area_cm2": mp.Area,         # cm^2
        "center_of_mass_mm": {
            "x": com.X * 10.0,
            "y": com.Y * 10.0,
            "z": com.Z * 10.0,
        },
    }


@mcp.tool()
def list_bodies() -> list:
    """
    Listet alle Volumenkoerper des aktiven Bauteils mit ID,
    Bounding-Box, Volumen und Oberflaeche auf.
    """
    app = _get_app()
    doc = _require_part_document(app)
    comp_def = doc.ComponentDefinition

    result = []
    for i in range(1, comp_def.SurfaceBodies.Count + 1):
        body = comp_def.SurfaceBodies.Item(i)
        info = {"index": i, "name": body.Name}
        try:
            info["id"] = body.Id
        except Exception:
            pass
        try:
            box = body.RangeBox
            info["bounding_box_mm"] = {
                "length_x": round((box.MaxPoint.X - box.MinPoint.X) * 10.0, 4),
                "width_y": round((box.MaxPoint.Y - box.MinPoint.Y) * 10.0, 4),
                "height_z": round((box.MaxPoint.Z - box.MinPoint.Z) * 10.0, 4),
            }
        except Exception:
            pass
        try:
            info["volume_cm3"] = round(body.Volume, 6)
        except Exception:
            pass
        try:
            info["surface_area_cm2"] = round(body.Area, 6)
        except Exception:
            pass
        try:
            info["face_count"] = body.Faces.Count
        except Exception:
            pass
        try:
            info["edge_count"] = body.Edges.Count
        except Exception:
            pass
        result.append(info)
    return result


@mcp.tool()
def get_bounding_box() -> dict:
    """
    Liefert die exakten Abmessungen des ersten Volumenkoerpers
    (Bounding-Box) in mm.
    """
    app = _get_app()
    doc = _require_part_document(app)
    comp_def = doc.ComponentDefinition

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError("Kein Volumenkoerper vorhanden.")

    body = comp_def.SurfaceBodies.Item(1)
    try:
        box = body.RangeBox
    except Exception as exc:
        raise RuntimeError(f"RangeBox nicht verfuegbar: {exc}")

    x_min, y_min, z_min = box.MinPoint.X, box.MinPoint.Y, box.MinPoint.Z
    x_max, y_max, z_max = box.MaxPoint.X, box.MaxPoint.Y, box.MaxPoint.Z

    return {
        "length_x_mm": round((x_max - x_min) * 10.0, 4),
        "width_y_mm": round((y_max - y_min) * 10.0, 4),
        "height_z_mm": round((z_max - z_min) * 10.0, 4),
        "min": {
            "x_mm": round(x_min * 10.0, 4),
            "y_mm": round(y_min * 10.0, 4),
            "z_mm": round(z_min * 10.0, 4),
        },
        "max": {
            "x_mm": round(x_max * 10.0, 4),
            "y_mm": round(y_max * 10.0, 4),
            "z_mm": round(z_max * 10.0, 4),
        },
    }


@mcp.tool()
def list_faces() -> list:
    """
    Listet alle Flaechen des ersten Volumenkoerpers mit Index, Typ
    (planar/cylindrical/conical/etc.) und Flaeche (cm^2) auf.
    """
    app = _get_app()
    doc = _require_part_document(app)
    comp_def = doc.ComponentDefinition

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError("Kein Volumenkoerper vorhanden.")

    # SurfaceType Enums aus Inventors Typbibliothek.
    _surface_types = {
        5890: "cylinder",
        5891: "plane",
        5892: "cone",
        5893: "sphere",
        5894: "torus",
        5895: "surface",
        5896: "nurbs",
    }

    body = comp_def.SurfaceBodies.Item(1)
    result = []
    for i in range(1, body.Faces.Count + 1):
        face = body.Faces.Item(i)
        info = {"index": i}
        try:
            st = face.SurfaceType
            info["surface_type"] = _surface_types.get(st, f"unknown({st})")
        except Exception:
            info["surface_type"] = "unknown"
        try:
            info["area_cm2"] = round(face.Area, 6)
        except Exception:
            pass
        try:
            info["edge_count"] = face.Edges.Count
        except Exception:
            pass
        result.append(info)
    return result


@mcp.tool()
def list_features() -> list:
    """
    Listet die vorhandenen Features des aktiven Bauteils nach Typ auf
    (die Typen, die dieser Server erzeugen kann).
    """
    app = _get_app()
    doc = _require_part_document(app)
    feats = doc.ComponentDefinition.Features

    collections = {
        "Extrude": "ExtrudeFeatures",
        "Hole": "HoleFeatures",
        "Fillet": "FilletFeatures",
        "Chamfer": "ChamferFeatures",
        "Revolve": "RevolveFeatures",
        "Shell": "ShellFeatures",
        "Draft": "FaceDraftFeatures",
        "Mirror": "MirrorFeatures",
        "Sweep": "SweepFeatures",
        "Loft": "LoftFeatures",
        "Thread": "ThreadFeatures",
    }

    result = []
    for typ, attr in collections.items():
        try:
            coll = getattr(feats, attr)
            for i in range(1, coll.Count + 1):
                item = coll.Item(i)
                result.append({"type": typ, "name": item.Name})
        except Exception:
            continue
    return result


def _find_feature(comp_def, feature_name: str):
    """Sucht ein Feature nach Name in allen typisierten Sammlungen."""
    collections = {
        "Extrude": "ExtrudeFeatures",
        "Hole": "HoleFeatures",
        "Fillet": "FilletFeatures",
        "Chamfer": "ChamferFeatures",
        "Revolve": "RevolveFeatures",
        "Shell": "ShellFeatures",
        "Draft": "FaceDraftFeatures",
        "Mirror": "MirrorFeatures",
        "Sweep": "SweepFeatures",
        "Loft": "LoftFeatures",
        "Thread": "ThreadFeatures",
    }
    for typ, attr in collections.items():
        try:
            coll = getattr(comp_def.Features, attr)
            for i in range(1, coll.Count + 1):
                item = coll.Item(i)
                if item.Name == feature_name:
                    return item, typ
        except Exception:
            continue
    return None, None


# ===========================================================================
# FEATURE MANIPULATION (LOESCHEN, VERSCHIEBEN, SKIZZENE)
# ===========================================================================
@mcp.tool()
def delete_feature(feature_name: str) -> str:
    """
    Loescht ein Feature nach seinem Namen.

    Args:
        feature_name: Name des Features (z. B. "Extrusion1", "Hole1").
                      Namen ueber 'list_features' ermitteln.
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    feat, typ = _find_feature(comp_def, feature_name)
    if feat is None:
        raise RuntimeError(
            f"Feature '{feature_name}' nicht gefunden. "
            "Namen mit 'list_features' pruefen."
        )

    feat.Delete(False, False, False)
    return f"Feature '{feature_name}' (Typ: {typ}) geloescht."


@mcp.tool()
def move_feature(feature_name: str, direction: str = "up") -> str:
    """
    Verschiebt ein Feature in der Chronologie (Browser-Reihenfolge).
    Verschiebt den End-of-Part-Marker relativ zum Feature.

    Args:
        feature_name: Name des Features (z. B. "Extrusion1").
        direction:    "up" = Feature nach oben (vor die nachfolgenden),
                      "down" = Feature nach unten (nach den vorherigen).
    """
    if direction not in ("up", "down"):
        raise ValueError("direction muss 'up' oder 'down' sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    feat, typ = _find_feature(comp_def, feature_name)
    if feat is None:
        raise RuntimeError(
            f"Feature '{feature_name}' nicht gefunden. "
            "Namen mit 'list_features' pruefen."
        )

    try:
        feat.SetEndOfPart(direction == "up")
    except Exception as exc:
        raise RuntimeError(
            f"Verschieben von '{feature_name}' fehlgeschlagen: {exc}"
        )

    return (
        f"Feature '{feature_name}' (Typ: {typ}) "
        f"nach {direction} verschoben."
    )


@mcp.tool()
def change_sketch_plane(feature_name: str, plane: str) -> str:
    """
    Ändert die Skizzenebene eines skizzenbasierten Features
    (Extrude, Revolve) auf eine andere Arbeitsflaechen oder Flaechen.

    Args:
        feature_name: Name des Features (z. B. "Extrusion1").
        plane:        Zielebene. Erlaubt:
                      - "XY", "YZ", "XZ" fuer die drei Standard-Arbeitsflaechen
                      - "Work Plane1", "Work Plane2", ... fuer benannte Ebenen
                      - "Face1", "Face2", ... fuer Flaechen des Koerpers
                        (Nummer wie in der Inventor-Oberflaeche)
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    feat, typ = _find_feature(comp_def, feature_name)
    if feat is None:
        raise RuntimeError(
            f"Feature '{feature_name}' nicht gefunden. "
            "Namen mit 'list_features' pruefen."
        )

    if typ not in ("Extrude", "Revolve"):
        raise RuntimeError(
            f"Feature '{feature_name}' ist Typ '{typ}'. "
            "Nur Extrude und Revolve haben eine Skizzene."
        )

    # Skizze am Feature holen.
    try:
        sketch = feat.Sketch
    except Exception:
        raise RuntimeError(
            f"Kein Zugriff auf die Skizze von '{feature_name}'."
        )

    # Neue Ebene bestimmen.
    new_entity = None
    plane_upper = plane.strip().upper()

    if plane_upper in ("XY", "YZ", "XZ"):
        plane_map = {"XY": 3, "YZ": 1, "XZ": 2}
        idx = plane_map[plane_upper]
        new_entity = comp_def.WorkPlanes.Item(idx)
    elif plane_upper.startswith("WORK PLANE"):
        try:
            new_entity = comp_def.WorkPlanes.Item(plane.strip())
        except Exception:
            try:
                idx = int(plane_upper.replace("WORK PLANE", "").strip())
                new_entity = comp_def.WorkPlanes.Item(idx)
            except Exception:
                raise RuntimeError(
                    f"Arbeitsflaeche '{plane}' nicht gefunden."
                )
    elif plane_upper.startswith("FACE"):
        try:
            face_idx = int(plane_upper.replace("FACE", "").strip())
            new_entity = comp_def.SurfaceBodies.Item(1).Faces.Item(face_idx)
        except Exception:
            raise RuntimeError(f"Flaeche '{plane}' nicht gefunden.")
    else:
        raise ValueError(
            f"Ungueltiger Ebenen-Angabe '{plane}'. "
            "Erlaubt: 'XY'/'YZ'/'XZ', 'Work Plane<N>', 'Face<N>'."
        )

    try:
        sketch.PlanarEntity = new_entity
    except Exception as exc:
        raise RuntimeError(
            f"Umschalten der Skizenebene fehlgeschlagen: {exc}"
        )

    doc.Update()
    return (
        f"Skizenebene von '{feature_name}' auf '{plane}' geaendert."
    )


# ===========================================================================
# KANTENAUSWAHL AN FLAECHEN
# ===========================================================================
@mcp.tool()
def list_face_edges(face_index: int = 1) -> list:
    """
    Listet die Kanten einer Flaechen des ersten Volumenkoerpers auf.
    Liefert Index, Typ (gerade/kruemm), Z-Start, Z-Stop pro Kante.

    Args:
        face_index: Index der Flaeche (1-basiert, wie in Inventor).
                    Standard: 1 (erste Flaeche).
    """
    app = _get_app()
    doc = _require_part_document(app)
    comp_def = doc.ComponentDefinition

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError("Kein Volumenkoerper vorhanden.")

    body = comp_def.SurfaceBodies.Item(1)
    if face_index < 1 or face_index > body.Faces.Count:
        raise ValueError(
            f"Flaechen-Index {face_index} ungueltig. "
            f"Verfuegbare Indizes: 1 bis {body.Faces.Count}."
        )

    face = body.Faces.Item(face_index)
    result = []
    for i in range(1, face.Edges.Count + 1):
        edge = face.Edges.Item(i)
        info = {"index": i}
        try:
            geom_type = edge.GeometryType
            info["geometry_type"] = geom_type
        except Exception:
            pass
        try:
            z1 = edge.StartVertex.Point.Z
            z2 = edge.StopVertex.Point.Z
            info["z_start"] = round(z1, 6)
            info["z_stop"] = round(z2, 6)
            if abs(z1 - z2) < 1e-4:
                info["orientation"] = "horizontal"
            else:
                info["orientation"] = "vertical"
        except Exception:
            info["orientation"] = "curved"
        result.append(info)
    return result


def _get_fillet_feature(comp_def, feature_name: str):
    """Sucht ein Fillet-Feature und liefert (feat, typ)."""
    feat, typ = _find_feature(comp_def, feature_name)
    if feat is None:
        raise RuntimeError(
            f"Feature '{feature_name}' nicht gefunden. "
            "Namen mit 'list_features' pruefen."
        )
    if typ != "Fillet":
        raise RuntimeError(
            f"Feature '{feature_name}' ist Typ '{typ}', nicht 'Fillet'."
        )
    return feat


def _get_chamfer_feature(comp_def, feature_name: str):
    """Sucht ein Chamfer-Feature und liefert (feat, typ)."""
    feat, typ = _find_feature(comp_def, feature_name)
    if feat is None:
        raise RuntimeError(
            f"Feature '{feature_name}' nicht gefunden. "
            "Namen mit 'list_features' pruefen."
        )
    if typ != "Chamfer":
        raise RuntimeError(
            f"Feature '{feature_name}' ist Typ '{typ}', nicht 'Chamfer'."
        )
    return feat


def _build_edge_collection(app, comp_def, edge_specs: str):
    """
    Baut eine EdgeCollection aus einer Spezifikation.
    edge_specs: Kommagetrennte Liste von Angaben wie:
        "F1:E1,F1:E3"     -> Flaeche 1, Kante 1 und 3
        "top"              -> alle oberen Kanten (wie bei fillet/chamfer)
        "bottom"           -> alle unteren Kanten
        "vertical"         -> alle senkrechten Kanten
        "all"              -> alle Kanten
    """
    spec = edge_specs.strip().lower()

    if spec in ("all", "top", "bottom", "vertical"):
        return _filtered_edges(app, comp_def, spec), spec

    coll = app.TransientObjects.CreateEdgeCollection()
    body = comp_def.SurfaceBodies.Item(1)
    parts = [p.strip() for p in edge_specs.split(",") if p.strip()]

    for part in parts:
        if ":" not in part:
            raise ValueError(
                f"Ungueltiges Format '{part}'. Erwartet 'F<idx>:E<idx>' "
                "oder 'top'/'bottom'/'vertical'/'all'."
            )
        face_str, edge_str = part.split(":", 1)
        face_idx = int(face_str.upper().replace("F", ""))
        edge_idx = int(edge_str.upper().replace("E", ""))

        if face_idx < 1 or face_idx > body.Faces.Count:
            raise ValueError(f"Flaechen-Index {face_idx} ungueltig.")
        face = body.Faces.Item(face_idx)
        if edge_idx < 1 or edge_idx > face.Edges.Count:
            raise ValueError(
                f"Kanten-Index {edge_idx} auf Flaeche {face_idx} ungueltig."
            )
        coll.Add(face.Edges.Item(edge_idx))

    if coll.Count == 0:
        raise RuntimeError("Keine Kanten aus der Angabe extrahiert.")
    return coll, edge_specs


@mcp.tool()
def change_fillet_edges(feature_name: str, edges: str) -> str:
    """
    Ändert die Kanten-Auswahl eines bestehenden Fillet-Features.
    Das Feature wird geloescht und mit den neuen Kanten neu erstellt.
    Radius und andere Einstellungen bleiben erhalten.

    Args:
        feature_name: Name des Fillet-Features (z. B. "Fillet1").
        edges:        Neue Kanten-Angabe. Erlaubt:
                      - "all", "top", "bottom", "vertical"
                      - "F1:E1,F1:E3" (Flaeche 1, Kanten 1 und 3)
                      - Kombination: "F1:E1,top"
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    feat = _get_fillet_feature(comp_def, feature_name)

    # Alten Radius auslesen.
    try:
        radius_cm = feat.Parameters.Item(1).Value
        radius_mm = radius_cm * 10.0
    except Exception:
        radius_mm = None

    # Alte Einstellungen auslesen.
    try:
        auto_chain = feat.AutomaticEdgeChain
    except Exception:
        auto_chain = True
    try:
        roll_sharp = feat.RollAlongSharpEdges
    except Exception:
        roll_sharp = False

    # Neue Kanten bauen.
    new_edges, _ = _build_edge_collection(app, comp_def, edges)

    # Altes Feature loeschen.
    feat.Delete(False, False, False)

    # Neues Feature mit gleichen Einstellungen erstellen.
    if radius_mm is not None:
        comp_def.Features.FilletFeatures.AddSimple(
            new_edges,
            _mm_to_cm(radius_mm),
            False,
            False,
            auto_chain,
            roll_sharp,
        )
    else:
        comp_def.Features.FilletFeatures.AddSimple(new_edges, 0.1)

    return (
        f"Fillet '{feature_name}' neu erstellt: "
        f"{new_edges.Count} Kante(n), "
        f"Radius {radius_mm if radius_mm else '?'} mm."
    )


@mcp.tool()
def change_chamfer_edges(feature_name: str, edges: str) -> str:
    """
    Ändert die Kanten-Auswahl eines bestehenden Chamfer-Features.
    Das Feature wird geloescht und mit den neuen Kanten neu erstellt.
    Distanz und andere Einstellungen bleiben erhalten.

    Args:
        feature_name: Name des Chamfer-Features (z. B. "Chamfer1").
        edges:        Neue Kanten-Angabe (siehe 'change_fillet_edges').
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    feat = _get_chamfer_feature(comp_def, feature_name)

    # Alte Distanz auslesen.
    try:
        dist_cm = feat.Parameters.Item(1).Value
        dist_mm = dist_cm * 10.0
    except Exception:
        dist_mm = None

    # Alte Einstellungen auslesen.
    try:
        auto_chain = feat.AutomaticEdgeChain
    except Exception:
        auto_chain = True

    # Neue Kanten bauen.
    new_edges, _ = _build_edge_collection(app, comp_def, edges)

    # Altes Feature loeschen.
    feat.Delete(False, False, False)

    # Neues Feature mit gleichen Einstellungen erstellen.
    if dist_mm is not None:
        comp_def.Features.ChamferFeatures.AddUsingDistance(
            new_edges, _mm_to_cm(dist_mm), auto_chain
        )
    else:
        comp_def.Features.ChamferFeatures.AddUsingDistance(
            new_edges, 0.1, auto_chain
        )

    return (
        f"Chamfer '{feature_name}' neu erstellt: "
        f"{new_edges.Count} Kante(n), "
        f"Distanz {dist_mm if dist_mm else '?'} mm."
    )


# ===========================================================================
# ROTATIONSKOERPER (REVOLVE)
# ===========================================================================
@mcp.tool()
def create_revolve(
    outer_radius_mm: float,
    height_mm: float,
    inner_radius_mm: float = 0.0,
    angle_deg: float = 360.0,
) -> str:
    """
    Erstellt einen Rotationskoerper (Scheibe / Ring / Rohr) durch Rotation
    eines Rechteckprofils um die Y-Achse.

    Args:
        outer_radius_mm: Aussenradius (mm).
        height_mm:       Hoehe entlang der Y-Achse (mm).
        inner_radius_mm: Innenradius (mm). 0 = Vollkoerper, >0 = Rohr/Ring.
        angle_deg:       Rotationswinkel in Grad (360 = voller Koerper).
    """
    if outer_radius_mm <= 0 or height_mm <= 0:
        raise ValueError("Aussenradius und Hoehe muessen groesser als 0 sein.")
    if inner_radius_mm < 0 or inner_radius_mm >= outer_radius_mm:
        raise ValueError("Innenradius muss >=0 und < Aussenradius sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    r_out = _mm_to_cm(outer_radius_mm)
    r_in = _mm_to_cm(inner_radius_mm)
    height = _mm_to_cm(height_mm)

    # Rechteckprofil in der XY-Ebene, versetzt vom Ursprung entlang X.
    sketch = comp_def.Sketches.Add(comp_def.WorkPlanes.Item(3))
    p1 = tg.CreatePoint2d(r_in, 0)
    p2 = tg.CreatePoint2d(r_out, height)
    rect_lines = sketch.SketchLines.AddAsTwoPointRectangle(p1, p2)
    constrain_status = _safe_constrain(
        lambda: _constrain_rectangle(
            app, sketch, comp_def, rect_lines, r_in, 0, r_out - r_in, height
        )
    )
    profile = sketch.Profiles.AddForSolid()

    # Rotationsachse = Y-Achse (WorkAxes.Item(2)).
    axis = comp_def.WorkAxes.Item(2)
    rev_features = comp_def.Features.RevolveFeatures

    if angle_deg >= 360.0:
        rev_features.AddFull(profile, axis, _const.kNewBodyOperation)
        art = "Vollkoerper (360 grd)"
    else:
        angle_rad = math.radians(angle_deg)
        rev_features.AddByAngle(
            profile,
            axis,
            angle_rad,
            _const.kPositiveExtentDirection,
            _const.kNewBodyOperation,
        )
        art = f"Teilkoerper ({angle_deg} grd)"

    kind = "Rohr/Ring" if inner_radius_mm > 0 else "Scheibe/Zylinder"
    return (
        f"Rotationskoerper erstellt ({kind}, {art}). "
        f"Skizze: {constrain_status} bestimmt."
    )


@mcp.tool()
def create_revolved_profile(
    points: str,
    axis: str = "Y",
    angle_deg: float = 360.0,
    plane: str = "XY",
    operation: str = "new",
) -> str:
    """
    Erstellt einen Rotationskoerper aus einem frei definierbaren 2D-Profil.
    Das Profil wird als geschlossene Punktkette uebergeben und um eine
    Achse rotiert.

    Args:
        points:    2D-Punkte des Profils (in mm), semikolongetrennt.
                   Format: "x1,y1;x2,y2;x3,y3;..." (Koordinaten in der
                   Skizzebene, x = Abstand von Rotationsachse,
                   y = Hoehe entlang der Achse).
                   Das Profil muss geschlossen sein (erster und letzter
                   Punkte sollten übereinstimmen oder die Skizze wird
                   automatisch geschlossen).
        axis:      Rotationsachse: "X", "Y" oder "Z" (Standard "Y").
        angle_deg: Rotationswinkel in Grad (Standard 360 = voller Koerper).
        plane:     Skizzebene fuer das Profil: "XY", "XZ", "YZ" oder
                   Offset wie "XZ:30" (Standard "XY").
        operation: "new" = neuer Koerper, "join" = verbinden, "cut" = schneiden.
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    # Punkte parsen.
    raw_points = [p.strip() for p in points.split(";") if p.strip()]
    if len(raw_points) < 3:
        raise ValueError("Mindestens 3 Punkte noetig fuer ein Profil.")

    points_2d = []
    for pt_str in raw_points:
        parts = pt_str.split(",")
        if len(parts) != 2:
            raise ValueError(
                f"Ungueltiger Punkt: '{pt_str}'. Erwartet 'x,y' (in mm)."
            )
        x, y = float(parts[0]), float(parts[1])
        points_2d.append(tg.CreatePoint2d(_mm_to_cm(x), _mm_to_cm(y)))

    # Skizze anlegen.
    work_plane = _resolve_work_plane(comp_def, plane)
    sketch = comp_def.Sketches.Add(work_plane)

    # Profil zeichnen: Polylinie aus Linien (sequenziell, damit Endpunkte
    # automatisch verbunden werden).
    first_line = sketch.SketchLines.AddByTwoPoints(
        points_2d[0], points_2d[1]
    )
    prev_line = first_line
    for i in range(2, len(points_2d)):
        new_line = sketch.SketchLines.AddByTwoPoints(
            prev_line.EndSketchPoint, points_2d[i]
        )
        prev_line = new_line

    # Profil schliessen falls noetig.
    dx = abs(points_2d[0].X - points_2d[-1].X)
    dy = abs(points_2d[0].Y - points_2d[-1].Y)
    if dx > 1e-6 or dy > 1e-6:
        sketch.SketchLines.AddByTwoPoints(
            prev_line.EndSketchPoint, first_line.StartSketchPoint
        )

    profile = sketch.Profiles.AddForSolid()

    # Rotationsachse bestimmen.
    axis_map = {"X": 1, "Y": 2, "Z": 3}
    axis_upper = axis.strip().upper()
    if axis_upper not in axis_map:
        raise ValueError(f"Unbekannte Achse: '{axis}'. Erlaubt: X, Y, Z.")
    rot_axis = comp_def.WorkAxes.Item(axis_map[axis_upper])

    # Rotations-Operation.
    op_map = {
        "new": _const.kNewBodyOperation,
        "join": _const.kJoinOperation,
        "cut": _const.kCutOperation,
    }
    op = op_map.get(operation.lower(), _const.kNewBodyOperation)

    rev_features = comp_def.Features.RevolveFeatures
    if angle_deg >= 360.0:
        rev_features.AddFull(profile, rot_axis, op)
        art = "Vollkoerper (360 grd)"
    else:
        angle_rad = math.radians(angle_deg)
        rev_features.AddByAngle(
            profile, rot_axis, angle_rad,
            _const.kPositiveExtentDirection, op,
        )
        art = f"Teilkoerper ({angle_deg} grd)"

    return (
        f"Rotationskoerper aus freiem Profil erstellt ({art}). "
        f"{len(raw_points)} Punkte, Achse '{axis}', Ebene '{plane}', "
        f"Operation '{operation}'."
    )


# ===========================================================================
# SWEEP, LOFT, GEWINDE, NUT
# ===========================================================================
@mcp.tool()
def sweep(
    profile_diameter_mm: float,
    path_points: str,
    operation: str = "new",
) -> str:
    """
    Erzeugt einen Sweep: Kreisprofil entlang eines 3D-Pfads.

    Args:
        profile_diameter_mm: Durchmesser des Kreisprofils (mm).
        path_points:         Kommagetrennte 3D-Punkte des Pfads.
                             Format: "x1,y1,z1;x2,y2,z2;x3,y3,z3" (in mm).
                             Mindestens 2 Punkte.
        operation:           "new" = neuer Koerper, "join" = mit bestehendem
                             verbinden, "cut" = Material abtragen.
    """
    if profile_diameter_mm <= 0:
        raise ValueError("Durchmesser muss groesser als 0 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    # Pfad-Punkte parsen.
    raw_points = [p.strip() for p in path_points.split(";") if p.strip()]
    if len(raw_points) < 2:
        raise ValueError("Mindestens 2 Pfadpunkte noetig.")

    points_cm = []
    for pt_str in raw_points:
        parts = pt_str.split(",")
        if len(parts) != 3:
            raise ValueError(f"Ungueltiger Punkt: '{pt_str}'. Erwartet 'x,y,z'.")
        x, y, z = [_mm_to_cm(float(v)) for v in parts]
        points_cm.append(tg.CreatePoint(x, y, z))

    # 3D-Skizze fuer den Pfad.
    sketch3d = comp_def.Sketches3D.Add()
    wp_list = []
    for p in points_cm:
        wp = comp_def.WorkPoints.AddFixed(p)
        wp_list.append(wp)

    line = sketch3d.SketchLines3D.AddByTwoPoints(wp_list[0], wp_list[1], True)
    for i in range(2, len(wp_list)):
        line = sketch3d.SketchLines3D.AddByTwoPoints(
            line.EndPoint, wp_list[i], True
        )

    # Path aus der letzten Linie erstellen.
    path = comp_def.Features.CreatePath(line)

    # Profil-Skizze: Ebene senkrecht zum Pfad-Anfang.
    # WorkPlane durch den Startpunkt der Pfadlinie, senkrecht zur Linie.
    start_pt = points_cm[0]
    end_pt = points_cm[1]
    direction = tg.CreateVector(
        end_pt.X - start_pt.X,
        end_pt.Y - start_pt.Y,
        end_pt.Z - start_pt.Z,
    )

    # Profil auf der XY-Ebene, Kreis zentriert auf (0,0).
    # Die Skizze wird spaeter durch das Sweep-Features.AddUsingPath
    # automatisch auf den Pfad-Anfang projiziert.
    sketch2d = comp_def.Sketches.Add(comp_def.WorkPlanes.Item(3))
    radius = _mm_to_cm(profile_diameter_mm) / 2.0
    sketch2d.SketchCircles.AddByCenterRadius(tg.CreatePoint2d(0, 0), radius)
    profile = sketch2d.Profiles.AddForSolid()

    # Sweep-Operation.
    op_map = {
        "new": _const.kNewBodyOperation,
        "join": _const.kJoinOperation,
        "cut": _const.kCutOperation,
    }
    op = op_map.get(operation.lower(), _const.kNewBodyOperation)

    comp_def.Features.SweepFeatures.AddUsingPath(profile, path, op)

    return (
        f"Sweep erstellt: Profil D {profile_diameter_mm} mm, "
        f"{len(points_cm)} Pfadpunkte, Operation '{operation}'."
    )


@mcp.tool()
def loft(
    sections: str,
    operation: str = "new",
) -> str:
    """
    Erzeugt einen Loft (Uebergang) zwischen mehreren Profilen auf
    verschiedenen Ebenen.

    Args:
        sections:  Profil-Spezifikationen, semikolongetrennt.
                   Jedes Profil: "ebene:durchmesser_mm"
                   Ebenen: "XY", "XZ", "YZ" oder "höhe_mm" (Abstand von XY).
                   Beispiel: "XY:50;30:40;60:30" = Kreis D50 auf XY,
                             D40 in 30mm Hoehe, D30 in 60mm Hoehe.
        operation: "new" = neuer Koerper, "join" = verbinden, "cut" = schneiden.
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    section_parts = [s.strip() for s in sections.split(";") if s.strip()]
    if len(section_parts) < 2:
        raise ValueError("Mindestens 2 Profile noetig.")

    sections_coll = app.TransientObjects.CreateObjectCollection()

    for sec_str in section_parts:
        parts = sec_str.split(":")
        if len(parts) != 2:
            raise ValueError(
                f"Ungueltige Profil-Angabe: '{sec_str}'. "
                "Erwartet 'ebene:durchmesser_mm'."
            )
        plane_str, diam_str = parts[0].strip(), parts[1].strip()
        diameter_mm = float(diam_str)
        if diameter_mm <= 0:
            raise ValueError("Durchmesser muss > 0 sein.")

        # Ebene bestimmen.
        plane_upper = plane_str.upper()
        if plane_upper in ("XY", "XZ", "YZ"):
            plane_map = {"XY": 3, "XZ": 2, "YZ": 1}
            work_plane = comp_def.WorkPlanes.Item(plane_map[plane_upper])
        else:
            # Als Hoehe in mm interpretieren -> WorkPlane per Offset.
            height_mm = float(plane_str)
            height_cm = _mm_to_cm(height_mm)
            work_plane = comp_def.WorkPlanes.AddByPlaneAndOffset(
                comp_def.WorkPlanes.Item(3), height_cm,
            )

        # Skizze mit Kreis auf der Ebene.
        sketch = comp_def.Sketches.Add(work_plane)
        sketch.SketchCircles.AddByCenterRadius(
            tg.CreatePoint2d(0, 0), _mm_to_cm(diameter_mm) / 2.0
        )
        profile = sketch.Profiles.AddForSolid()
        sections_coll.Add(profile)

    op_map = {
        "new": _const.kNewBodyOperation,
        "join": _const.kJoinOperation,
        "cut": _const.kCutOperation,
    }
    op = op_map.get(operation.lower(), _const.kNewBodyOperation)

    loft_def = comp_def.Features.LoftFeatures.CreateLoftDefinition(
        sections_coll, op
    )
    comp_def.Features.LoftFeatures.Add(loft_def)

    return (
        f"Loft erstellt: {len(section_parts)} Profile, "
        f"Operation '{operation}'."
    )


@mcp.tool()
def add_thread(
    face_index: int = 0,
    thread_standard: str = "ISO Metric Profile",
    thread_size: str = "M10x1.5",
    thread_class: str = "6g",
    depth_mm: float = 0.0,
) -> str:
    """
    Fuegt ein Gewinde auf einer zylindrischen Flaeche hinzu.

    Args:
        face_index:     Index der zylindrischen Flaeche (0 = auto-erkennen,
                        die erste zylindrische Flaeche wird genommen).
        thread_standard: Standard (z.B. "ISO Metric Profile",
                        "ANSI Unified Screw Threads").
        thread_size:    Gewindegroesse (z.B. "M10x1.5", "M20x2.5").
        thread_class:   Klasse (z.B. "6g", "6H", "2A").
        depth_mm:       Gewindetiefe in mm. 0 = Volle Laenge.
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    if comp_def.SurfaceBodies.Count == 0:
        raise RuntimeError("Kein Volumenkoerper vorhanden.")

    body = comp_def.SurfaceBodies.Item(1)

    # Zylindrische Flaeche finden.
    target_face = None
    if face_index > 0:
        if face_index > body.Faces.Count:
            raise ValueError(f"Flaechen-Index {face_index} ungueltig.")
        target_face = body.Faces.Item(face_index)
    else:
        # Auto-erkennen: erste zylindrische Flaeche.
        for i in range(1, body.Faces.Count + 1):
            face = body.Faces.Item(i)
            try:
                if face.SurfaceType == 5890:  # CylinderSurfaceType
                    target_face = face
                    break
            except Exception:
                continue

    if target_face is None:
        raise RuntimeError(
            "Keine zylindrische Flaeche gefunden. "
            "face_index angeben oder Zylinder erstellen."
        )

    # Startkante finden (Kante der Flaeche) - unterste Kante bevorzugen.
    start_edge = None
    for i in range(1, target_face.Edges.Count + 1):
        e = target_face.Edges.Item(i)
        try:
            z_val = e.StartVertex.Point.Z
            if start_edge is None or z_val < start_edge.StartVertex.Point.Z:
                start_edge = e
        except Exception:
            continue
    if start_edge is None:
        start_edge = target_face.Edges.Item(1)

    # ThreadInfo erstellen.
    try:
        thread_info = comp_def.Features.ThreadFeatures.CreateStandardThreadInfo(
            False, True, thread_standard, thread_size, thread_class
        )
    except Exception as exc:
        raise RuntimeError(
            f"CreateStandardThreadInfo fehlgeschlagen: {exc}. "
            f"Pruefe Standard '{thread_standard}', Groesse '{thread_size}', "
            f"Klasse '{thread_class}'."
        )

    # Thread erstellen.
    try:
        if depth_mm <= 0:
            comp_def.Features.ThreadFeatures.Add(
                target_face, start_edge, thread_info,
                False, False, "2 cm", 0,
            )
            art = "Gewinde (volle Laenge)"
        else:
            comp_def.Features.ThreadFeatures.Add(
                target_face, start_edge, thread_info,
                False, False, f"{_mm_to_cm(depth_mm)} cm", 0,
            )
            art = f"Gewinde (Tiefe {depth_mm} mm)"
    except Exception as exc:
        raise RuntimeError(
            f"ThreadFeatures.Add fehlgeschlagen: {exc}. "
            f"Flaeche Index={face_index}, Kante Index=1."
        )

    return (
        f"{art} erstellt: {thread_size} ({thread_standard}, "
        f"Klasse {thread_class})."
    )


@mcp.tool()
def create_slot(
    length_mm: float,
    width_mm: float,
    depth_mm: float,
    x_mm: float = 0.0,
    y_mm: float = 0.0,
) -> str:
    """
    Erzeugt eine Nut / einen Schlitz (rechteckiger Materialabtrag).
    Vereinfachte Version von extrude_cut mit sinnvollen Defaults.

    Args:
        length_mm: Laenge der Nut in X (mm).
        width_mm:  Breite der Nut in Y (mm).
        depth_mm:  Tiefe der Nut (mm).
        x_mm:      X-Position der linken unteren Ecke (mm, Standard 0).
        y_mm:      Y-Position der linken unteren Ecke (mm, Standard 0).
    """
    return extrude_cut(length_mm, width_mm, depth_mm, x_mm, y_mm)


@mcp.tool()
def create_3d_path(points: str) -> str:
    """
    Erzeugt eine 3D-Skizze aus verbundenen Linien (Pfad fuer Sweep/Loft).
    Die Skizze bleibt als 3D-Skizze bestehen und kann fuer spaetere
    Sweep/Loft-Operationen verwendet werden.

    Args:
        points: Kommagetrennte 3D-Punkte in mm.
                Format: "x1,y1,z1;x2,y2,z2;x3,y3,z3"
    """
    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition
    tg = app.TransientGeometry

    raw_points = [p.strip() for p in points.split(";") if p.strip()]
    if len(raw_points) < 2:
        raise ValueError("Mindestens 2 Punkte noetig.")

    points_cm = []
    for pt_str in raw_points:
        parts = pt_str.split(",")
        if len(parts) != 3:
            raise ValueError(f"Ungueltiger Punkt: '{pt_str}'. Erwartet 'x,y,z'.")
        x, y, z = [_mm_to_cm(float(v)) for v in parts]
        points_cm.append(tg.CreatePoint(x, y, z))

    sketch3d = comp_def.Sketches3D.Add()
    wp_list = []
    for p in points_cm:
        wp = comp_def.WorkPoints.AddFixed(p)
        wp_list.append(wp)

    line = sketch3d.SketchLines3D.AddByTwoPoints(wp_list[0], wp_list[1], True)
    for i in range(2, len(wp_list)):
        line = sketch3d.SketchLines3D.AddByTwoPoints(
            line.EndPoint, wp_list[i], True
        )

    return (
        f"3D-Pfad erstellt: {len(points_cm)} Punkte, "
        f"{len(points_cm) - 1} Linien-Segmente."
    )


# ===========================================================================
# MUSTER (PATTERN)  -  arbeitet auf der zuletzt erzeugten Bohrung
# ===========================================================================
def _last_hole(comp_def):
    """Liefert die zuletzt erzeugte Bohrung als Muster-Ausgangsfeature."""
    holes = comp_def.Features.HoleFeatures
    if holes.Count == 0:
        raise RuntimeError(
            "Keine Bohrung vorhanden. Muster arbeitet auf der letzten "
            "Bohrung - erst 'add_hole' aufrufen."
        )
    return holes.Item(holes.Count)


@mcp.tool()
def pattern_rectangular(
    x_count: int,
    x_spacing_mm: float,
    y_count: int = 1,
    y_spacing_mm: float = 0.0,
) -> str:
    """
    Erzeugt ein rechteckiges Muster der ZULETZT erstellten Bohrung entlang
    der X- und optional Y-Achse.

    Args:
        x_count:      Anzahl in X-Richtung (>=1).
        x_spacing_mm: Abstand in X (mm).
        y_count:      Anzahl in Y-Richtung (Standard 1).
        y_spacing_mm: Abstand in Y (mm, nur falls y_count > 1).
    """
    if x_count < 1 or y_count < 1:
        raise ValueError("Anzahlen muessen >= 1 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    parents = app.TransientObjects.CreateObjectCollection()
    parents.Add(_last_hole(comp_def))

    x_axis = comp_def.WorkAxes.Item(1)
    y_axis = comp_def.WorkAxes.Item(2)
    rect = comp_def.Features.RectangularPatternFeatures

    # Abstaende als Ausdruck mit Einheit (robust gegenueber Einheiten).
    x_spacing = f"{x_spacing_mm} mm"
    pdef = rect.CreateDefinition(parents, x_axis, True, x_count, x_spacing)
    if y_count > 1:
        pdef.YDirectionEntity = y_axis
        pdef.YCount = y_count
        pdef.YSpacing = f"{y_spacing_mm} mm"
    rect.AddByDefinition(pdef)

    return (
        f"Rechteckmuster erstellt: {x_count} x {y_count} "
        f"(X-Abstand {x_spacing_mm} mm, Y-Abstand {y_spacing_mm} mm)."
    )


@mcp.tool()
def pattern_circular(count: int, angle_deg: float = 360.0) -> str:
    """
    Erzeugt ein kreisfoermiges Muster der ZULETZT erstellten Bohrung um die
    Z-Achse.

    Args:
        count:     Anzahl der Elemente (>=2).
        angle_deg: Gesamtwinkel in Grad (360 = gleichmaessig im Vollkreis).
    """
    if count < 2:
        raise ValueError("Anzahl muss >= 2 sein.")

    app = _get_app()
    doc = _require_part_document(app)
    doc.Activate()
    comp_def = doc.ComponentDefinition

    parents = app.TransientObjects.CreateObjectCollection()
    parents.Add(_last_hole(comp_def))

    z_axis = comp_def.WorkAxes.Item(3)
    circ = comp_def.Features.CircularPatternFeatures
    # Add(ParentFeatures, AxisEntity, NaturalAxisDirection, Count, Angle, Fit)
    circ.Add(parents, z_axis, True, count, f"{angle_deg} deg", False)

    return f"Kreismuster erstellt: {count} Elemente ueber {angle_deg} grd."


# ===========================================================================
# BAUGRUPPEN (ASSEMBLIES)
# ===========================================================================
@mcp.tool()
def create_assembly(name: str = "Baugruppe1") -> str:
    """
    Erstellt eine neue, leere Baugruppe (.iam) und macht sie aktiv.

    Args:
        name: Anzeigename der Baugruppe.
    """
    app = _get_app()
    doc_type = _const.kAssemblyDocumentObject
    template = app.FileManager.GetTemplateFile(doc_type)
    doc = app.Documents.Add(doc_type, template, True)
    try:
        doc.DisplayName = name
    except Exception:
        pass
    return f"Neue Baugruppe '{name}' erstellt."


@mcp.tool()
def place_component(
    file_path: str,
    x_mm: float = 0.0,
    y_mm: float = 0.0,
    z_mm: float = 0.0,
) -> str:
    """
    Platziert ein Bauteil/eine Baugruppe an Position (x, y, z) in der aktiven
    Baugruppe. Hinweis: Es werden KEINE Abhaengigkeiten (Constraints) gesetzt,
    die Komponente wird nur positioniert. Die erste Komponente wird fixiert.

    Args:
        file_path: Pfad zur .ipt/.iam-Datei.
        x_mm:      X-Position (mm).
        y_mm:      Y-Position (mm).
        z_mm:      Z-Position (mm).
    """
    app = _get_app()
    doc = app.ActiveDocument
    if doc is None or doc.DocumentType != _const.kAssemblyDocumentObject:
        raise RuntimeError(
            "Aktives Dokument ist keine Baugruppe. Erst 'create_assembly'."
        )
    asm_def = win32com.client.CastTo(doc, "AssemblyDocument").ComponentDefinition
    tg = app.TransientGeometry

    # Positionsmatrix mit Verschiebung erstellen.
    matrix = tg.CreateMatrix()
    matrix.SetTranslation(
        tg.CreateVector(_mm_to_cm(x_mm), _mm_to_cm(y_mm), _mm_to_cm(z_mm))
    )

    is_first = asm_def.Occurrences.Count == 0
    occ = asm_def.Occurrences.Add(file_path, matrix)
    if is_first:
        try:
            occ.Grounded = True
        except Exception:
            pass

    return (
        f"Komponente platziert: {occ.Name} bei ({x_mm}, {y_mm}, {z_mm}) mm"
        + (" (fixiert)" if is_first else "")
        + "."
    )


@mcp.tool()
def list_occurrences() -> list:
    """Listet die Komponenten (Occurrences) der aktiven Baugruppe auf."""
    app = _get_app()
    doc = app.ActiveDocument
    if doc is None or doc.DocumentType != _const.kAssemblyDocumentObject:
        raise RuntimeError("Aktives Dokument ist keine Baugruppe.")
    asm_def = win32com.client.CastTo(doc, "AssemblyDocument").ComponentDefinition
    result = []
    for occ in asm_def.Occurrences:
        try:
            result.append({"name": occ.Name, "grounded": occ.Grounded})
        except Exception:
            continue
    return result


# ===========================================================================
# TECHNISCHE ZEICHNUNGEN (DRAWINGS / DWG)
# ===========================================================================
@mcp.tool()
def create_drawing(model_path: str = "", scale: float = 1.0) -> str:
    """
    Erstellt eine neue Zeichnung mit Standardansichten (Vorderansicht +
    Draufsicht + Seitenansicht + Iso) aus einem Modell.

    Args:
        model_path: Pfad zur .ipt/.iam-Datei. Leer = aktuell aktives Modell
                    verwenden (muss ein Bauteil/Baugruppe sein).
        scale:      Massstab der Ansichten (z. B. 1.0, 0.5, 2.0).
    """
    app = _get_app()

    # Modell bestimmen.
    if model_path:
        model = app.Documents.Open(model_path, False)
    else:
        model = app.ActiveDocument
        if model is None or model.DocumentType not in (
            _const.kPartDocumentObject,
            _const.kAssemblyDocumentObject,
        ):
            raise RuntimeError(
                "Kein Modell angegeben und aktives Dokument ist kein "
                "Bauteil/keine Baugruppe."
            )

    # Neue Zeichnung erstellen.
    doc_type = _const.kDrawingDocumentObject
    template = app.FileManager.GetTemplateFile(doc_type)
    draw_doc = win32com.client.CastTo(
        app.Documents.Add(doc_type, template, True), "DrawingDocument"
    )
    sheet = draw_doc.ActiveSheet
    tg = app.TransientGeometry

    style = _const.kHiddenLineRemovedDrawingViewStyle

    # Basisansicht (Vorderansicht) links unten.
    base_pt = tg.CreatePoint2d(12, 12)
    base_view = sheet.DrawingViews.AddBaseView(
        model, base_pt, scale, _const.kFrontViewOrientation, style
    )

    # Projizierte Ansichten: Draufsicht (oben), Seitenansicht (rechts),
    # Iso (rechts oben).
    sheet.DrawingViews.AddProjectedView(
        base_view, tg.CreatePoint2d(12, 24), style, scale
    )
    sheet.DrawingViews.AddProjectedView(
        base_view, tg.CreatePoint2d(26, 12), style, scale
    )
    sheet.DrawingViews.AddProjectedView(
        base_view, tg.CreatePoint2d(26, 24), style, scale
    )

    return (
        "Zeichnung mit 4 Ansichten erstellt (Vorder-, Drauf-, Seiten-, "
        f"Iso-Ansicht), Massstab {scale}."
    )


@mcp.tool()
def export_dwg(file_path: str) -> str:
    """
    Exportiert die aktive Zeichnung als DWG-Datei.

    Args:
        file_path: Zielpfad, z. B. C:/temp/zeichnung.dwg
    """
    app = _get_app()
    doc = app.ActiveDocument
    if doc is None or doc.DocumentType != _const.kDrawingDocumentObject:
        raise RuntimeError(
            "Aktives Dokument ist keine Zeichnung. Erst 'create_drawing'."
        )
    addin = _get_translator(app, "dwg")
    _export_via_translator(app, doc, addin, file_path)
    return f"DWG-Export gespeichert: {file_path}"


# ===========================================================================
# SCREENSHOT
# ===========================================================================
mViewOrientationMap = None


def _get_orientation_map():
    """Lazy-Load der ViewOrientationType-Konstanten."""
    global mViewOrientationMap
    if mViewOrientationMap is None:
        mViewOrientationMap = {
            "front": _const.kFrontViewOrientation,
            "back": _const.kBackViewOrientation,
            "top": _const.kTopViewOrientation,
            "bottom": _const.kBottomViewOrientation,
            "left": _const.kLeftViewOrientation,
            "right": _const.kRightViewOrientation,
            "iso": _const.kIsoTopRightViewOrientation,
            "iso_topleft": _const.kIsoTopLeftViewOrientation,
            "iso_bottomright": _const.kIsoBottomRightViewOrientation,
            "iso_bottomleft": _const.kIsoBottomLeftViewOrientation,
        }
    return mViewOrientationMap


@mcp.tool()
def screenshot(
    file_path: str,
    width: int = 1920,
    height: int = 1080,
    orientation: str = "home",
) -> str:
    """
    Speichert einen Screenshot des aktuellen Inventor-Views als Bild.

    Zwei Modi:
      - "home": Nimmt den aktuellen View (nach Fit), einfach und schnell.
      - Beliebige Orientierung (front/back/top/bottom/left/right/iso):
        Erstellt eine Kamera mit der gewuenschten Ausrichtung, fittet
        auf das gesamte Modell und speichert das Bild.

    Args:
        file_path:   Zielpfad (PNG/BMP/JPG), z. B. "C:/temp/screenshot.png".
        width:       Breite in Pixeln (Standard 1920).
        height:      Hoehe in Pixeln (Standard 1080).
        orientation: "home" oder eine der Orientierungen:
                     "front", "back", "top", "bottom", "left", "right",
                     "iso", "iso_topleft", "iso_bottomright", "iso_bottomleft".
    """
    app = _get_app()
    doc = app.ActiveDocument
    if doc is None:
        raise RuntimeError("Kein Dokument geoeffnet.")

    orientation = orientation.lower().strip()

    if orientation == "home":
        # Variante 1: ActiveView (aktueller View).
        view = app.ActiveView
        try:
            view.Fit()
        except Exception:
            pass
        try:
            view.SaveAsBitmap(file_path, width, height)
        except Exception as exc:
            raise RuntimeError(
                f"Screenshot fehlgeschlagen: {exc}"
            )
        return f"Screenshot (ActiveView) gespeichert: {file_path}"

    # Variante 2: Camera mit definierter Orientierung.
    orientation_map = _get_orientation_map()
    if orientation not in orientation_map:
        raise ValueError(
            f"Unbekannte Orientierung '{orientation}'. "
            f"Erlaubt: 'home', {', '.join(sorted(orientation_map.keys()))}."
        )

    # ComponentDefinition bestimmen.
    try:
        part_doc = win32com.client.CastTo(doc, "PartDocument")
        comp_def = part_doc.ComponentDefinition
    except Exception:
        try:
            asm_doc = win32com.client.CastTo(doc, "AssemblyDocument")
            comp_def = asm_doc.ComponentDefinition
        except Exception:
            raise RuntimeError(
                "Kein Bauteil/Baugruppe geoeffnet. "
                "Nur Bauteile und Baugruppen erlauben screenshots."
            )

    cam = app.TransientObjects.CreateCamera()
    cam.SceneObject = comp_def
    cam.ViewOrientationType = orientation_map[orientation]
    cam.Fit()
    cam.ApplyWithoutTransition()

    try:
        cam.SaveAsBitmap(file_path, width, height)
    except Exception as exc:
        raise RuntimeError(
            f"Screenshot fehlgeschlagen: {exc}"
        )

    return (
        f"Screenshot ({orientation}) gespeichert: {file_path} "
        f"({width}x{height} px)."
    )


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # stdio-Transport ist der Standard fuer Claude Desktop.
    mcp.run(transport="stdio")