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

STANDALONE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source(rel):
    with open(os.path.join(STANDALONE, rel), encoding="utf-8") as f:
        return f.read()


class TestVoice(unittest.TestCase):
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


class TestScope(unittest.TestCase):
    def test_the_graded_prompt_does_not_carry_it(self):
        self.assertNotIn("HOW TO WRITE THE SUMMARY", agent.HARNESS_SYSTEM)
        self.assertNotIn("HOW TO WRITE THE SUMMARY", agent.RAW_SYSTEM)
        self.assertEqual(agent.EXTRA_RULES, "")   # empty until a runner sets it

    def test_the_chat_surface_applies_it_and_the_cli_does_not(self):
        self.assertIn("chat.reply_rules()", source("webui/runner.py"))
        self.assertNotIn("reply_rules", source("agents/8b/run_agent.py"))

    def test_it_is_appended_so_root_and_mcp_rules_survive(self):
        """--root ASSIGNS EXTRA_RULES; anything added after must append or it
        erases the real-file rules."""
        line = next(l for l in source("webui/runner.py").splitlines()
                    if "reply_rules()" in l)
        self.assertIn("+=", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
