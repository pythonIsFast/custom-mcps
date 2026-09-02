import io
import json
import sys
import types
import unittest
import zipfile


class _FakeFastMCP:
    def __init__(self, _name):
        pass

    def tool(self):
        return lambda function: function

    def run(self):
        pass


fake_fastmcp = types.ModuleType("fastmcp")
fake_fastmcp.FastMCP = _FakeFastMCP
sys.modules.setdefault("fastmcp", fake_fastmcp)

from moodle_mcp_server import MoodleSession


MANIFEST = {
    "title": "Example",
    "language": "de",
    "mainLibrary": "H5P.Example",
    "embedTypes": ["div"],
    "preloadedDependencies": [
        {"machineName": "H5P.Example", "majorVersion": 1, "minorVersion": 0}
    ],
}
CONTENT = {"question": "Alt", "answers": ["A", "B"]}


class MoodleH5PPackageTests(unittest.TestCase):
    def test_build_and_inspect_minimal_package(self):
        package = MoodleSession.build_h5p_package(MANIFEST, CONTENT)
        details = MoodleSession.inspect_h5p_package(package)

        self.assertEqual(details["h5p"]["mainLibrary"], "H5P.Example")
        self.assertEqual(details["content"], CONTENT)
        self.assertIn("h5p.json", details["dateien"])
        self.assertIn("content/content.json", details["dateien"])

    def test_template_edit_preserves_libraries_and_assets(self):
        semantics = [{"name": "question", "type": "text"}]
        template = MoodleSession.build_h5p_package(
            MANIFEST,
            CONTENT,
            {
                "H5P.Example-1.0/library.json": json.dumps({
                    "machineName": "H5P.Example", "majorVersion": 1,
                    "minorVersion": 0,
                }).encode(),
                "H5P.Example-1.0/semantics.json": json.dumps(semantics).encode(),
                "content/images/example.png": b"image-bytes",
            },
        )

        edited = MoodleSession.build_h5p_package(
            MANIFEST, {"question": "Neu"}, template_bytes=template)
        details = MoodleSession.inspect_h5p_package(edited)

        self.assertEqual(details["content"], {"question": "Neu"})
        self.assertEqual(
            details["semantics"]["H5P.Example-1.0/semantics.json"], semantics)
        with zipfile.ZipFile(io.BytesIO(edited)) as archive:
            self.assertEqual(
                archive.read("content/images/example.png"), b"image-bytes")

    def test_unsafe_archive_path_is_rejected(self):
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("h5p.json", json.dumps(MANIFEST))
            archive.writestr("content/content.json", json.dumps(CONTENT))
            archive.writestr("../outside", "bad")

        with self.assertRaisesRegex(ValueError, "Unsicherer Pfad"):
            MoodleSession.inspect_h5p_package(target.getvalue())

    def test_missing_manifest_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Pflichtfelder"):
            MoodleSession.build_h5p_package({"title": "Incomplete"}, CONTENT)

    def test_context_id_supports_moodle_page_variants(self):
        self.assertEqual(
            MoodleSession._context_id_from_html('M.cfg.contextid = 42;'), 42)
        self.assertEqual(
            MoodleSession._context_id_from_html('<body class="context-81">'), 81)
        self.assertEqual(
            MoodleSession._context_id_from_html('{"contextid":123}'), 123)


class MoodleH5PActivityTests(unittest.TestCase):
    def setUp(self):
        self.client = MoodleSession("https://moodle.example")
        self.package = MoodleSession.build_h5p_package(MANIFEST, CONTENT)

    def test_create_uploads_to_form_context_and_uses_packagefile(self):
        calls = {}
        self.client.context_id = lambda path, params: 77

        def upload(filename, content, **kwargs):
            calls["upload"] = (filename, content, kwargs)
            return {"draft_itemid": 456, "repo_id_verwendet": 4}

        def create(course, section, activity_type, name, intro, fields):
            calls["create"] = (course, section, activity_type, name, intro, fields)
            return {"antwort_url": "https://moodle.example/mod/h5pactivity/view.php?id=9"}

        self.client.upload_draft_file = upload
        self.client.create_activity = create

        result = self.client.create_h5p_activity(
            10, 2, "Mein H5P", self.package, settings={"enabletracking": 1})

        self.assertEqual(calls["upload"][2]["course_context_id"], 77)
        self.assertEqual(calls["upload"][2]["content_type"], "application/zip")
        self.assertEqual(calls["create"][2], "h5pactivity")
        self.assertEqual(calls["create"][5]["packagefile"], 456)
        self.assertEqual(calls["create"][5]["enabletracking"], 1)
        self.assertEqual(result["upload"]["draft_itemid"], 456)

    def test_update_without_package_changes_only_settings(self):
        captured = {}

        def update(cmid, name, intro, fields):
            captured["args"] = (cmid, name, intro, fields)
            return {"antwort_url": "ok"}

        self.client.update_activity = update
        result = self.client.update_h5p_activity(
            91, name="Neu", settings={"reviewmode": 1})

        self.assertEqual(captured["args"], (91, "Neu", None, {"reviewmode": 1}))
        self.assertEqual(result["antwort_url"], "ok")


if __name__ == "__main__":
    unittest.main()
