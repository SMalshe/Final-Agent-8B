"""Tests for "waiting is not a step".

    cd standalone && python3 -m tests.test_wait_guard

Observed live: asked to email someone about a meeting, an 8B sent it and then
checked the inbox six times for a reply. The loop had licensed that itself -
the send moved world_version, and a moved version is how the loop says "that
call may return something new now", so every earlier inbox read got a fresh
repeat budget. Sending mail cannot put mail in your inbox.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import agent, profiles  # noqa: E402
from harness.memory import MemoryStore  # noqa: E402
from harness.world import World  # noqa: E402


class _Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.output_tokens = self.prompt_tokens = 0
        self.wall = 0.0

    def chat(self, messages, **kw):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


def call(tool, **args):
    return json.dumps({"thought": "x", "tool": tool, "args": args})


class TestWaitGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.world = World(self.tmp.name)
        self.mem = MemoryStore(os.path.join(self.tmp.name, "m.jsonl"))
        self.saved = (agent.PROFILE, agent.MAX_CALLS, agent.WAIT_GUARD, agent.EXTRA_RULES)
        agent.set_profile(profiles.replace(profiles.DEFAULT, plan=False, verify_rounds=0,
                                           repeat_limit=1))
        agent.MAX_CALLS = 12

    def tearDown(self):
        agent.PROFILE, agent.MAX_CALLS, agent.WAIT_GUARD, agent.EXTRA_RULES = self.saved
        self.tmp.cleanup()

    def poll_after_send(self):
        """Send, then check the inbox over and over - the observed failure."""
        return [call("send_email", to="sam@corp.com", subject="Meeting", body="Free Friday?"),
                call("list_emails"), call("list_emails"), call("list_emails"),
                call("list_emails"), call("done", summary="sent")]

    def executed(self, name):
        return [a for a in self.world.actions if a["tool"] == name]

    def poll_with_different_calls(self):
        """The failure as observed: the polls are not identical, so the repeat
        budget never applies to them."""
        return [call("send_email", to="sam@corp.com", subject="Meeting", body="Free Friday?"),
                call("list_emails"), call("read_email", id="e1"),
                call("read_email", id="e2"), call("list_emails"),
                call("done", summary="sent")]

    def test_without_the_guard_the_poll_runs_unchecked(self):
        agent.WAIT_GUARD = False
        agent.run_harness(_Scripted(self.poll_with_different_calls()), self.world,
                          self.mem, "ask Sam about Friday")
        looks = len(self.executed("list_emails")) + len(self.executed("read_email"))
        self.assertGreaterEqual(looks, 3)

    def test_the_guard_allows_one_look_after_sending_and_no_more(self):
        agent.WAIT_GUARD = True
        agent.run_harness(_Scripted(self.poll_with_different_calls()), self.world,
                          self.mem, "ask Sam about Friday")
        looks = len(self.executed("list_emails")) + len(self.executed("read_email"))
        self.assertEqual(looks, 1)

    def test_looking_before_sending_is_untouched(self):
        """Reading the inbox to do the work is the job; the guard only starts
        once something has gone out."""
        agent.WAIT_GUARD = True
        script = [call("list_emails"), call("read_email", id="e1"),
                  call("read_email", id="e2"), call("done", summary="ok")]
        agent.run_harness(_Scripted(script), self.world, self.mem, "read my mail")
        self.assertEqual(len(self.executed("read_email")), 2)

    def test_with_the_guard_it_does_not(self):
        agent.WAIT_GUARD = True
        agent.run_harness(_Scripted(self.poll_after_send()), self.world, self.mem,
                          "ask Sam about Friday")
        self.assertEqual(len(self.executed("list_emails")), 1)

    def test_the_refusal_says_why_rather_than_just_no(self):
        agent.WAIT_GUARD = True
        ep = agent.run_harness(_Scripted(self.poll_with_different_calls()), self.world,
                               self.mem, "ask Sam about Friday")
        feedback = " ".join(n["content"] for n in ep.transcript if n["kind"] == "feedback")
        self.assertIn("nothing new arrives while you are working", feedback.lower())
        self.assertIn("call done", feedback.lower())

    def test_a_write_that_could_change_the_inbox_still_grants_a_fresh_look(self):
        """The guard is about outbound writes only. Anything that genuinely
        changes the world still resets the budget, or the loop would start
        refusing legitimate re-reads."""
        agent.WAIT_GUARD = True
        script = [call("list_emails"),
                  call("add_event", title="Deep work", date="2026-07-23",
                       start_time="14:00", end_time="15:00"),
                  call("list_emails"),
                  call("done", summary="ok")]
        agent.run_harness(_Scripted(script), self.world, self.mem, "book an hour")
        self.assertEqual(len(self.executed("list_emails")), 2)

    def test_the_rule_text_says_it_plainly(self):
        self.assertIn("Nothing new arrives while you are working", agent.WAIT_RULES)
        self.assertIn("not watching the inbox between runs", agent.WAIT_RULES)

    def test_both_runners_turn_it_on(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("webui/runner.py", "agents/8b/run_agent.py"):
            with open(os.path.join(here, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("WAIT_GUARD = True", src, rel)
            self.assertIn("agent_mod.WAIT_RULES", src, rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
