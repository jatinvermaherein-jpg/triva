#!/usr/bin/env python3
"""Run every check. Exits non-zero if anything fails, so this is CI-ready.

Usage:  python3 run_tests.py
Takes ~3 seconds and needs no Discord token, no network and no guild.
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
