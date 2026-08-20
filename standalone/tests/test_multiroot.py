"""Tests for working across several real folders at once.

    cd standalone && python3 -m tests.test_multiroot

The roots stay separate sandboxes rather than being merged into one tree, so
the things worth pinning down are that a path carries which root it belongs to,
that a bare name finds the root it actually lives in, and that neither root
becomes a way out of the other.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import fs_tools  # noqa: E402
from harness.tools import TOOLS  # noqa: E402
from harness.world import ToolError  # noqa: E402


class MultiRoot(unittest.TestCase):
    def setUp(self):
        self.a = tempfile.TemporaryDirectory()
        self.b = tempfile.TemporaryDirectory()
        # realpath: on macOS a tempdir sits under /var, which is on the
        # deny-list, and /private/var is the same folder by another name.
        self.a_root = os.path.realpath(self.a.name)
        self.b_root = os.path.realpath(self.b.name)
        self.work = os.path.join(self.a_root, "work")
        self.personal = os.path.join(self.b_root, "personal")
        os.mkdir(self.work)
        os.mkdir(self.personal)
        self.saved_tools = dict(TOOLS)
        open(os.path.join(self.work, "report.txt"), "w").write("quarterly numbers")
        open(os.path.join(self.personal, "recipe.txt"), "w").write("flour")
        fs_tools.enable([self.work, self.personal])

    def tearDown(self):
        TOOLS.clear()
        TOOLS.update(self.saved_tools)
        fs_tools._ROOTS, fs_tools._LABELS, fs_tools._ROOT = [], {}, None
        self.a.cleanup()
        self.b.cleanup()

    def read(self, path):
        return TOOLS["read_file"]["run"](None, None, {"path": path})

    def test_each_root_gets_a_label_from_its_folder_name(self):
        self.assertEqual(sorted(fs_tools._LABELS), ["personal", "work"])

    def test_listing_nothing_in_particular_lists_the_roots(self):
        out = TOOLS["list_dir"]["run"](None, None, {})
        self.assertIn("work/", out)
        self.assertIn("personal/", out)

    def test_a_labelled_path_reaches_into_the_right_root(self):
        self.assertIn("quarterly", self.read("work/report.txt"))
        self.assertIn("flour", self.read("personal/recipe.txt"))

    def test_a_bare_name_finds_the_root_it_actually_lives_in(self):
        self.assertIn("flour", self.read("recipe.txt"))

    def test_paths_come_back_labelled_so_they_can_be_passed_straight_back(self):
        out = TOOLS["list_dir"]["run"](None, None, {"path": "personal"})
        self.assertTrue(out.startswith("personal/"), out)

    def test_neither_root_is_a_way_out_of_the_other(self):
        with self.assertRaises(ToolError):
            self.read("../../etc/hosts")
        with self.assertRaises(ToolError):
            self.read(os.path.join(self.a_root, "outside.txt"))

    def test_a_new_file_lands_in_the_primary_root(self):
        TOOLS["write_file"]["run"](None, None, {"path": "fresh.txt", "content": "x"})
        self.assertTrue(os.path.exists(os.path.join(self.work, "fresh.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.personal, "fresh.txt")))

    def test_duplicate_roots_collapse(self):
        fs_tools.enable([self.work, self.work])
        self.assertEqual(len(fs_tools._ROOTS), 1)

    def test_same_named_folders_still_get_distinct_labels(self):
        c = tempfile.TemporaryDirectory()
        other = os.path.join(os.path.realpath(c.name), "work")
        os.mkdir(other)
        try:
            fs_tools.enable([self.work, other])
            self.assertEqual(sorted(fs_tools._LABELS), ["work", "work2"])
        finally:
            c.cleanup()

    def test_one_root_behaves_exactly_as_before(self):
        fs_tools.enable(self.work)
        out = TOOLS["list_dir"]["run"](None, None, {"path": "."})
        self.assertNotIn("work/", out.splitlines()[0])   # no label prefix
        self.assertIn("report.txt", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
