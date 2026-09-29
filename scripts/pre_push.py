"""Optional local checks to run before pushing changes."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def run_pytest(paths: list[str], *, live: bool = False) -> int:
    env = os.environ.copy()
    if live:
        env["BIZWARE_RUN_LIVE_TESTS"] = "1"
    else:
        env.pop("BIZWARE_RUN_LIVE_TESTS", None)
    command = [sys.executable, "-m", "pytest", *paths, "-q"]
    print(f"\n$ {' '.join(command)}")
    return subprocess.run(command, env=env, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(prog="pre_push", description="Run local checks before pushing")
    parser.add_argument(
        "--live",
        action="store_true",
        help="also enable explicitly configured live integration checks",
    )
    args = parser.parse_args()

    print("==> Offline unit tests")
    result = run_pytest(["tests/unit"], live=False)
    if result:
        print("Offline checks failed.")
        return result

    if not args.live:
        print("==> Live integration checks skipped. Use --live with staging credentials to enable.")
        return 0

    print("==> Live integration checks; confirm .env.test points to your own staging services.")
    return run_pytest(["tests/integration"], live=True)


if __name__ == "__main__":
    sys.exit(main())
