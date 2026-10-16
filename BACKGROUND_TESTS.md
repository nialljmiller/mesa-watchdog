# Background tests

Colors polling remains every 120 seconds. When no Colors run is triggered,
background work can start at most once every 12 hours (43200 seconds).
The last attempt time persists in ~/.mesa_test/last_idle_dispatch; failures,
timeouts and an empty queue also count toward the interval.

background_test.py asks TestHub for a build with `mesa_test install best`,
submits the build, then runs and submits all three short cases in each batch:

- binary/jdot_gr_check: about 41 seconds on Vega
- astero/fast_from_file: about 93 seconds
- binary/jdot_ls_check: about 96 seconds

These are observed wall times from October 14, 2026, not guarantees.
Optional inlists and convergence runs are disabled for these background cases.
TestHub still selects the commit/build modes, but the local shortlist selects
which test to run; this does not drain TestHub's test queue.

GNU timeout limits the entire background build/tests/submissions to 55 minutes,
with forced termination after another 30 seconds if needed. A timeout may
leave an unfulfilled claim until TestHub expires it. Colors runs have their
existing behavior and are not subject to this background timeout.

Tests: `python3 -m unittest -q test_mesa_watch test_background_test`
