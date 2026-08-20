"""Tests for saying what a step was.

    cd standalone && python3 -m tests.test_narrate

The table is easy; the cases that matter are the ones where the sentence has to
survive contact with reality - an argument that is missing, a tool name no one
here chose, a call that failed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import narrate  # noqa: E402


class TestSentences(unittest.TestCase):
    def test_present_while_running_past_once_done(self):
        self.assertEqual(narrate.about("list_events", {}), "Checking your calendar")
        self.assertEqual(narrate.about("list_events", {}, done=True), "Checked your calendar")

    def test_arguments_land_in_the_sentence(self):
        line = narrate.about("add_event", {"title": "Deep work", "date": "2026-07-23"},
                             done=True)
        self.assertEqual(line, "Added Deep work to 2026-07-23")

    def test_a_missing_argument_does_not_strand_its_preposition(self):
        """"Putting Deep work on" reads as a sentence cut off, not a short one."""
        self.assertEqual(narrate.about("add_event", {"title": "Deep work"}),
                         "Putting Deep work")
        self.assertEqual(narrate.about("send_email", {}), "Writing")

    def test_a_long_argument_is_clipped_not_pasted_whole(self):
        line = narrate.about("create_presentation", {"filename": "x" * 200})
        self.assertLess(len(line), 80)
        self.assertIn("...", line)

    def test_the_harness_talking_to_itself_is_not_narrated(self):
        for quiet in ("think", "done", "request_tools"):
            self.assertIsNone(narrate.about(quiet, {}))


class TestInventedNames(unittest.TestCase):
    """An MCP server names its own tools, so this is the common case the moment
    a real mailbox is connected."""

    def test_a_known_verb_is_conjugated(self):
        self.assertEqual(narrate.about("get-calendar-view", {}), "Getting calendar view")
        self.assertEqual(narrate.about("get-calendar-view", {}, done=True),
                         "Got calendar view")
        self.assertEqual(narrate.about("create-reply-draft", {}, done=True),
                         "Created reply draft")

    def test_an_unknown_verb_keeps_the_name_rather_than_inventing(self):
        self.assertEqual(narrate.about("frobnicate_thing", {}), "Running frobnicate thing")

    def test_a_nameless_call_still_produces_a_sentence(self):
        self.assertTrue(narrate.about("", {}))


class TestFailure(unittest.TestCase):
    def test_a_failure_names_the_errand_not_the_function(self):
        line = narrate.failed("add_event", {"title": "Deep work", "date": "2026-07-23"},
                              "that slot is taken")
        self.assertIn("Putting Deep work on 2026-07-23", line)
        self.assertIn("that didn't work", line)
        self.assertIn("that slot is taken", line)

    def test_a_wall_of_error_text_is_cut(self):
        self.assertLess(len(narrate.failed("read_file", {"path": "a"}, "x" * 900)), 220)


if __name__ == "__main__":
    unittest.main(verbosity=2)
