"""Tests for the sent folder and the drafts folder.

    cd standalone && python3 -m tests.test_mailbox

The world kept sent mail all along with no tool able to read it, and had no
drafts at all. What matters here is that both survive a restart, that an older
state.json still loads, and that the agent cannot send a draft - composing is
its half, sending is the person's.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import mailbox  # noqa: E402
from harness.tools import TOOLS  # noqa: E402
from harness.world import ToolError, World  # noqa: E402


class TestFolders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = World(self.tmp.name, persistent=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_what_was_sent_can_be_read_back(self):
        self.w.send_email("dana@corp.com", "Re: numbers", "Got it, thanks!")
        row = self.w.list_sent()[0]
        self.assertEqual((row["to"], row["subject"]), ("dana@corp.com", "Re: numbers"))
        self.assertIn("Got it", row["preview"])

    def test_a_long_body_is_previewed_not_dumped(self):
        self.w.send_email("dana@corp.com", "Long", "word " * 500)
        self.assertLessEqual(len(self.w.list_sent()[0]["preview"]), 120)

    def test_a_draft_is_kept_and_readable_in_full(self):
        made = self.w.save_draft("sam@corp.com", "Billing", "Any update on the webhook?")
        self.assertEqual(made["id"], "d1")
        self.assertEqual(self.w.list_drafts(), [{"id": "d1", "to": "sam@corp.com",
                                                 "subject": "Billing"}])
        self.assertIn("webhook", self.w.read_draft("d1")["body"])

    def test_a_draft_is_not_a_sent_email(self):
        self.w.save_draft("sam@corp.com", "Billing", "Any update?")
        self.assertEqual(self.w.list_sent(), [])

    def test_an_unknown_draft_id_says_how_to_find_a_real_one(self):
        with self.assertRaises(ToolError) as e:
            self.w.read_draft("d9")
        self.assertIn("list_drafts", str(e.exception))

    def test_a_bad_recipient_is_refused(self):
        with self.assertRaises(ToolError):
            self.w.save_draft("", "Billing", "Any update?")

    def test_both_folders_survive_a_restart(self):
        self.w.send_email("dana@corp.com", "Re: numbers", "Got it")
        self.w.save_draft("sam@corp.com", "Billing", "Any update?")
        self.w.snapshot()
        later = World(self.tmp.name, persistent=True)
        self.assertEqual(len(later.list_sent()), 1)
        self.assertEqual(len(later.list_drafts()), 1)

    def test_a_state_file_written_before_drafts_existed_still_loads(self):
        with open(os.path.join(self.tmp.name, "state.json"), "w") as f:
            json.dump({"emails": [], "sent_emails": [], "events": [],
                       "messages": [], "reminders": []}, f)
        self.assertEqual(World(self.tmp.name, persistent=True).list_drafts(), [])


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.saved = dict(TOOLS)

    def tearDown(self):
        TOOLS.clear()
        TOOLS.update(self.saved)

    def test_the_tools_are_opt_in_so_bench_keeps_its_registry(self):
        for name in mailbox.SPECS:
            self.assertNotIn(name, TOOLS)
        mailbox.enable()
        for name in mailbox.SPECS:
            self.assertIn(name, TOOLS)
        mailbox.disable()
        for name in mailbox.SPECS:
            self.assertNotIn(name, TOOLS)

    def test_there_is_no_way_for_the_agent_to_send_a_draft(self):
        """Composing is its half. An agent that can draft and then send has
        sent mail with extra steps."""
        mailbox.enable()
        self.assertNotIn("send_draft", TOOLS)
        for name, spec in mailbox.SPECS.items():
            self.assertNotIn("send", name)

    def test_they_sit_with_the_mail_tools_not_after_done(self):
        """Regression, found live: appended to the registry they rendered AFTER
        done in the docs, so the plan for "who have I emailed recently?"
        reached for list_emails, never saw a sent folder past the finish tool,
        and answered out of the inbox. Twice."""
        mailbox.enable()
        order = list(TOOLS)
        self.assertEqual(order[order.index("send_email") + 1], "list_sent")
        self.assertLess(order.index("list_drafts"), order.index("done"))

    def test_enabling_twice_does_not_duplicate_or_reshuffle(self):
        mailbox.enable()
        first = list(TOOLS)
        mailbox.enable()
        self.assertEqual(first, list(TOOLS))

    def test_they_still_arrive_when_there_is_no_send_email(self):
        """Real-file mode drops the simulated office tools."""
        TOOLS.pop("send_email", None)
        mailbox.enable()
        for name in mailbox.SPECS:
            self.assertIn(name, TOOLS)

    def test_saving_a_draft_counts_as_a_write(self):
        """Loop-breaking has to know, or an identical draft saved twice against
        an unchanged world reads as a retry."""
        self.assertEqual(mailbox.WRITE_TOOLS, {"save_draft"})

    def test_both_runners_enable_them(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("webui/runner.py", "agents/8b/run_agent.py"):
            with open(os.path.join(here, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("mailbox.enable()", src, rel)
            self.assertIn("mailbox.WRITE_TOOLS", src, rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
