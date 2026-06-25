import os, sys, time, subprocess, yaml, requests, logging
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
        cr = self.rules.get('commits', {})
        self.full_run_keywords = [k.upper() for k in cr.get('full_run_keywords', [])]
        self.skip_keywords = [k.upper() for k in cr.get('skip_keywords', [])]

    def print_dashboard(self):
        print("\n>>> ACTIVE RULES CONFIGURATION <<<")
        print(f"  [Paths Watched]   : {', '.join(self.watch_paths) if self.watch_paths else 'All Paths'}")
        print(f"  [Branch Always]   : {', '.join(self.always_test) if self.always_test else 'None'}")
        print(f"  [Branch Exclude]  : {', '.join(self.exclude_branches) if self.exclude_branches else 'None'}")
        print(f"  [Commit VEGA]     : {', '.join(self.full_run_keywords)}")
        print(f"  [Commit CI_SKIP]  : {', '.join(self.skip_keywords)}")
        print("-" * 60)

    def evaluate(self, branch, commit_msg, changed_files):
        branch_lower = branch.lower()
        msg_upper = commit_msg.upper()
        if any(kw in branch_lower for kw in self.exclude_branches) or any(kw in msg_upper for kw in self.skip_keywords):
            return "SKIP", "Match exclusion/skip keyword."
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
        self.known_heads = {}; self.is_first_run = True

    def get_headers(self):
        headers = {"Accept": "application/vnd.github+json"}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def discover_active_pushes(self):
        active_jobs = {}
        try:
            response = requests.get(self.api_url, headers=self.get_headers())
            if response.status_code == 200:
                events = response.json()
                push_events = [e for e in events if e['type'] == 'PushEvent']
                
                if self.is_first_run and self.verbose:
                    print(f"[+] Initial Back-Scan: Mapping latest activity from global events feed...")

                for event in reversed(push_events):
                    ref = event['payload'].get('ref', '')
                    if not ref.startswith('refs/heads/'): continue
                    branch = ref.replace('refs/heads/', '')
                    head_sha = event['payload'].get('head', '')
                    commit_msg = " ".join([c['message'] for c in event['payload'].get('commits', [])])
                    
                    if self.is_first_run:
                        if self.verbose:
                            print(f"    -> Tracked Branch Head: {branch} @ {head_sha[:7]}")
                        self.known_heads[branch] = head_sha; continue
                        
                    if self.known_heads.get(branch) != head_sha:
                        active_jobs[branch] = {'sha': head_sha, 'msg': commit_msg, 'old_sha': self.known_heads.get(branch)}
                        self.known_heads[branch] = head_sha
                
                if self.is_first_run:
                    print(f"[+] Baseline mapping complete. Tracking {len(self.known_heads)} active branch positions.")
                self.is_first_run = False
        except Exception as e: logging.warning(f"Remote check issue: {e}")
        return active_jobs

    def get_changed_files_locally(self, branch, job_data):
        try:
            subprocess.run(["git", "fetch", "origin"], cwd=self.local_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = job_data['old_sha'] if job_data['old_sha'] else "origin/main"
            res = subprocess.run(["git", "diff", "--name-only", f"{base}...origin/{branch}"], cwd=self.local_path, capture_output=True, text=True)
            if res.returncode == 0: return [line.strip() for line in res.stdout.split('\n') if line.strip()]
        except Exception as e: logging.error(f"Local diff issue: {e}")
        return []

    def process_pipeline(self):
        if self.verbose and not self.is_first_run:
            print(f"[*] Polling GitHub Events API... (Interval: {self.poll_interval}s)")
        jobs = self.discover_active_pushes()
        for branch, data in jobs.items():
            files = self.get_changed_files_locally(branch, data)
            action, reason = self.engine.evaluate(branch, data['msg'], files)
            logging.info(f"Evaluating branch '{branch}' -> Action: {action} ({reason})")
            if action == "SKIP": continue
            original_test_command = self.test_command
            if action == "FULL_RUN": self.test_command = "mesa_test test"
            print(f"\n[!] Triggering sync and evaluation pipeline for: '{branch}'")
            self.sync_and_test(branch); self.test_command = original_test_command

    def sync_and_test(self, branch):
        sandbox_path = os.path.expanduser("~/.mesa_test/work")
        try:
            subprocess.run(["git", "fetch", "origin"], cwd=self.local_path, check=True)
            subprocess.run(["git", "checkout", branch], cwd=self.local_path, check=True)
            subprocess.run(["git", "pull", "origin", branch], cwd=self.local_path, check=True)
            if os.path.exists(sandbox_path):
                subprocess.run(["git", "reset", "--hard"], cwd=sandbox_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "checkout", "--detach"], cwd=sandbox_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "fetch", "origin"], cwd=sandbox_path, check=True)
                subprocess.run(["git", "checkout", branch], cwd=sandbox_path, check=True)
                subprocess.run(["git", "reset", "--hard"], cwd=sandbox_path, check=True)
                subprocess.run("./clean && ./install", shell=True, cwd=sandbox_path, check=True)
            if self.build_submit_command: subprocess.run(self.build_submit_command, shell=True, stdout=sys.stdout, stderr=sys.stderr)
            process = subprocess.Popen(self.test_command, shell=True, stdout=sys.stdout, stderr=sys.stderr); process.communicate()
        except Exception as e: print(f"[X] Execution Failure: {e}")

    def start(self):
        validate_environment()
        print("\n============================================================\n  MESA Rules-Engine Watchdog Online\n============================================================")
        self.engine.print_dashboard()
        try:
            while True: self.process_pipeline(); time.sleep(self.poll_interval)
        except KeyboardInterrupt: sys.exit(0)

if __name__ == "__main__":
    with open('mesa_watch.yml', 'r') as f: config = yaml.safe_load(f)
    MesaRemoteWatcher(config).start()
