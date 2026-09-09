#!/usr/bin/env python3
"""Run every check. Exits non-zero if anything fails, so this is CI-ready.

Usage:  python3 run_tests.py
        HUB_TEST_DB=postgres://user:pw@host/db python3 run_tests.py   # same suites on Postgres

Takes a few seconds and needs no Discord token, no network and no guild. Against a real
Postgres server it additionally proves the Supabase adapter: the parity suite's 14th check
(dropped-connection recovery) only runs when a live server is reachable, and says so rather
than quietly counting itself as a pass.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SUITES = [
    ("rules engine", "tests/test_engine.py"),
    ("service layer", "tests/test_bot_services.py"),
    ("button UI + permissions", "tests/test_bot_ui.py"),
    ("scheduler + outage recovery", "tests/test_bot_scheduler.py"),
    ("backend parity (sqlite vs postgres)", "tests/test_pg_parity.py"),
    ("cross-module references", "tests/test_symbols.py"),
]


def main() -> int:
    failed = []
    for label, path in SUITES:
        proc = subprocess.run([sys.executable, str(ROOT / path)],
                              capture_output=True, text=True, cwd=str(ROOT))
        tail = (proc.stdout.strip().split("\n") or [""])[-1]
        if proc.returncode != 0:
            failed.append(label)
            print(f"✗ {label:32} {proc.stdout.strip().splitlines()[-1] if proc.stdout else ''}")
            err = proc.stderr.strip().splitlines()
            if err:
                print("   ", err[-1])
        else:
            print(f"✓ {label:32} {tail}")
    print()
    if failed:
        print(f"BLOCKED: {len(failed)} suite(s) failed -> {', '.join(failed)}")
        return 1
    print("All suites green. Safe to deploy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
