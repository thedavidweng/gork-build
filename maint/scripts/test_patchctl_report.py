"""Exercise report's stdout protocol through real clone and fetch paths."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class ReportJsonTest(unittest.TestCase):
    def test_fresh_clone_and_cached_fetch_emit_only_json(self):
        scripts = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream = root / "origin"
            upstream.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(upstream), *args], text=True,
                    stderr=subprocess.PIPE,
                ).strip()

            git("init")
            git("config", "user.name", "Report test")
            git("config", "user.email", "report@example.invalid")
            (upstream / "README.md").write_text("first\n")
            git("add", ".")
            git("commit", "-m", "first")
            old = git("rev-parse", "HEAD")
            control = root / "control"
            (control / "maint/scripts").mkdir(parents=True)
            shutil.copy(scripts / "upstream_diff_report.py", control / "maint/scripts")
            (control / "maint/upstream.lock.toml").write_text(
                f'repository = "{upstream}"\ncommit = "{old}"\nversion = "test"\n'
            )
            (control / "maint/sensitive-paths.txt").write_text("README.md\n")

            def report(new):
                result = subprocess.run(
                    [sys.executable, str(scripts / "patchctl.py"), "report", "--json", "--new", new],
                    cwd=control, capture_output=True, text=True, check=True,
                    # Force the explicit fetch path, not Git's lazy object fetch.
                    env={**os.environ, "GORK_CONTROL_ROOT": str(control), "GIT_NO_LAZY_FETCH": "1"},
                )
                return json.loads(result.stdout), result.stderr

            fresh, progress = report(old)
            self.assertEqual(fresh["total_files_changed"], 0)
            self.assertIn("cloning upstream", progress)
            (upstream / "README.md").write_text("second\n")
            git("commit", "-am", "second")
            new = git("rev-parse", "HEAD")
            fetched, progress = report(new)
            self.assertEqual(fetched["total_files_changed"], 1)
            self.assertIn("fetching upstream", progress)
            cached, progress = report(new)
            self.assertEqual(cached, fetched)
            self.assertNotIn("fetching upstream", progress)


if __name__ == "__main__":
    unittest.main()
