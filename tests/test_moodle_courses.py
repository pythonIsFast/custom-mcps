import sys
import types
import unittest


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


class MoodleCoursePaginationTests(unittest.TestCase):
    def setUp(self):
        self.client = MoodleSession("https://moodle.example")
        self.courses = [
            {
                "id": index,
                "fullname": f"Course {index:02d}",
                "shortname": f"C{index:02d}",
                "coursecategory": "Test",
                "visible": 1,
                "summary": "Large field that must not reach the MCP result",
            }
            for index in range(1, 61)
        ]
        self.ajax_calls = []

        def fake_ajax(method, args):
            self.ajax_calls.append((method, args))
            filtered = self.courses
            search = args.get("searchvalue", "").casefold()
            if search:
                filtered = [
                    course for course in filtered
                    if search in course["fullname"].casefold()
                ]
            offset = args["offset"]
            limit = args["limit"]
            page = filtered[offset:offset + limit]
            next_offset = offset + len(page)
            if next_offset >= len(filtered):
                next_offset = 0
            return {"courses": page, "nextoffset": next_offset}

        self.client.ajax = fake_ajax

    def test_large_course_list_is_paginated(self):
        first = self.client.list_courses()
        second = self.client.list_courses(offset=first["naechster_offset"])
        third = self.client.list_courses(offset=second["naechster_offset"])

        self.assertEqual(len(first["kurse"]), 25)
        self.assertEqual(first["naechster_offset"], 25)
        self.assertEqual(len(second["kurse"]), 25)
        self.assertEqual(second["naechster_offset"], 50)
        self.assertEqual(len(third["kurse"]), 10)
        self.assertFalse(third["weitere_vorhanden"])
        self.assertIsNone(third["naechster_offset"])

    def test_search_is_sent_to_moodle_and_result_is_compact(self):
        result = self.client.list_courses(search="Course 42", limit=10)

        self.assertEqual(result["anzahl"], 1)
        self.assertEqual(result["kurse"][0]["id"], 42)
        self.assertNotIn("summary", result["kurse"][0])
        _, args = self.ajax_calls[-1]
        self.assertEqual(args["searchvalue"], "Course 42")
        self.assertEqual(args["limit"], 10)

    def test_invalid_pagination_is_rejected(self):
        with self.assertRaises(ValueError):
            self.client.list_courses(limit=0)
        with self.assertRaises(ValueError):
            self.client.list_courses(limit=101)
        with self.assertRaises(ValueError):
            self.client.list_courses(offset=-1)


class MoodleCourseHtmlFallbackTests(unittest.TestCase):
    def test_dashboard_fallback_is_filtered_and_paginated(self):
        client = MoodleSession("https://moodle.example")
        client.ajax = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("AJAX unavailable")
        )

        class Response:
            text = """
                <main>
                  <div data-region="course-content">
                    <a href="/course/view.php?id=10">Math Basics</a>
                    <a href="/course/view.php?id=11">Advanced Math</a>
                    <a href="/course/view.php?id=12">English</a>
                  </div>
                </main>
            """

            @staticmethod
            def raise_for_status():
                return None

        client.s.get = lambda *_args, **_kwargs: Response()

        result = client.list_courses(search="math", limit=1, offset=1)

        self.assertEqual(result["quelle"], "HTML-Fallback (Meine Kurse/Dashboard)")
        self.assertEqual(result["anzahl"], 1)
        self.assertEqual(result["kurse"][0]["id"], 11)
        self.assertFalse(result["weitere_vorhanden"])


if __name__ == "__main__":
    unittest.main()
