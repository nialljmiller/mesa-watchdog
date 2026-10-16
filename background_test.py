"""Build TestHub's recommended commit and submit exactly one short test."""
from pathlib import Path
import subprocess

# Vega wall times on 2026-10-14: approximately 41, 93 and 96 seconds.
FAST_TESTS = (
    ("binary", "jdot_gr_check"),
    ("astero", "fast_from_file"),
    ("binary", "jdot_ls_check"),
)


def run_one(state_path=None):
    state = Path(state_path or "~/.mesa_test/background_test_index").expanduser()
    try:
        index = int(state.read_text())
    except FileNotFoundError:
        index = 0
    module, test = FAST_TESTS[index % len(FAST_TESTS)]
    result = subprocess.run(["mesa_test", "install", "best"])
    if result.returncode:
        return result.returncode
    result = subprocess.run(["mesa_test", "submit", "--empty"])
    if result.returncode:
        return result.returncode
    # Rotate attempts, even if interrupted or failed, to avoid starving other tests.
    state.parent.mkdir(parents=True, exist_ok=True)
    temporary = state.with_suffix(".tmp")
    temporary.write_text(str(index + 1))
    temporary.replace(state)
    print(f"Background test: {module}/{test}", flush=True)
    return subprocess.run([
        "mesa_test", "test", test, "--module", module,
        "--skip-optional", "--no-converge",
    ]).returncode


if __name__ == "__main__":
    raise SystemExit(run_one())
