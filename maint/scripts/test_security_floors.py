"""Security floors select the intended dependency series and never downgrade."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "security_floors", Path(__file__).with_name("apply_security_floors.py")
)
floors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(floors)


class SecurityFloorTest(unittest.TestCase):
    def run_floor(self, versions, series=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "maint").mkdir()
            selection = f'series = "{series}"\n' if series else ""
            (root / "maint/security-floors.toml").write_text(
                '[[floor]]\npackage = "rand"\n' + selection + 'precise = "0.10.1"\n'
            )
            (root / "Cargo.lock").write_text("\n".join(
                f'[[package]]\nname = "rand"\nversion = "{version}"\n'
                for version in versions
            ))
            with patch.dict(os.environ, {"GORK_CONTROL_ROOT": directory,
                                         "GORK_WORK_SRC": directory}), \
                    patch.object(floors.subprocess, "run") as cargo, \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                cargo.return_value.returncode = 0
                result = floors.main()
                return result, [call.args[0] for call in cargo.call_args_list]

    def test_update_only_selected_series(self):
        self.assertEqual(self.run_floor(["0.8.5", "0.10.0"], "0.10"),
                         (0, [["cargo", "update", "-p", "rand@0.10.0",
                               "--precise", "0.10.1"]]))

    def test_caught_up_and_absent_series_are_noops(self):
        for versions in (["0.8.5", "0.10.2"], ["0.8.5"]):
            self.assertEqual(self.run_floor(versions, "0.10"), (0, []))

    def test_ambiguous_selection_still_fails(self):
        self.assertEqual(self.run_floor(["0.8.5", "0.10.0"]), (1, []))
        self.assertEqual(self.run_floor(["0.10.0", "0.10.1"], "0.10"), (1, []))


if __name__ == "__main__":
    unittest.main()
