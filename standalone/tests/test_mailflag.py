"""Tests for noticing new mail.

    cd standalone && python3 -m tests.test_mailflag

The agent is told not to watch the inbox, so this is what watches it. The two
things it must not do: announce a workspace's existing mail as new the first
time it is opened, and keep flagging what has already been shown.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webui import mailflag  # noqa: E402


def workspace(tmp, emails):
    ws = os.path.join(tmp, "workspace")
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, "state.json"), "w") as f:
        json.dump({"emails": emails}, f)
    return tmp


MAIL = [{"id": "e1", "from": "dana@corp.com", "subject": "Q3", "date": "2026-07-16 14:12"}]


class TestMailFlag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_workspace_opened_for_the_first_time_is_not_a_pile_of_new_mail(self):
        workspace(self.dir, MAIL)
        self.assertEqual(mailflag.check(self.dir)["count"], 0)

    def test_mail_that_arrives_after_that_is_flagged(self):
        workspace(self.dir, MAIL)
        mailflag.check(self.dir)                       # baseline
        workspace(self.dir, MAIL + [{"id": "e9", "from": "sam@corp.com",
                                     "subject": "Re: Friday", "date": "2026-07-20 09:00"}])
        got = mailflag.check(self.dir)
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["new"][0]["from"], "sam@corp.com")

    def test_it_keeps_flagging_until_it_is_seen(self):
        workspace(self.dir, MAIL)
        mailflag.check(self.dir)
        workspace(self.dir, MAIL + [{"id": "e9", "from": "sam@corp.com",
                                     "subject": "Re", "date": "2026-07-20"}])
        self.assertEqual(mailflag.check(self.dir)["count"], 1)
        self.assertEqual(mailflag.check(self.dir)["count"], 1)   # not cleared by looking
        mailflag.mark_seen(self.dir)
        self.assertEqual(mailflag.check(self.dir)["count"], 0)

    def test_newest_first_and_capped(self):
        workspace(self.dir, [])
        mailflag.check(self.dir)
        many = [{"id": f"e{i}", "from": f"p{i}@corp.com", "subject": "x",
                 "date": f"2026-07-{i:02d}"} for i in range(1, 9)]
        workspace(self.dir, many)
        got = mailflag.check(self.dir)
        self.assertEqual(got["count"], 8)
        self.assertLessEqual(len(got["new"]), 5)
        self.assertEqual(got["new"][0]["id"], "e8")

    def test_a_missing_or_broken_workspace_is_quiet_not_loud(self):
        self.assertEqual(mailflag.check(self.dir)["count"], 0)
        ws = os.path.join(self.dir, "workspace")
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "state.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(mailflag.check(self.dir)["count"], 0)

    def test_deleted_mail_does_not_come_back_as_new(self):
        workspace(self.dir, MAIL)
        mailflag.check(self.dir)
        workspace(self.dir, [])
        self.assertEqual(mailflag.check(self.dir)["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
