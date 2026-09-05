#!/usr/bin/env python3
"""Run one pytest target in a process-isolated acceptance harness.

The harness disables unrelated globally-installed pytest plugin autoload and uses
``os._exit`` only after ``pytest.main`` has returned.  This avoids third-party
interpreter-shutdown hooks on competition machines from turning a completed test
file into an indefinite launcher hang.  The pytest return code remains authoritative.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run_isolated_pytest.py <pytest-target> [extra pytest args...]", file=sys.stderr)
        return 2
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    import pytest

    args = [*sys.argv[1:], "-p", "no:xonsh"]
    rc = int(pytest.main(args))
    sys.stdout.flush()
    sys.stderr.flush()
    # Do not let unrelated atexit hooks redefine a completed pytest result.
    os._exit(rc)


if __name__ == "__main__":
    raise SystemExit(main())
