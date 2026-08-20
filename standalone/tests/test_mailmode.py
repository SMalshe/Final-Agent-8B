"""Tests for real mail versus the practice mailbox.

    cd standalone && python3 -m tests.test_mailmode

Mail is the one simulated thing that reads exactly like the real thing, so the
rules are: use the real account whenever one is connected, never run both
mailboxes at once, and when it IS the practice office, say so every time.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import mailmode  # noqa: E402
from harness.tools import TOOLS  # noqa: E402
from harness.world import World  # noqa: E402


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.saved = dict(mailmode.CREDENTIALS)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        mailmode.CREDENTIALS.clear()
        mailmode.CREDENTIALS.update(self.saved)
        self.tmp.cleanup()

    def test_no_credentials_means_not_connected(self):
        mailmode.CREDENTIALS["gmail"] = (os.path.join(self.tmp.name, "nope.json"),)
        self.assertEqual(mailmode.connected(["gmail"]), [])
        self.assertFalse(mailmode.is_real(["gmail"]))

    def test_a_credential_file_is_what_counts_as_connected(self):
        path = os.path.join(self.tmp.name, "credentials.json")
        open(path, "w").write("{}")
        mailmode.CREDENTIALS["gmail"] = (path,)
        self.assertEqual(mailmode.connected(["gmail"]), ["gmail"])
        self.assertTrue(mailmode.is_real(["gmail"]))


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.saved = {k: dict(v) for k, v in TOOLS.items()}
        self.tmp = tempfile.TemporaryDirectory()
        self.world = World(self.tmp.name)

    def tearDown(self):
        TOOLS.clear()
        TOOLS.update(self.saved)
        self.tmp.cleanup()

    def test_real_mail_removes_the_fixture_inbox(self):
        """Both at once is how a model sends a real reply and then reads a fake
        inbox to check it."""
        mailmode.drop_simulated(TOOLS)
        for name in mailmode.SIM_MAIL_TOOLS:
            self.assertNotIn(name, TOOLS)

    def test_the_calendar_is_left_alone(self):
        """A fixture meeting is obviously a fixture. An email is not."""
        mailmode.drop_simulated(TOOLS)
        self.assertIn("list_events", TOOLS)
        self.assertIn("add_event", TOOLS)

    def test_a_simulated_send_says_so_in_its_own_result(self):
        mailmode.mark_simulated(TOOLS)
        out = TOOLS["send_email"]["run"](self.world, None,
                                         {"to": "a@b.com", "subject": "x", "body": "y"})
        self.assertIn("SIMULATED MAILBOX", out)
        self.assertIn("not a real account", out)

    def test_reading_the_fixture_inbox_says_so_too(self):
        mailmode.mark_simulated(TOOLS)
        self.assertIn("SIMULATED MAILBOX", TOOLS["list_emails"]["run"](self.world, None, {}))

    def test_marking_twice_does_not_stack_the_warning(self):
        mailmode.mark_simulated(TOOLS)
        mailmode.mark_simulated(TOOLS)
        out = TOOLS["list_emails"]["run"](self.world, None, {})
        self.assertEqual(out.count("SIMULATED MAILBOX"), 1)

    def test_the_mail_still_works_while_marked(self):
        """Saying it is practice must not stop it being usable practice."""
        mailmode.mark_simulated(TOOLS)
        TOOLS["send_email"]["run"](self.world, None,
                                   {"to": "a@b.com", "subject": "x", "body": "y"})
        self.assertEqual(len(self.world.sent_emails), 1)


class TestWiring(unittest.TestCase):
    def source(self, rel):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, rel), encoding="utf-8") as f:
            return f.read()

    def test_the_app_uses_a_connected_account_without_being_asked(self):
        src = self.source("webui/runner.py")
        self.assertIn("names = mailmode.connected()", src)

    def test_the_app_registers_every_server_tool_and_routes_them(self):
        """All sixty-nine of Outlook's tools available, the few a task needs
        exposed."""
        src = self.source("webui/runner.py")
        self.assertIn("allow_all=True", src)
        self.assertIn("agent_mod.ROUTE_TOOLS = True", src)

    def test_a_huge_registry_does_not_get_dumped_on_abstention(self):
        from harness import toolrouter
        self.assertLessEqual(toolrouter.ABSTAIN_CAP, 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
