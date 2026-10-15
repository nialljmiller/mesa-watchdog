import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from mesa_watch import MesaRemoteWatcher, RulesEngine


def config():
    return {
        "github_repo": "example/repo",
        "local_repo_path": ".",
        "test_command": "true",
        "rules": {},
    }


def push(branch, sha, message="change"):
    return {
        "type": "PushEvent",
        "payload": {
            "ref": f"refs/heads/{branch}",
            "head": sha,
            "commits": [{"message": message}],
        },
    }


class WatcherTests(unittest.TestCase):
    @patch("mesa_watch.requests.get")
    def test_historical_events_do_not_retrigger_unchanged_branch(self, get):
        response = Mock()
        response.json.return_value = [push("topic", "new"), push("topic", "old")]
        response.raise_for_status.return_value = None
        get.return_value = response
        watcher = MesaRemoteWatcher(config())

        self.assertEqual(watcher.discover_active_pushes(), {})
        self.assertEqual(watcher.known_heads, {"topic": "new"})
        self.assertEqual(watcher.discover_active_pushes(), {})
        get.assert_called_with(watcher.api_url, headers=watcher.get_headers(), timeout=30)

    @patch("mesa_watch.requests.get")
    def test_new_head_is_returned_once(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = [[push("topic", "one")], [push("topic", "two")]]
        get.return_value = response
        watcher = MesaRemoteWatcher(config())

        watcher.discover_active_pushes()
        jobs = watcher.discover_active_pushes()
        self.assertEqual(jobs["topic"]["old_sha"], "one")
        self.assertEqual(jobs["topic"]["sha"], "two")

    def test_rule_precedence(self):
        engine = RulesEngine({
            "watch_paths": ["colors/"],
            "branches": {"always_test_keywords": ["force"], "exclude_keywords": ["wip"]},
            "commits": {"full_run_keywords": ["VEGA"], "skip_keywords": ["CI_SKIP"]},
        })
        self.assertEqual(engine.evaluate("wip-force", "VEGA", ["colors/a"])[0], "SKIP")
        self.assertEqual(engine.evaluate("topic", "VEGA", ["docs/a"])[0], "FULL_RUN")
        self.assertEqual(engine.evaluate("force-me", "normal", ["docs/a"])[0], "STANDARD_RUN")

    def test_skip_markers_override_forced_branch(self):
        engine = RulesEngine({
            "branches": {"always_test_keywords": ["filter_interp"]},
            "commits": {"skip_keywords": ["CI_SKIP"]},
        })
        for message in ("CI_SKIP change", "[ci skip] change", "[skip ci] change"):
            with self.subTest(message=message):
                self.assertEqual(engine.evaluate("filter_interp_fix", message, ["colors/a"])[0], "SKIP")

    @patch("mesa_watch.requests.get")
    def test_current_push_payload_fetches_missing_commit_message(self, get):
        event = push("topic", "new")
        del event["payload"]["commits"]
        events = Mock()
        events.json.return_value = [event]
        commit = Mock()
        commit.json.return_value = {"commit": {"message": "[ci skip] docs"}}
        get.side_effect = [events, commit]
        watcher = MesaRemoteWatcher(config())
        watcher.is_first_run = False
        watcher.known_heads = {"topic": "old"}
        self.assertEqual(watcher.discover_active_pushes()["topic"]["msg"], "[ci skip] docs")
        get.assert_called_with("https://api.github.com/repos/example/repo/commits/new",
                               headers=watcher.get_headers(), timeout=30)
        self.assertEqual(watcher.known_heads["topic"], "new")

    def test_author_or_path_overrides_skip(self):
        engine = RulesEngine({
            "watch_paths": ["colors/", "star/test_suite/custom_colors/"],
            "ignore_skip_authors": ["nialljmiller"],
            "commits": {"skip_keywords": ["CI_SKIP"]},
        })
        for message in ("[ci skip] update", "CI_SKIP update", "[skip ci] update"):
            for author, files, expected in (
                ("nialljmiller", ["docs/a"], "STANDARD_RUN"),
                ("someone_else", ["colors/private/a.f90"], "STANDARD_RUN"),
                ("someone_else", ["star/test_suite/custom_colors/inlist"], "STANDARD_RUN"),
                ("someone_else", ["docs/a"], "SKIP"),
                ("", ["docs/a"], "SKIP"),
                ("someone_else", ["colors_extra/a"], "SKIP"),
            ):
                with self.subTest(message=message, author=author, files=files):
                    self.assertEqual(engine.evaluate("topic", message, files, author)[0], expected)

    @patch("mesa_watch.requests.get")
    def test_commit_author_is_resolved_not_push_actor(self, get):
        event = push("topic", "new", "[ci skip] legacy payload")
        event["actor"] = {"login": "someone_else"}
        events = Mock()
        events.json.return_value = [event]
        commit = Mock()
        commit.json.return_value = {
            "commit": {"message": "[ci skip] update"},
            "author": {"login": "nialljmiller"},
        }
        get.side_effect = [events, commit]
        cfg = config()
        cfg["rules"] = {"ignore_skip_authors": ["nialljmiller"]}
        watcher = MesaRemoteWatcher(cfg)
        watcher.is_first_run = False
        watcher.known_heads = {"topic": "old"}
        self.assertEqual(watcher.discover_active_pushes()["topic"]["author_login"], "nialljmiller")

    @patch("mesa_watch.logging.warning")
    @patch("mesa_watch.requests.get")
    def test_commit_message_failure_retries_push(self, get, _warning):
        event = push("topic", "new")
        del event["payload"]["commits"]
        events = Mock()
        events.json.return_value = [event]
        commit = Mock()
        commit.json.return_value = {}
        get.side_effect = [events, commit]
        watcher = MesaRemoteWatcher(config())
        watcher.is_first_run = False
        watcher.known_heads = {"topic": "old"}
        self.assertEqual(watcher.discover_active_pushes(), {})
        self.assertEqual(watcher.known_heads["topic"], "old")

    @patch("mesa_watch.logging.error")
    def test_diff_failure_leaves_push_eligible_for_retry(self, _error):
        watcher = MesaRemoteWatcher(config())
        watcher.is_first_run = False
        watcher.known_heads = {"topic": "new"}
        watcher.discover_active_pushes = Mock(return_value={
            "topic": {"sha": "new", "msg": "change", "old_sha": "old"}
        })
        watcher.get_changed_files_locally = Mock(return_value=None)

        watcher.process_pipeline()

        self.assertEqual(watcher.known_heads["topic"], "old")

    @patch("mesa_watch.subprocess.run")
    def test_dispatcher_nothing_to_do_is_not_an_error(self, run):
        cfg = config()
        cfg["dispatch_command"] = "mesa_test install_and_test best"
        watcher = MesaRemoteWatcher(cfg)
        run.return_value.returncode = 3
        with patch("mesa_watch.logging.error") as error:
            watcher.dispatch_idle_work()
            error.assert_not_called()
        self.assertEqual(run.call_args.args[0], cfg["dispatch_command"])

    @patch("mesa_watch.time.time")
    @patch("mesa_watch.subprocess.run")
    def test_dispatch_interval_survives_restart_and_includes_empty_queue(self, run, clock):
        cfg = config()
        cfg.update(dispatch_command="mesa_test install_and_test best",
                   dispatch_interval_seconds=18000)
        run.return_value.returncode = 3
        with tempfile.TemporaryDirectory() as directory:
            cfg["dispatch_state_path"] = str(Path(directory) / "last_dispatch")
            watcher = MesaRemoteWatcher(cfg)
            clock.return_value = 100000
            watcher.dispatch_idle_work()
            self.assertEqual(run.call_count, 1)
            clock.return_value = 117999
            watcher.dispatch_idle_work()
            restarted = MesaRemoteWatcher(cfg)
            restarted.dispatch_idle_work()
            self.assertEqual(run.call_count, 1)
            clock.return_value = 118000
            restarted.dispatch_idle_work()
            self.assertEqual(run.call_count, 2)
            run.return_value.returncode = 1
            clock.return_value = 136000
            with patch("mesa_watch.logging.error"):
                restarted.dispatch_idle_work()
            restarted.dispatch_idle_work()
            self.assertEqual(run.call_count, 3)

    def test_skipped_colors_push_leaves_worker_available(self):
        cfg = config()
        cfg["rules"] = {"commits": {"skip_keywords": ["CI_SKIP"]}}
        watcher = MesaRemoteWatcher(cfg)
        watcher.discover_active_pushes = Mock(return_value={
            "topic": {"sha": "new", "msg": "[ci skip] docs", "old_sha": "old"}
        })
        watcher.get_changed_files_locally = Mock(return_value=["colors/a"])
        watcher.sync_and_test = Mock()
        self.assertFalse(watcher.process_pipeline())
        watcher.sync_and_test.assert_not_called()

    @patch("mesa_watch.logging.info")
    def test_pipeline_uses_exact_push_sha_and_retries_failure(self, _info):
        watcher = MesaRemoteWatcher(config())
        watcher.is_first_run = False
        watcher.known_heads = {"topic": "new"}
        watcher.discover_active_pushes = Mock(return_value={
            "topic": {"sha": "new", "msg": "change", "old_sha": "old"}
        })
        watcher.get_changed_files_locally = Mock(return_value=["colors/a"])
        watcher.sync_and_test = Mock(return_value=False)
        self.assertTrue(watcher.process_pipeline())
        watcher.sync_and_test.assert_called_once_with("new", detached=True)
        self.assertEqual(watcher.known_heads["topic"], "old")

    @patch("mesa_watch.logging.info")
    @patch("mesa_watch.subprocess.run")
    def test_manual_commit_resolves_and_tests_exact_sha(self, run, _info):
        full_sha = "a" * 40
        run.side_effect = [Mock(returncode=0), Mock(returncode=0, stdout=full_sha + "\n")]
        watcher = MesaRemoteWatcher(config())
        watcher.sync_and_test = Mock(return_value=True)

        self.assertTrue(watcher.test_commit("abcdef1"))
        watcher.sync_and_test.assert_called_once_with(full_sha, detached=True)

    @patch("mesa_watch.logging.error")
    def test_manual_commit_rejects_non_sha_input(self, _error):
        watcher = MesaRemoteWatcher(config())
        watcher.sync_and_test = Mock()

        self.assertFalse(watcher.test_commit("origin/main"))
        watcher.sync_and_test.assert_not_called()

    @patch("mesa_watch.os.path.exists", return_value=True)
    @patch("mesa_watch.subprocess.run")
    def test_detached_run_does_not_checkout_active_repository(self, run, _exists):
        watcher = MesaRemoteWatcher(config())

        self.assertTrue(watcher.sync_and_test("a" * 40, detached=True))
        self.assertFalse(any(call.kwargs.get("cwd") == watcher.local_path for call in run.call_args_list))
        self.assertEqual(run.call_args_list[0].args[0], ["mesa_test", "install", "a" * 40])


if __name__ == "__main__":
    unittest.main()
