"""Build TestHub's recommended commit and submit the short-test batch."""
import subprocess

# Vega wall times on 2026-10-14: approximately 41, 93 and 96 seconds.
FAST_TESTS = (
    ("binary", "jdot_gr_check"),
    ("astero", "fast_from_file"),
    ("binary", "jdot_ls_check"),
)


def run_batch():
    result = subprocess.run(["mesa_test", "install", "best"])
    if result.returncode:
        return result.returncode
    result = subprocess.run(["mesa_test", "submit", "--empty"])
    if result.returncode:
        return result.returncode
    status = 0
    for module, test in FAST_TESTS:
        print(f"Background test: {module}/{test}", flush=True)
        result = subprocess.run([
            "mesa_test", "test", test, "--module", module,
            "--skip-optional", "--no-converge",
        ])
        if result.returncode:
            status = result.returncode
    return status



if __name__ == "__main__":
    raise SystemExit(run_batch())
