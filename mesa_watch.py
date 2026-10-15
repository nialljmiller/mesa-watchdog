import argparse, os, re, sys, time, subprocess, yaml, requests, logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[RotatingFileHandler("watchdog.log", maxBytes=1024*1024*5, backupCount=2), logging.StreamHandler(sys.stdout)])

def validate_environment():
    if subprocess.call("command -v mesa_test", shell=True, stdout=subprocess.DEVNULL) != 0:
        logging.error("mesa_test not found in $PATH. Source your environment."); sys.exit(1)

class RulesEngine:
    def __init__(self, config_rules):
        self.rules = config_rules or {}
        self.watch_paths = self.rules.get('watch_paths', [])
        br = self.rules.get('branches', {})
        self.always_test = [b.lower() for b in br.get('always_test_keywords', [])]
        self.exclude_branches = [b.lower() for b in br.get('exclude_keywords', [])]
        self.ignore_skip_authors = [a.lower() for a in self.rules.get('ignore_skip_authors', [])]
        cr = self.rules.get('commits', {})
        self.full_run_keywords = [k.upper() for k in cr.get('full_run_keywords', [])]
        self.skip_keywords = [k.upper() for k in cr.get('skip_keywords', [])]
        # Preserve the legacy marker while recognizing GitHub's bracketed forms.
        if 'CI_SKIP' in self.skip_keywords:
            self.skip_keywords.extend(['[CI SKIP]', '[SKIP CI]'])

    def print_dashboard(self):
        print("\n>>> ACTIVE RULES CONFIGURATION <<<")
        print(f"  [Paths Watched]   : {', '.join(self.watch_paths) if self.watch_paths else 'All Paths'}")
        print(f"  [Branch Always]   : {', '.join(self.always_test) if self.always_test else 'None'}")
        print(f"  [Branch Exclude]  : {', '.join(self.exclude_branches) if self.exclude_branches else 'None'}")
        print(f"  [Commit VEGA]     : {', '.join(self.full_run_keywords)}")
        print(f"  [Commit CI_SKIP]  : {', '.join(self.skip_keywords)}")
        print("-" * 60)

    def evaluate(self, branch, commit_msg, changed_files, author_login=""):
        branch_lower = branch.lower()
        msg_upper = commit_msg.upper()
        if any(kw in branch_lower for kw in self.exclude_branches):
            return "SKIP", "Match branch exclusion keyword."
        ignore_skip = (author_login.lower() in self.ignore_skip_authors or
                       any(f.startswith(p) for f in changed_files for p in self.watch_paths))
        if any(kw in msg_upper for kw in self.skip_keywords):
            if not ignore_skip:
                return "SKIP", "Match commit skip keyword."
            return "STANDARD_RUN", "CI-skip overridden by commit author or watched path."
        if any(kw in msg_upper for kw in self.full_run_keywords):
            return "FULL_RUN", "Match VEGA explicit keyword."
        if any(kw in branch_lower for kw in self.always_test):
            return "STANDARD_RUN", "Match explicit branch override keyword."
        if self.watch_paths:
            matched = [f for f in changed_files if any(f.startswith(p) for p in self.watch_paths)]
            if matched: return "STANDARD_RUN", f"Modifications inside watched paths ({len(matched)} files)."
            return "SKIP", "No modifications inside watched paths."
        return "STANDARD_RUN", "Default fallback acceptance."

class MesaRemoteWatcher:
    def __init__(self, config):
        self.config = config; self.repo = config.get('github_repo')
        self.local_path = os.path.abspath(os.path.expandvars(config.get('local_repo_path')))
        self.build_submit_command = config.get('build_submit_command', '')
        self.test_command = config.get('test_command'); self.poll_interval = config.get('poll_interval_seconds', 60)
        self.token = config.get('github_token', ''); self.api_url = f"https://api.github.com/repos/{self.repo}/events"
        self.engine = RulesEngine(config.get('rules'))
        self.verbose = config.get('verbose', False)
        self.dispatch_command = config.get('dispatch_command', '')
        self.dispatch_interval = float(config.get('dispatch_interval_seconds', 0))
        self.dispatch_state_path = config.get('dispatch_state_path')
        self.last_dispatch_at = None
        if self.dispatch_state_path:
            self.dispatch_state_path = Path(self.dispatch_state_path).expanduser()
            try:
                self.last_dispatch_at = float(self.dispatch_state_path.read_text())
            except FileNotFoundError:
                pass
        self.known_heads = {}; self.is_first_run = True

    def get_headers(self):
        headers = {"Accept": "application/vnd.github+json"}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def discover_active_pushes(self):
        active_jobs = {}
        try:
            response = requests.get(self.api_url, headers=self.get_headers(), timeout=30)
            response.raise_for_status()
            events = response.json()
            push_events = [e for e in events if e.get('type') == 'PushEvent']

            # The events endpoint returns newest events first and can contain
            # several pushes for the same branch. Keep only the newest head.
            # Replaying every historical event made an unchanged branch look
            # changed on every poll.
            latest_push_by_branch = {}
            for event in push_events:
                payload = event.get('payload', {})
                ref = payload.get('ref', '')
                if not ref.startswith('refs/heads/'):
                    continue
                branch = ref.removeprefix('refs/heads/')
                if branch not in latest_push_by_branch:
                    latest_push_by_branch[branch] = event
                
            if self.is_first_run and self.verbose:
                print("[+] Initial Back-Scan: Mapping latest activity from global events feed...")

            for branch, event in latest_push_by_branch.items():
                payload = event.get('payload', {})
                head_sha = payload.get('head', '')
                if not head_sha:
                    logging.warning("Ignoring push for '%s' without a head SHA.", branch)
                    continue
                commit_msg = " ".join(c.get('message', '') for c in payload.get('commits', []))

                if self.is_first_run:
                    if self.verbose:
                        print(f"    -> Tracked Branch Head: {branch} @ {head_sha[:7]}")
                    self.known_heads[branch] = head_sha
                    continue

                old_sha = self.known_heads.get(branch)
                if old_sha != head_sha:
                    author_login = ""
                    if not commit_msg or self.engine.ignore_skip_authors:
                        try:
                            commit_response = requests.get(
                                f'https://api.github.com/repos/{self.repo}/commits/{head_sha}',
                                headers=self.get_headers(), timeout=30,
                            )
                            commit_response.raise_for_status()
                            commit_data = commit_response.json()
                            commit_msg = commit_data['commit']['message']
                            author_login = (commit_data.get('author') or {}).get('login', '')
                        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                            logging.warning("Cannot read commit message for '%s': %s", branch, e)
                            # Keep the old head so the next poll retries this push.
                            continue
                    active_jobs[branch] = {'sha': head_sha, 'msg': commit_msg, 'old_sha': old_sha, 'author_login': author_login}
                    self.known_heads[branch] = head_sha

            if self.is_first_run:
                print(f"[+] Baseline mapping complete. Tracking {len(self.known_heads)} active branch positions.")
            self.is_first_run = False
        except (requests.RequestException, ValueError) as e:
            logging.warning(f"Remote check issue: {e}")
        return active_jobs

    def get_changed_files_locally(self, branch, job_data):
        try:
            subprocess.run(["git", "fetch", "origin"], cwd=self.local_path, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = job_data['old_sha'] if job_data['old_sha'] else "origin/main"
            res = subprocess.run(["git", "diff", "--name-only", f"{base}...{job_data['sha']}"], cwd=self.local_path, capture_output=True, text=True)
            res.check_returncode()
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
        except Exception as e: logging.error(f"Local diff issue: {e}")
        return None

    def process_pipeline(self):
        if self.verbose and not self.is_first_run:
            print(f"[*] Polling GitHub Events API... (Interval: {self.poll_interval}s)")
        jobs = self.discover_active_pushes()
        attempted_run = False
        for branch, data in jobs.items():
            files = self.get_changed_files_locally(branch, data)
            if files is None:
                logging.error("Skipping branch '%s' because its changed files could not be determined.", branch)
                # Do not lose this push because of a transient fetch/diff
                # failure. Restoring the old head makes the next poll retry it.
                if data['old_sha'] is None:
                    self.known_heads.pop(branch, None)
                else:
                    self.known_heads[branch] = data['old_sha']
                continue
            action, reason = self.engine.evaluate(branch, data['msg'], files, data.get('author_login', ''))
            logging.info(f"Evaluating branch '{branch}' -> Action: {action} ({reason})")
            if action == "SKIP": continue
            attempted_run = True
            original_test_command = self.test_command
            try:
                if action == "FULL_RUN": self.test_command = "mesa_test test"
                print(f"\n[!] Triggering sync and evaluation pipeline for: '{branch}'")
                if not self.sync_and_test(data['sha'], detached=True):
                    # A failed build/submission is not a completed push.
                    if data['old_sha'] is None:
                        self.known_heads.pop(branch, None)
                    else:
                        self.known_heads[branch] = data['old_sha']
            finally:
                self.test_command = original_test_command
        return attempted_run

    def dispatch_idle_work(self):
        if not self.dispatch_command:
            return
        now = time.time()
        if self.last_dispatch_at is not None and now - self.last_dispatch_at < self.dispatch_interval:
            return
        # Record attempts too, so failures or an empty queue cannot cause rapid retries.
        if self.dispatch_state_path:
            self.dispatch_state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.dispatch_state_path.with_suffix('.tmp')
            temporary.write_text(str(now))
            temporary.replace(self.dispatch_state_path)
        self.last_dispatch_at = now
        logging.info("Starting scheduled TestHub dispatcher (interval %ss).", self.dispatch_interval)
        # Serialize with Colors jobs: both commands own the same mesa_test worktree.
        result = subprocess.run(self.dispatch_command, shell=True,
                                stdout=sys.stdout, stderr=sys.stderr)
        if result.returncode not in (0, 3):
            logging.error("TestHub dispatcher exited with status %s", result.returncode)

    def sync_and_test(self, revision, detached=False):
        target_name = f"commit {revision}" if detached else f"branch '{revision}'"
        try:
            # Let mesa_test own its configured mirror/worktree and record build
            # modes and claims. Never checkout or pull the development repo.
            subprocess.run(["mesa_test", "install", revision], check=True)
            if self.build_submit_command:
                subprocess.run(self.build_submit_command, shell=True, check=True,
                               stdout=sys.stdout, stderr=sys.stderr)
            subprocess.run(self.test_command, shell=True, check=True,
                           stdout=sys.stdout, stderr=sys.stderr)
            return True
        except Exception as e:
            logging.error(f"Execution failure for {target_name}: {e}")
            return False

    def test_commit(self, commit):
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
            logging.error("Invalid commit ID '%s'. Expected a 7-40 character hexadecimal SHA.", commit)
            return False

        try:
            subprocess.run(["git", "fetch", "origin"], cwd=self.local_path, check=True)
            resolved = subprocess.run(
                ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
                cwd=self.local_path, check=True, capture_output=True, text=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            logging.error("Commit '%s' was not found after fetching origin.", commit)
            return False

        logging.info("Starting manual test for commit %s.", resolved)
        return self.sync_and_test(resolved, detached=True)

    def start(self):
        validate_environment()
        print("\n============================================================\n  MESA Rules-Engine Watchdog Online\n============================================================")
        self.engine.print_dashboard()
        try:
            while True:
                if not self.process_pipeline():
                    self.dispatch_idle_work()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt: sys.exit(0)

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Watch MESA pushes or manually test one commit.")
    parser.add_argument("--commit", metavar="SHA", help="test one exact commit and exit")
    parser.add_argument("--config", default="mesa_watch.yml", help="configuration file (default: mesa_watch.yml)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)
    watcher = MesaRemoteWatcher(config)
    if args.commit:
        validate_environment()
        return 0 if watcher.test_commit(args.commit) else 1
    watcher.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
