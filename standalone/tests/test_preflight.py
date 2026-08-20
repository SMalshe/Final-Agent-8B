"""Tests for the first-run checks.

    cd standalone && python3 -m tests.test_preflight

Ollama's presence is the thing under test, so every probe here is faked. The
cases that matter are the ones a new machine actually hits: nothing installed,
installed but not running, running but empty.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webui import preflight  # noqa: E402


class Fake:
    """Stand in for the two things that touch the outside world."""

    def __init__(self, tags, binary=True):
        self.tags, self.binary = tags, binary

    def __enter__(self):
        self.saved = (preflight.installed_tags, preflight.shutil.which)
        preflight.installed_tags = lambda: self.tags
        preflight.shutil.which = lambda n: "/usr/local/bin/ollama" if self.binary else None
        return self

    def __exit__(self, *a):
        preflight.installed_tags, preflight.shutil.which = self.saved


def by_id(checks):
    return {c["id"]: c for c in checks}


class TestTags(unittest.TestCase):
    def test_a_bare_name_matches_its_latest_and_back(self):
        self.assertTrue(preflight.tag_installed("llama3.1:8b", {"llama3.1:8b": 1}))
        self.assertTrue(preflight.tag_installed("llama3.1", {"llama3.1:latest": 1}))
        self.assertFalse(preflight.tag_installed("qwen2.5:14b", {"llama3.1:8b": 1}))

    def test_nothing_installed_matches_nothing(self):
        self.assertFalse(preflight.tag_installed("llama3.1:8b", {}))
        self.assertFalse(preflight.tag_installed("llama3.1:8b", None))


class TestChecks(unittest.TestCase):
    def test_a_machine_with_nothing_is_told_to_get_ollama(self):
        with Fake(tags=None, binary=False):
            c = by_id(preflight.check("llama3.1:8b"))
        self.assertEqual(c["ollama"]["state"], "fail")
        self.assertEqual(c["ollama"]["fix"], "open_url")
        self.assertIn("ollama.com", c["ollama"]["url"])

    def test_what_cannot_be_known_yet_says_unknown_not_fine(self):
        """With the server down there is no way to tell whether the model is
        there, and reporting that as a failure sends people to re-download a
        model they already have."""
        with Fake(tags=None):
            c = by_id(preflight.check("llama3.1:8b"))
        self.assertEqual(c["server"]["state"], "fail")
        self.assertEqual(c["server"]["fix"], "start_ollama")
        self.assertEqual(c["model"]["state"], "unknown")
        self.assertIsNone(c["model"]["fix"])

    def test_running_but_empty_offers_the_download(self):
        with Fake(tags={}):
            c = by_id(preflight.check("llama3.1:8b"))
        self.assertEqual(c["server"]["state"], "ok")
        self.assertEqual(c["model"]["state"], "fail")
        self.assertEqual(c["model"]["fix"], "pull_model")
        self.assertEqual(c["model"]["tag"], "llama3.1:8b")

    def test_a_reachable_server_counts_as_installed_even_off_the_path(self):
        """Docker, or an Ollama that simply is not on this PATH. Telling someone
        to install what is plainly answering would be nonsense."""
        with Fake(tags={"llama3.1:8b": 1}, binary=False):
            c = by_id(preflight.check("llama3.1:8b"))
        self.assertEqual(c["ollama"]["state"], "ok")
        self.assertIn("not on this PATH", c["ollama"]["detail"])

    def test_a_ready_machine_is_ready(self):
        with Fake(tags={"llama3.1:8b": 1}):
            checks = preflight.check("llama3.1:8b")
        self.assertTrue(preflight.ready(checks))

    def test_a_missing_app_window_does_not_block_anything(self):
        """pywebview only decides window versus browser tab, so it warns."""
        with Fake(tags={"llama3.1:8b": 1}):
            checks = preflight.check("llama3.1:8b")
        desktop = by_id(checks)["desktop"]
        self.assertIn(desktop["state"], ("ok", "warn"))
        forced = [dict(c, state="warn") if c["id"] == "desktop" else c for c in checks]
        self.assertTrue(preflight.ready(forced))


class TestFixes(unittest.TestCase):
    def test_only_named_actions_can_run(self):
        self.assertEqual(sorted(preflight.FIXES),
                         ["install_deps", "install_optional", "pull_model", "start_ollama"])
        with self.assertRaises(ValueError):
            preflight.apply_fix("rm -rf /")
        with self.assertRaises(ValueError):
            preflight.apply_fix("")

    def test_starting_ollama_without_ollama_is_an_error_not_a_crash(self):
        saved = preflight.shutil.which
        preflight.shutil.which = lambda n: None
        try:
            with self.assertRaises(RuntimeError):
                preflight.start_ollama()
        finally:
            preflight.shutil.which = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
