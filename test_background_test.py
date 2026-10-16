import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from background_test import FAST_TESTS, run_one


class BackgroundTests(unittest.TestCase):
    @patch("background_test.subprocess.run")
    def test_one_case_per_invocation_rotates_across_restarts(self, run):
        run.return_value = Mock(returncode=0)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "index"
            for module, name in FAST_TESTS + FAST_TESTS[:1]:
                run.reset_mock()
                self.assertEqual(run_one(state), 0)
                self.assertEqual(run.call_count, 3)
                self.assertEqual(run.call_args_list[0].args[0], ["mesa_test", "install", "best"])
                self.assertEqual(run.call_args_list[1].args[0], ["mesa_test", "submit", "--empty"])
                self.assertEqual(run.call_args.args[0], [
                    "mesa_test", "test", name, "--module", module,
                    "--skip-optional", "--no-converge",
                ])

    @patch("background_test.subprocess.run")
    def test_no_work_or_install_error_does_not_run_test(self, run):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "index"
            for code in (3, 1):
                run.reset_mock()
                run.return_value = Mock(returncode=code)
                self.assertEqual(run_one(state), code)
                self.assertEqual(run.call_count, 1)
                self.assertFalse(state.exists())

    @patch("background_test.subprocess.run")
    def test_submission_failure_stops_before_test(self, run):
        run.side_effect = [Mock(returncode=0), Mock(returncode=1)]
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(run_one(Path(directory) / "index"), 1)
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
