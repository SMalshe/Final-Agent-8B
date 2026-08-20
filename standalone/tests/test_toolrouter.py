"""Tests for per-task tool routing.

    cd standalone && python3 -m tests.test_toolrouter

The router's safety argument is that being wrong is cheap: it abstains when it
has no signal, and three separate paths put a hidden tool back in view. That is
what these cover, plus the guarantee that routing OFF changes nothing.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import agent, profiles, toolrouter  # noqa: E402
from harness.memory import MemoryStore  # noqa: E402
from harness.tools import TOOLS, tool_docs  # noqa: E402
from harness.world import World  # noqa: E402


class _ScriptedLLM:
    """Replies from a fixed list, then repeats the last forever."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.output_tokens = self.prompt_tokens = 0
        self.wall = 0.0

    def chat(self, messages, **kw):
        self.system = messages[0]["content"]
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


def call(tool, **args):
    return json.dumps({"thought": "x", "tool": tool, "args": args})


# ----------------------------------------------------------------- select ---

class TestSelect(unittest.TestCase):
    def test_a_calendar_task_gets_calendar_tools_not_slide_tools(self):
        picked = toolrouter.select("find a free hour on Thursday and book it")
        self.assertIn("add_event", picked)
        self.assertIn("list_events", picked)
        self.assertNotIn("create_presentation", picked)

    def test_a_deck_task_gets_the_deck_tool(self):
        picked = toolrouter.select("build a three slide deck about the budget")
        self.assertIn("create_presentation", picked)

    def test_the_core_four_are_never_routed_away(self):
        picked = toolrouter.select("build a deck")
        for name in toolrouter.CORE:
            self.assertIn(name, picked)

    def test_no_signal_means_expose_everything(self):
        """Abstaining beats guessing: a router that hides the one tool the task
        needed is worse than no router."""
        self.assertEqual(toolrouter.select("qqq zzz"), set(TOOLS))

    def test_synonyms_reach_tools_the_task_never_names(self):
        # "book me an hour" names no tool and shares no word with add_event.
        self.assertIn("add_event", toolrouter.select("book me an hour tomorrow"))


class TestDocs(unittest.TestCase):
    def test_docs_render_only_the_named_tools(self):
        docs = tool_docs(with_examples=True, names=["done", "think"])
        self.assertIn("- done:", docs)
        self.assertNotIn("- send_email:", docs)

    def test_docs_default_to_the_whole_registry(self):
        self.assertIn("- send_email:", tool_docs(with_examples=True))


# -------------------------------------------------------------- in the loop ---

class TestRoutedRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.world = World(self.tmp.name)
        self.mem = MemoryStore(os.path.join(self.tmp.name, "m.jsonl"))
        self.saved = (agent.PROFILE, agent.MAX_CALLS, agent.ROUTE_TOOLS)
        agent.set_profile(profiles.replace(profiles.DEFAULT, plan=False, verify_rounds=0))
        agent.MAX_CALLS = 6

    def tearDown(self):
        agent.PROFILE, agent.MAX_CALLS, agent.ROUTE_TOOLS = self.saved
        toolrouter.end()
        self.tmp.cleanup()

    def run_with(self, replies, task):
        llm = _ScriptedLLM(replies)
        ep = agent.run_harness(llm, self.world, self.mem, task)
        return llm, ep

    def test_routing_off_shows_every_tool(self):
        agent.ROUTE_TOOLS = False
        llm, _ = self.run_with([call("done", summary="ok")], "book an hour")
        self.assertIn("- create_presentation:", llm.system)
        self.assertNotIn("- request_tools:", llm.system)

    def test_routing_on_hides_the_irrelevant_ones(self):
        agent.ROUTE_TOOLS = True
        llm, _ = self.run_with([call("done", summary="ok")], "book an hour on Thursday")
        self.assertIn("- add_event:", llm.system)
        self.assertNotIn("- create_presentation:", llm.system)
        self.assertIn("- request_tools:", llm.system)

    def test_request_tools_adds_them_and_the_prompt_is_rebuilt(self):
        agent.ROUTE_TOOLS = True
        llm, ep = self.run_with(
            [call("request_tools", need="build a slide deck"),
             call("done", summary="ok")],
            "book an hour on Thursday")
        # the model can actually READ the new docs, not just hold a promise.
        # (exposed() is empty by now on purpose: run_harness clears the routing
        # state on the way out, so a later run starts from the registry.)
        self.assertIn("- create_presentation:", llm.system)
        self.assertTrue(any("create_presentation" in n["content"]
                            for n in ep.transcript if n["kind"] == "observation"))

    def test_naming_a_hidden_tool_runs_it_instead_of_rejecting_it(self):
        agent.ROUTE_TOOLS = True
        llm, ep = self.run_with(
            [call("create_presentation", filename="q3.pptx", title="Q3",
                  slides=[{"title": "One", "bullets": ["a"]}]),
             call("done", summary="ok")],
            "book an hour on Thursday")
        self.assertTrue([a for a in self.world.actions
                         if a["tool"] == "create_presentation"])
        self.assertEqual(ep.invalid_calls, 0)

    def test_request_tools_leaves_the_registry_clean_afterwards(self):
        agent.ROUTE_TOOLS = True
        self.run_with([call("done", summary="ok")], "book an hour")
        self.assertNotIn("request_tools", TOOLS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
