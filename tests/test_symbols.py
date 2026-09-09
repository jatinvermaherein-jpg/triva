"""Static guard: every cross-module attribute reference must resolve.
Catches `V.cfg`-style bugs (function living in the wrong module) without needing
a live Discord connection. Add this to CI or run it before every deploy."""
import ast, importlib, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

def _triples(tree) -> list[tuple[str, str, str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "MIGRATIONS":
            for el in getattr(node.value, "elts", []):
                if isinstance(el, ast.Tuple) and len(el.elts) == 3:
                    out.append(tuple(e.value for e in el.elts))
    return out


def main() -> int:
    import db as D, services as V, ui
    try:
        import main as M
    except SystemExit:
        M = None
    except Exception as e:                       # import-time failure IS a finding
        print(f"FAIL  bot/main.py does not import: {type(e).__name__}: {e}")
        return 1
    mods = {"D": D, "V": V, "ui": ui}
    problems = []
    files = ["bot/main.py", "bot/ui.py", "bot/services.py", "bot/db.py", "engine/scoring.py"]
    for f in files:
        tree = ast.parse((ROOT / f).read_text())
        local_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                local_imports |= {a.asname or a.name for a in node.names}
            elif isinstance(node, ast.Import):
                local_imports |= {(a.asname or a.name).split(".")[0] for a in node.names}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                name = node.value.id
                if name in mods:
                    if not hasattr(mods[name], node.attr):
                        problems.append(f"{f}:{node.lineno} {name}.{node.attr}")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) \
                    and isinstance(node.value.value, ast.Name) and node.value.value.id in mods:
                m = mods[node.value.value.id]
                mid = node.value.attr
                if hasattr(m, mid) and not hasattr(getattr(m, mid), node.attr):
                    problems.append(f"{f}:{node.lineno} {node.value.value.id}.{mid}.{node.attr}")
    # and confirm the engine exposes everything services.py reaches for
    from loader import load_scoring            # noqa: WPS433
    sc = load_scoring()
    engine_needs = {"EventResult", "League", "standings", "season_report", "rollover",
                    "league1_raw", "speed_bonus", "band_from_rubric", "merge_judges",
                    "rubric_for", "event_points", "point_capacity", "BAND_THRESHOLDS",
                    "BAND_POINTS", "L1_TIER_POINTS", "EDIT_OVERRIDE_MULTIPLIER"}
    # v4.1 deleted the Sunday multiplier. If it comes back, it comes back as a
    # deliberate decision, not a stale reference in a required-symbols list.
    for gone in ("SUNDAY_MULTIPLIER", "GRAND_TIEBREAK_BONUS", "GRAND_PARTICIPATION_BONUS"):
        if hasattr(sc, gone):
            problems.append(f"engine still exposes {gone} - Sunday must pay flat")
    missing = sorted(n for n in engine_needs if not hasattr(sc, n))
    for n in missing:
        problems.append(f"engine/scoring.py missing {n}")
    # --- structural guards on the upgrade path ----------------------------- #
    # A dict literal keyed by table name lets a second migration for the same table
    # silently delete the first: the column is simply never added, on the one DB
    # (a live, in-progress season) where it matters. Caught statically, in seconds.
    mig_tree = ast.parse((ROOT / "bot/db.py").read_text())
    for node in ast.walk(mig_tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "MIGRATIONS":
            if isinstance(node.value, ast.Dict):
                tables = [k.value for k in node.value.keys]
                dupes = {t for t in tables if tables.count(t) > 1}
                if dupes:
                    problems.append(
                        f"bot/db.py MIGRATIONS is a dict with duplicate table keys {sorted(dupes)} "
                        f"- the earlier migration for that table was silently dropped")
            elif isinstance(node.value, ast.List):
                seen: list[tuple[str, str]] = []
                for el in node.value.elts:
                    if isinstance(el, ast.Tuple) and len(el.elts) == 3:
                        key = tuple(e.value for e in el.elts[:2])
                        if key in seen:
                            problems.append(f"bot/db.py MIGRATIONS repeats {key}")
                        seen.append(key)
                if len(node.value.elts) != len(seen):
                    problems.append("bot/db.py MIGRATIONS entry is not a (table, col, decl) triple")
    # SCHEMA must define what MIGRATIONS assumes, or ALTER TABLE duplicates a column
    # Every migrated column must ALSO be in SCHEMA, or a fresh install only gets it
    # as a side effect of connect(). Parse the CREATE blocks; a regex that cannot tell
    # which table a line belongs to would accept a column from its neighbour.
    tables = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?\n)\);",
                         (ROOT / "bot/db.py").read_text(), re.S):
        tables[m.group(1)] = m.group(2)
    for table, col, _decl in _triples(mig_tree):
        body = tables.get(table)
        if body is None:
            problems.append(f"bot/db.py: MIGRATIONS targets unknown table {table}")
        elif not re.search(rf"\b{col}\b", body):
            problems.append(f"bot/db.py: {table}.{col} is migrated but absent from SCHEMA "
                            f"(fresh installs would only get it via ALTER on boot)")

    if M is not None:
        for name in ("HubBot", "build_tree", "SETUP_NOTES", "main"):
            if not hasattr(M, name):
                problems.append(f"bot/main.py missing {name}")

    # ---- the fairness simulator must stay runnable and reproducible -------- #
    # sim/sim_rotating.py is the source of every measured claim in the plan. It has
    # broken twice: once by hard-coding a Sunday multiplier the engine no longer
    # had, and once by seeding leagues with `hash(lg) % 13` - Python randomises
    # string hashes per process, so every run re-rolled the experiment and the
    # published numbers were unreproducible. Both are silent. Guard both.
    sim = ROOT / "sim" / "sim_rotating.py"
    if sim.exists():
        # Scan CODE, not prose. The file documents *why* hash() is banned, so a naive
        # regex flags its own comment - a guard that fires on clean input gets
        # silenced wholesale within a week, which is worse than no guard.
        _raw = sim.read_text().splitlines()
        st = "\n".join(ln for ln in _raw if not ln.lstrip().startswith("#"))
        for gone in ("SUNDAY_MULTIPLIER", "GRAND_TIEBREAK_BONUS",
                     "played_all_sunday", "tiebreak_win"):
            # getattr(S, "X", default) is the sanctioned way to read an optional
            # constant; a bare S.X would AttributeError the moment it is deleted.
            for m in re.finditer(r"S\." + gone + r"\b", st):
                line_start = st.rfind("\n", 0, m.start()) + 1
                line = st[line_start:m.start()].strip()
                if not line.endswith("getattr(") and "getattr(" not in line:
                    problems.append(
                        f"sim/sim_rotating.py:{st[:m.start()].count(chr(10)) + 1} reads "
                        f"S.{gone} directly - that constant is deleted; read it via "
                        f"getattr(S, {gone!r}, default)")
        if re.search(r"(?<![\w.])hash\s*\(", st):
            problems.append("sim/sim_rotating.py seeds with hash() - string hashes are "
                            "randomised per process, so the run is not reproducible")
        if "LEAGUE_SEED" not in st:
            problems.append("sim/sim_rotating.py has no literal seed map (LEAGUE_SEED)")
    for stale in sorted((ROOT / "sim").glob("*.txt")):
        problems.append(f"sim/{stale.name} is a checked-in results table - regenerate it "
                        f"from the script instead of letting it drift from the engine")
    return _report(problems)

def _report(problems) -> int:
    if problems:
        print("UNRESOLVED REFERENCES:")
        for p in sorted(set(problems)):
            print("  ", p)
        return 1
    print("pass  all cross-module references resolve")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
