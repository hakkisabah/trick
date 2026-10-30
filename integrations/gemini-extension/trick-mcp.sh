#!/bin/sh
# Launch the Trick MCP server for Gemini CLI.
#
# Gemini CLI spawns MCP servers with a sanitised environment: PATH is not the
# shell's PATH, so a bare "trick" resolves to nothing and the extension fails
# with "spawn trick ENOENT". Resolving the interpreter here - instead of trusting
# PATH - is what makes the extension work on a machine where the CLI was started
# from a desktop launcher or a service manager.
#
# Resolution order, first hit wins:
#   1. $TRICK_PYTHON              explicit override (declared as an extension setting)
#   2. <repo>/.venv/bin/python    the development layout of a linked clone
#   3. python3 on PATH            a normal install
#   4. /usr/bin/python3           last resort, still better than ENOENT
#
# The package is imported from <repo>/src, so no installation step is required.

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)

if [ -n "${TRICK_PYTHON:-}" ] && [ -x "${TRICK_PYTHON}" ]; then
    python=${TRICK_PYTHON}
elif [ -x "$repo/.venv/bin/python" ]; then
    python="$repo/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    python=$(command -v python3)
else
    python=/usr/bin/python3
fi

PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec "$python" -m trick mcp "$@"
