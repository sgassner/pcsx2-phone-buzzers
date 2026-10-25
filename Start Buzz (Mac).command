#!/bin/bash
# Double-click in Finder to start the phone buzzer server (opens a Terminal window).
# Ctrl+C or closing the window stops the server – all held keys are released.
# The first start sets everything up (needs Python 3.9 or newer).

cd "$(dirname "$0")" || exit 1
printf '\033]0;Buzz! Server\007'  # window title

pause () { echo; read -r -n 1 -s -p "$1"; echo; }
has_packages () { [ -x "$1" ] && "$1" -c "import aiohttp, pynput" 2>/dev/null; }
is_python39 () { [ -x "$1" ] && "$1" -c "import sys; sys.exit(sys.version_info < (3, 9))" 2>/dev/null; }

# Places where Python usually lives – started from Finder, PATH may be shorter than in Terminal.
candidates=(
    "$(command -v python3)"
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3
    /opt/homebrew/bin/python3 /usr/local/bin/python3
    "$HOME/miniforge3/bin/python3" "$HOME/mambaforge/bin/python3"
    "$HOME/miniconda3/bin/python3" "$HOME/anaconda3/bin/python3"
)
# Apple's Python only if the Command Line Tools are installed (otherwise macOS shows an install dialog)
xcode-select -p >/dev/null 2>&1 && candidates+=(/usr/bin/python3)

PY=""
if has_packages .venv/bin/python; then
    PY=.venv/bin/python                          # 1. set up by an earlier start
else
    for c in "${candidates[@]}"; do              # 2. a Python that already has the packages
        if has_packages "$c"; then PY="$c"; break; fi
    done
fi

if [ -z "$PY" ]; then                            # 3. first start: own environment in .venv
    base=""
    for c in "${candidates[@]}"; do
        if is_python39 "$c"; then base="$c"; break; fi
    done
    if [ -z "$base" ]; then
        echo "Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/"
        echo "and then double-click this file again."
        pause "Press any key to close …"
        exit 1
    fi
    echo "First start: setting things up with $base (takes about a minute) …"
    # A current pip first: old ones (e.g. Apple's Python 3.9) pick broken package versions
    if ! "$base" -m venv --clear .venv \
        || ! .venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip \
        || ! .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt; then
        echo
        echo "Setup failed – see the messages above."
        pause "Press any key to close …"
        exit 1
    fi
    PY=.venv/bin/python
fi

"$PY" buzz.py "$@"
status=$?
if [ $status -ne 0 ] && [ $status -ne 130 ]; then
    pause "buzz.py stopped with error $status – press any key to close …"
fi
exit $status
