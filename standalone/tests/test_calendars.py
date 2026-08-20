"""Tests for asking every connected calendar one question.

    cd standalone && python3 -m tests.test_calendars

Two calendars cannot be tested against real accounts here, so the sources are
fakes shaped like the real ones: different argument names, and one that fails.
That is the case the merge exists for.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import calendars  # noqa: E402
from harness.tools import TOOLS  # noqa: E402
from harness.world import ToolError  # noqa: E402


def fake(name, params, run):
    TOOLS[name] = {"desc": "fake calendar", "params": params,
                   "example": {"tool": name, "args": {}}, "run": run}


class TestCalendars(unittest.TestCase):
    def setUp(self):
        self.saved = dict(TOOLS)

    def tearDown(self):
        TOOLS.clear()
        TOOLS.update(self.saved)

    def test_creators_and_reminders_are_not_calendar_listings(self):
        found = calendars.sources()
        self.assertIn("list_events", found)
        for not_a_listing in ("add_event", "cancel_event", "update_event",
                              "set_reminder"):
            self.assertNotIn(not_a_listing, found)

    def test_server_invented_names_are_recognised(self):
        fake("get-calendar-view", {}, lambda w, m, a: "")
        fake("gcal_list-events", {}, lambda w, m, a: "")
        found = calendars.sources()
        self.assertIn("get-calendar-view", found)
        self.assertIn("gcal_list-events", found)

    def test_one_calendar_gets_no_merged_tool(self):
        calendars.enable()
        self.assertNotIn(calendars.MERGED, TOOLS)

    def test_two_calendars_get_one(self):
        fake("gcal_list-events", {}, lambda w, m, a: "personal: dentist")
        calendars.enable()
        self.assertIn(calendars.MERGED, TOOLS)

    def test_every_calendar_answers_and_each_answer_is_labelled(self):
        fake("gcal_list-events", {"date": ("string", False)},
             lambda w, m, a: "dentist 09:00")
        fake("ms365_get-calendar-view", {"date": ("string", False)},
             lambda w, m, a: "standup 10:00")
        calendars.enable()
        out = TOOLS[calendars.MERGED]["run"](None, None, {"date": "2026-07-23"})
        self.assertIn("[gcal_list-events]", out)
        self.assertIn("dentist 09:00", out)
        self.assertIn("[ms365_get-calendar-view]", out)
        self.assertIn("standup 10:00", out)

    def test_each_source_gets_only_the_arguments_it_declares(self):
        seen = {}
        fake("gcal_list-events", {"date": ("string", False)},
             lambda w, m, a: seen.setdefault("gcal", a) and "" or "")
        fake("ms365_get-calendar-view", {"start": ("string", False)},
             lambda w, m, a: seen.setdefault("ms", a) and "" or "")
        calendars.enable()
        TOOLS[calendars.MERGED]["run"](None, None, {"date": "2026-07-23"})
        self.assertEqual(seen["gcal"], {"date": "2026-07-23"})
        self.assertEqual(seen["ms"], {})       # never handed an argument it rejects

    def test_a_failing_calendar_does_not_take_the_others_down(self):
        def boom(w, m, a):
            raise ToolError("token expired")
        fake("gcal_list-events", {}, boom)
        fake("ms365_get-calendar-view", {}, lambda w, m, a: "standup 10:00")
        calendars.enable()
        out = TOOLS[calendars.MERGED]["run"](None, None, {})
        self.assertIn("token expired", out)
        self.assertIn("standup 10:00", out)

    def test_a_calendar_that_needs_an_argument_we_lack_says_so(self):
        fake("gcal_list-events", {"calendar_id": ("string", True)},
             lambda w, m, a: "never runs")
        fake("ms365_get-calendar-view", {}, lambda w, m, a: "standup 10:00")
        calendars.enable()
        out = TOOLS[calendars.MERGED]["run"](None, None, {"date": "2026-07-23"})
        self.assertIn("needs calendar_id", out)
        self.assertNotIn("never runs", out)
        self.assertIn("standup 10:00", out)

    def test_no_calendar_at_all_is_an_error_not_an_empty_answer(self):
        for n in calendars.sources():
            TOOLS.pop(n)
        with self.assertRaises(ToolError):
            calendars._run_all(None, None, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
