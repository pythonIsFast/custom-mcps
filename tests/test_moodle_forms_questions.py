import sys
import types
import unittest
from unittest.mock import Mock


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


class MoodleFormTests(unittest.TestCase):
    def setUp(self):
        self.client = MoodleSession("https://moodle.example")

    @staticmethod
    def response(html, url="https://moodle.example/example.php", status=200):
        response = Mock()
        response.text = html
        response.url = url
        response.status_code = status
        response.raise_for_status.side_effect = None
        return response

    def test_fetch_form_skips_editmode_and_lists_all_forms(self):
        self.client.s.get = Mock(return_value=self.response("""
            <form id="edit" action="/editmode.php"><input name="setmode" value="1"></form>
            <form id="content" action="submit.php" method="post" enctype="multipart/form-data">
              <input name="sesskey" value="abc"><textarea name="body">Text</textarea>
            </form>
        """))

        result = self.client.fetch_form("/example.php")

        self.assertEqual(result["form_id"], "content")
        self.assertEqual(result["form_action"], "https://moodle.example/submit.php")
        self.assertEqual(result["werte"], {"sesskey": "abc", "body": "Text"})
        self.assertEqual(len(result["formulare"]), 2)

    def test_missing_explicit_selector_is_an_error(self):
        self.client.s.get = Mock(return_value=self.response("<form id='one'></form>"))
        with self.assertRaisesRegex(RuntimeError, "findet kein Formular"):
            self.client.fetch_form("/example.php", form_selector="form#missing")

    def test_multipart_post_has_browser_headers(self):
        response = self.response("ok", url="https://moodle.example/done.php")
        self.client.s.post = Mock(return_value=response)

        self.client.submit_form("/submit.php", {"a": "1"},
                                "https://moodle.example/form.php", multipart=True)

        _, kwargs = self.client.s.post.call_args
        self.assertEqual(kwargs["headers"]["Origin"], "https://moodle.example")
        self.assertEqual(kwargs["headers"]["Referer"],
                         "https://moodle.example/form.php")
        self.assertEqual(kwargs["files"], [("a", (None, "1"))])

    def test_url_validation_rejects_prefix_confusion(self):
        with self.assertRaises(ValueError):
            self.client._resolve_url("https://moodle.example.evil.test/path")


class MoodleQuestionQuizTests(unittest.TestCase):
    def setUp(self):
        self.client = MoodleSession("https://moodle.example")
        self.client.sesskey = "session-key"

    def test_list_questions_uses_documented_ajax_arguments(self):
        captured = {}

        def ajax(method, args):
            captured.update(method=method, args=args)
            return {"totalcount": 1, "questions": [
                {"id": 7, "name": "Q", "qtype": "multichoice", "category": 3}
            ]}

        self.client.ajax = ajax
        result = self.client.list_questions(3, 9)

        self.assertEqual(captured["method"],
                         "core_question_get_random_question_summaries")
        self.assertEqual(captured["args"]["contextid"], 9)
        self.assertEqual(result["fragen"][0]["id"], 7)

    def test_quiz_structure_parses_slot(self):
        html = """
          <li class="slot" data-slot="2">
            <span class="questionname">Frage A</span>
            <a href="/question/bank/previewquestion/preview.php?id=17">Preview</a>
            <input data-region="maxmark" value="2.5">
          </li>
        """
        response = MoodleFormTests.response(
            html, "https://moodle.example/mod/quiz/edit.php?cmid=4")
        self.client.s.get = Mock(return_value=response)

        result = self.client.quiz_structure(4)

        self.assertEqual(result["slots"][0]["frage_id"], 17)
        self.assertEqual(result["slots"][0]["punkte"], "2.5")


if __name__ == "__main__":
    unittest.main()
