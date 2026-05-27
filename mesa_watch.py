import os
import sys
import time
import subprocess
import yaml
import requests

class MesaRemoteWatcher:
    
    def __init__(self, config):
        self.config = config
        self.repo = config.get('github_repo')
        self.local_path = os.path.abspath(config.get('local_repo_path'))
        self.build_submit_command = config.get('build_submit_command', '') # <-- Read new config
        self.test_command = config.get('test_command')
        self.poll_interval = config.get('poll_interval_seconds', 60)
        self.token = config.get('github_token', '')
        
        self.api_url = f"https://api.github.com/repos/{self.repo}/events"
        self.last_processed_event_id = None
        self.is_first_run = True

    def get_headers(self):
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def check_github(self):
        try:
            response = requests.get(self.api_url, headers=self.get_headers())
            if response.status_code == 200:
                events = response.json()
                if not events:
                    return

                # Filter for the latest PushEvent
                push_events = [e for e in events if e['type'] == 'PushEvent']
                if not push_events:
                    return

                latest_push = push_events[0]
                event_id = latest_push['id']

                # On startup, just record the latest event ID without running tests
                if self.is_first_run:
                    self.last_processed_event_id = event_id
                    self.is_first_run = False
                    print(f"[+] Synced with GitHub. Baseline event ID: {event_id}")
                    return

                # If we see a new event ID, someone pushed to GitHub
                if event_id != self.last_processed_event_id:
                    self.last_processed_event_id = event_id
                    
                    # Extract branch name (e.g., 'refs/heads/main' -> 'main')
                    ref = latest_push['payload']['ref']
                    branch = ref.replace('refs/heads/', '')
                    
                    print(f"\n[!] New push detected on GitHub branch: '{branch}'")
                    self.sync_and_test(branch)

            elif response.status_code == 403:
                print("[X] GitHub API rate limit hit. Consider adding a github_token to mesa_watch.yml.")
            else:
                print(f"[X] Failed to fetch GitHub events. Status code: {response.status_code}")
        except Exception as e:
            print(f"[X] Connection error: {e}")



    def sync_and_test(self, branch):
        print(f"[+] Syncing local dev repository at {self.local_path}...")
        sandbox_path = os.path.expanduser("~/.mesa_test/work")
        
        try:
            # 1. Sync your local development repository directory
            subprocess.run(["git", "fetch", "origin"], cwd=self.local_path, check=True)
            subprocess.run(["git", "checkout", branch], cwd=self.local_path, check=True)
            subprocess.run(["git", "pull", "origin", branch], cwd=self.local_path, check=True)
            
            # 2. Sync the internal mirror sandbox safely
            if os.path.exists(sandbox_path):
                print(f"[+] Syncing internal mesa_test sandbox at {sandbox_path}...")
                subprocess.run(["git", "reset", "--hard"], cwd=sandbox_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "checkout", "--detach"], cwd=sandbox_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "fetch", "origin"], cwd=sandbox_path, check=True)
                subprocess.run(["git", "checkout", branch], cwd=sandbox_path, check=True)
                subprocess.run(["git", "reset", "--hard"], cwd=sandbox_path, check=True)

            # 3. Submit build status if configured
            if self.build_submit_command:
                print(f"[+] Reporting overall build compilation status to TestHub...")
                # Corrected: Passing sys.stdout directly as the file handle
                subprocess.run(self.build_submit_command, shell=True, stdout=sys.stdout, stderr=sys.stderr)

            # 4. Trigger the individual MESA test suite
            print("\n" + "="*60)
            print(f"[!] Target Branch: {branch}")
            print(f"[!] Launching automated MESA Test...")
            print(f"[!] Executing: {self.test_command}")
            print("="*60 + "\n")
            
            process = subprocess.Popen(self.test_command, shell=True, stdout=sys.stdout, stderr=sys.stderr)
            process.communicate()
            
        except subprocess.CalledProcessError as git_err:
            print(f"[X] Git/Shell operation failed: {git_err}")
        except Exception as e:
            print(f"[X] Error running test automation: {e}")


    def start(self):
        print(f"[+] MESA Cloud Watchdog active.")
        print(f"[+] Monitoring Remote Repo: {self.repo}")
        print(f"[+] Local Repo Tracked:     {self.local_path}")
        print(f"[+] Check Interval:         {self.poll_interval}s")
        print("[+] Press Ctrl+C to exit.\n")
        
        while True:
            self.check_github()
            time.sleep(self.poll_interval)

if __name__ == "__main__":
    config_file = 'mesa_watch.yml'
    
    if not os.path.exists(config_file):
        print(f"[X] Critical Error: Configuration file '{config_file}' missing.")
        sys.exit(1)
        
    with open(config_file, 'r') as f:
        config = yaml.safe_load(f)

    watcher = MesaRemoteWatcher(config)
    try:
        watcher.start()
    except KeyboardInterrupt:
        print("\n[-] Shutting down Watchdog cleanly...")
        sys.exit(0)
