#!/usr/bin/env bash
# Double-click: run the default daily push (pull latest WiGLE upload,
# push to WDGWars). Both keys must already be saved (./setup.sh).
# CLI:         forward any args to wigle_to_wdgwars.py through the venv.
#
# Examples:
#   ./run.sh                                # default: --from-wigle daily push
#   ./run.sh --whoami                       # validate saved WDGWars key
#   ./run.sh wardrive.wiglecsv.gz           # one-off file push
#   ./run.sh --setup                        # save keys + optional timer
#   ./run.sh --schedule                     # configure scheduled task

cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi
if [ $# -eq 0 ]; then
    "$PY" wigle_to_wdgwars.py --from-wigle --chunk-size 10000
else
    "$PY" wigle_to_wdgwars.py "$@"
fi
if [ -t 0 ]; then
    echo
    read -n 1 -s -r -p "Press any key to close..."
    echo
fi
