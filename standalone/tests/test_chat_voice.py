"""Tests for the reply voice, and for where it is allowed to apply.

    cd standalone && python3 -m tests.test_chat_voice

The summary done() carries is the only part of a run a person reads, so the
chat surface asks for it in a different voice. The risk is not that the wording
is wrong - it is that the wording leaks into the prompt bench/ grades, or into
the CLI, and quietly changes runs that are supposed to be comparable.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import agent, chat  # noqa: E402
from harness.tools import TOOLS  # noqa: E402

STANDALONE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source(rel):
    with open(os.path.join(STANDALONE, rel), encoding="utf-8") as f:
        return f.read()


class TestVoice(unittest.TestCase):
    def test_it_says_not_every_message_needs_a_tool(self):
        """Without this the loop's one-tool-call-per-reply contract turns
        "morning, what can you do?" into a hunt through the inbox."""
        text = chat.reply_rules()
        self.assertIn("Not every message needs a tool", text)
        self.assertIn("say", text)

    def test_it_asks_for_second_person_and_an_answer_first(self):
        text = chat.reply_rules()
        self.assertIn('"you"', text)
        self.assertIn("the user", text)          # named as the thing to avoid
        self.assertIn("first sentence IS the answer", text)

    def test_it_stays_abstract(self):
        """agent.py keeps concrete content out of RULES because a small model
        copies it verbatim. An instruction about how to answer must not carry a
        specimen answer."""
        text = chat.reply_rules().lower()
        for specimen in ("teal", "booked you", "sam", "thursday"):
            self.assertNotIn(specimen, text)


class TestSay(unittest.TestCase):
    def setUp(self):
        self.saved = dict(TOOLS)

    def tearDown(self):
        TOOLS.clear()
        TOOLS.update(self.saved)

    def test_say_is_opt_in_and_reversible(self):
        """It is not in the registry bench/ grades; the chat surface adds it."""
        self.assertNotIn("say", TOOLS)
        chat.enable_say()
        self.assertIn("say", TOOLS)
        chat.disable_say()
        self.assertNotIn("say", TOOLS)

    def test_saying_something_costs_no_context_beyond_the_message(self):
        """The message is the effect; echoing it back would put a second copy
        of it in the transcript."""
        chat.enable_say()
        self.assertEqual(TOOLS["say"]["run"](None, None, {"text": "hello"}), "sent")

    def test_the_example_carries_no_specimen_sentence(self):
        chat.enable_say()
        self.assertIn("<", TOOLS["say"]["example"]["args"]["text"])

    def test_said_collects_the_messages_in_order(self):
        events = [{"t": "tool", "name": "say", "args": {"text": "One."}},
                  {"t": "tool", "name": "list_events", "args": {}},
                  {"t": "note", "kind": "plan", "content": "x"},
                  {"t": "tool", "name": "say", "args": {"text": "Two."}}]
        self.assertEqual(chat.said(events), ["One.", "Two."])

    def test_an_empty_message_is_not_a_turn(self):
        self.assertEqual(chat.said([{"t": "tool", "name": "say", "args": {"text": "  "}}]), [])


class TestScope(unittest.TestCase):
    def test_the_graded_prompt_does_not_carry_it(self):
        self.assertNotIn("HOW TO WRITE THE SUMMARY", agent.HARNESS_SYSTEM)
        self.assertNotIn("HOW TO WRITE THE SUMMARY", agent.RAW_SYSTEM)
        self.assertEqual(agent.EXTRA_RULES, "")   # empty until a runner sets it

    def test_the_chat_surface_applies_it_and_the_cli_does_not(self):
        self.assertIn("chat.reply_rules()", source("webui/runner.py"))
        self.assertIn("chat.enable_say()", source("webui/runner.py"))
        self.assertNotIn("reply_rules", source("agents/8b/run_agent.py"))
        self.assertNotIn("enable_say", source("agents/8b/run_agent.py"))

    def test_what_was_said_becomes_the_stored_turn(self):
        """A run that already spoke must not get a summary of itself pasted
        underneath the message."""
        self.assertIn("chat.said(run.events)", source("webui/server.py"))

    def test_the_step_sentence_is_sent_and_rendered(self):
        """One source for the phrasing: narrate.py fills it in, the event
        carries it, the page reads it. A copy of the table in JavaScript is
        what this is guarding against."""
        self.assertIn("narrate.about(name, args_, done=True)", source("webui/runner.py"))
        self.assertIn("e.line", source("webui/static/app.js"))

    def test_it_is_appended_so_root_and_mcp_rules_survive(self):
        """--root ASSIGNS EXTRA_RULES; anything added after must append or it
        erases the real-file rules."""
        line = next(l for l in source("webui/runner.py").splitlines()
                    if "reply_rules()" in l)
        self.assertIn("+=", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
