import unittest
from unittest.mock import Mock, patch

from background_test import FAST_TESTS, run_batch


class BackgroundTests(unittest.TestCase):
    @patch("background_test.subprocess.run")
    def test_one_build_and_all_fast_cases(self, run):
        run.return_value = Mock(returncode=0)
        self.assertEqual(run_batch(), 0)
        self.assertEqual(run.call_count, 2 + len(FAST_TESTS))
        self.assertEqual(run.call_args_list[0].args[0], ["mesa_test", "install", "best"])
        self.assertEqual(run.call_args_list[1].args[0], ["mesa_test", "submit", "--empty"])
        for call, (module, name) in zip(run.call_args_list[2:], FAST_TESTS):
            self.assertEqual(call.args[0], [
                "mesa_test", "test", name, "--module", module,
                "--skip-optional", "--no-converge",
            ])

    @patch("background_test.subprocess.run")
    def test_no_work_or_install_error_stops_batch(self, run):
        for code in (3, 1):
            run.reset_mock()
            run.return_value = Mock(returncode=code)
            self.assertEqual(run_batch(), code)
            self.assertEqual(run.call_count, 1)

    @patch("background_test.subprocess.run")
    def test_submission_failure_stops_before_tests(self, run):
        run.side_effect = [Mock(returncode=0), Mock(returncode=1)]
        self.assertEqual(run_batch(), 1)
        self.assertEqual(run.call_count, 2)

    @patch("background_test.subprocess.run")
    def test_failed_case_does_not_skip_remaining_cases(self, run):
        run.side_effect = [Mock(returncode=n) for n in (0, 0, 1, 0, 0)]
        self.assertEqual(run_batch(), 1)
        self.assertEqual(run.call_count, 5)


if __name__ == "__main__":
    unittest.main()
