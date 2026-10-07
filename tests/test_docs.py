import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "docs" / "scripts" / "docs.py"


class DocsCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "docs-test@example.invalid")
        self.git("config", "user.name", "Docs Test")
        fence = chr(96) * 3
        self.guide = self.root / "docs" / "guide.MD"
        self.guide.parent.mkdir()
        self.guide.write_text(
            "---\n# Front matter fake\nfields:\n  ## Nested fake\n---\n"
            "# Start\nOverview text\n## Setup\nsetup body\n"
            "~~~~python\n# fenced fake\n" + fence + "\n## still fenced\n~~~~~\n"
            "### Install\ninstallation details\n## Other\nother body\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "untracked.md").write_text("# Hidden\n", encoding="utf-8")
        self.git("add", "docs/guide.MD")
        self.git("commit", "-qm", "fixture")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout

    def run_cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_index_tracks_only_markdown_and_parses_outline(self):
        self.guide.write_text(self.guide.read_text() + "\nchanged\n", encoding="utf-8")
        default = self.run_cli("index", "--json")
        data = json.loads(default.stdout)
        self.assertTrue(data["repository"]["dirty"])
        self.assertEqual([f["path"] for f in data["files"]], ["docs/guide.MD"])
        titles = [h["title"] for h in data["outline"]]
        self.assertEqual(titles, ["Start", "Setup", "Other"])
        setup = next(h for h in data["outline"] if h["title"] == "Setup")
        self.assertEqual(setup["section_path"], ["Start", "Setup"])
        deeper = json.loads(self.run_cli("index", "--depth", "6", "--json").stdout)
        install = next(h for h in deeper["outline"] if h["title"] == "Install")
        self.assertEqual(install["section_path"], ["Start", "Setup", "Install"])
        other = next(h for h in deeper["outline"] if h["title"] == "Other")
        self.assertEqual(setup["end_line"], other["start_line"] - 1)

    def test_search_uses_body_path_and_heading_values(self):
        text = self.guide.read_text(encoding="utf-8")
        target = "prefix-" + "x" * 800 + "NeedleAtEnd"
        self.guide.write_text(text.replace("other body", target), encoding="utf-8")
        self.git("add", "docs/guide.MD")
        self.git("commit", "-qm", "long line")
        result = self.run_cli("search", "needleatend", "--json")
        data = json.loads(result.stdout)
        match = next(m for m in data["matches"] if m["kind"] == "body")
        self.assertIn("NeedleAtEnd", match["excerpt"])
        self.assertGreater(match["character_offset"], 600)
        self.assertEqual(match["section_path"], ["Start", "Other"])
        self.assertGreaterEqual(len(json.loads(self.run_cli("search", "guide.MD", "--json").stdout)["matches"]), 1)
        empty = json.loads(self.run_cli("search", "section_path", "--json").stdout)
        self.assertEqual(empty["total_matches"], 0)
        heading = json.loads(self.run_cli("search", "Start", "--json").stdout)
        self.assertEqual(heading["matches"][0]["kind"], "heading")

    def test_read_section_ambiguity_heading_and_lines(self):
        text = self.guide.read_text(encoding="utf-8")
        self.guide.write_text(text + "\n## Setup\nsecond setup\n", encoding="utf-8")
        self.git("add", "docs/guide.MD")
        self.git("commit", "-qm", "duplicate heading")
        duplicate = self.run_cli("read", "docs/guide.MD", "--section", "Setup")
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("--heading-line", duplicate.stderr)
        heading = json.loads(self.run_cli("read", "docs/guide.MD", "--heading-line", "8", "--json").stdout)
        self.assertEqual(heading["selection"]["start_line"], 8)
        self.assertEqual(heading["ancestors"][0]["title"], "Start")
        self.assertEqual(heading["lines"][0]["text"], "---")
        self.assertEqual(heading["lines"][1]["text"], "# Front matter fake")
        exact_lines = json.loads(self.run_cli("read", "docs/guide.MD", "--lines", "2:2", "--json").stdout)
        self.assertEqual([item["line"] for item in exact_lines["lines"]], [2])
        self.assertEqual(exact_lines["lines"][0]["text"], "# Front matter fake")

    def test_symlink_and_traversal_are_rejected(self):
        link = self.root / "docs" / "link.md"
        link.symlink_to(self.guide.name)
        self.git("add", "docs/link.md")
        self.git("commit", "-qm", "tracked symlink")
        result = self.run_cli("read", "docs/link.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        outside = self.run_cli("read", "../outside.md")
        self.assertEqual(outside.returncode, 2)
        self.assertIn("relative path", outside.stderr)

    def test_long_line_has_json_and_text_continuations(self):
        path = self.root / "docs" / "long.md"
        path.write_text("# Long\n" + "z" * 5000 + "\nend\n", encoding="utf-8")
        self.git("add", "docs/long.md")
        self.git("commit", "-qm", "long line")
        result = self.run_cli("read", "docs/long.md", "--lines", "2:3", "--json", "--max-chars", "1024")
        data = json.loads(result.stdout)
        self.assertTrue(data["truncated"])
        self.assertLess(data["lines"][0]["char_end"], 5000)
        offset, recovered = data["lines"][0]["char_end"], len(data["lines"][0]["text"])
        self.assertIn(f"--column-offset {offset}", data["continuation"])
        self.assertIn("--lines 2:3", data["continuation"])
        for _ in range(10):
            resumed = json.loads(self.run_cli("read", "docs/long.md", "--lines", "2:3",
                                               "--column-offset", str(offset), "--json",
                                               "--max-chars", "1024").stdout)
            self.assertEqual(resumed["lines"][0]["char_start"], offset)
            recovered += len(resumed["lines"][0]["text"])
            if not resumed["truncated"]:
                self.assertEqual(resumed["lines"][-1]["line"], 3)
                break
            offset = resumed["lines"][0]["char_end"]
        else:
            self.fail("line continuation did not reach the end of the requested range")
        self.assertEqual(recovered, 5000)
        limited = json.loads(self.run_cli("read", "docs/long.md", "--lines", "1:3",
                                           "--limit", "1", "--json").stdout)
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["omitted_count"], 2)
        self.assertIn("--lines 2:3", limited["continuation"])
        human = self.run_cli("read", "docs/long.md", "--lines", "2:3", "--max-chars", "1024")
        self.assertLessEqual(len(human.stdout), 1024)
        self.assertIn("--column-offset", human.stdout)

    def test_json_errors_and_root_contract(self):
        result = self.run_cli("read", "missing.md", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not tracked Markdown", result.stderr)
        nested = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root / "docs"), "index"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(nested.returncode, 2)
        self.assertIn("top level", nested.stderr)
        invalid = self.run_cli("index", "--limit", "0")
        self.assertEqual(invalid.returncode, 2)


if __name__ == "__main__":
    unittest.main()
