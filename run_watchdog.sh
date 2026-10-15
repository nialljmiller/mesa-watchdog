#!/usr/bin/env bash
set -euo pipefail
export MESASDK_ROOT="$HOME/MESA/mesasdk-26.3.2"
export MESA_DIR="$HOME/MESA/mesa"
# The current dispatcher is upstream but not included in the published gem.
export RUBYLIB="$HOME/.local/share/mesa-watchdog/mesa_test/lib${RUBYLIB:+:$RUBYLIB}"
export PATH="$HOME/.local/share/mesa-watchdog/mesa_test/bin:$HOME/bin:$PATH"
set +u
source "$MESASDK_ROOT/bin/mesasdk_init.sh"
set -u
cd "$HOME/mesa-watchdog"
exec /usr/bin/flock --nonblock "$HOME/.mesa_test/watchdog.lock" /usr/bin/python3 -u mesa_watch.py --config mesa_watch.yml
