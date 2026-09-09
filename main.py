"""Railway entry point -- a shim, not a second copy of the bot.

Railpack (the builder that replaced Nixpacks on Railway) looks for `main.py` or `app.py` in
the **project root** and fails the build with "No start command detected" when it finds
neither. It does not read `railway.json`'s start command, and the real entry point is
`bot/main.py` -- so this file is the whole fix, and it works for every builder at once
(Railpack, Nixpacks, `python main.py` on a VPS) with no duplicated logic.

runpy rather than an import: `bot/` is not a package (no `__init__.py`), and executing the
file keeps `__name__ == "__main__"` so `bot/main.py` still owns argument parsing, --check,
HUB_DB resolution and the exit codes. Nothing here decides anything.
"""
import pathlib
import runpy

runpy.run_path(
    str(pathlib.Path(__file__).resolve().parent / "bot" / "main.py"), run_name="__main__")
