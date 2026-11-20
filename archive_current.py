"""Archive the current generation before pulling a new extract. Run it FIRST.

    python archive_current.py              # archive, then pull the new extract
    python archive_current.py --dry-run    # show what it would do, write nothing
    python archive_current.py --force=sql  # archive the .sql despite a newer mtime

Copies fts.sql, fts.xlsx and fts.parquet into archive/ as the next free
fts_prev_N.*, then tells you it is safe to drop the new extract in.

It also PRUNES: anything older than the last 3 generations is deleted, whole,
while its row in archive/README.md stays. See prune() for what that can and
cannot lose, and for the one generation it always refuses to touch.

WHY THIS EXISTS. The pipeline keeps exactly ONE previous cache:
fts_analysis.load() does os.replace(CACHE, PREV_CACHE), so fts_prev.parquet
is a rolling one-deep slot that is overwritten on every rebuild. Nothing keeps
the .xlsx at all - it is simply overwritten when a new pull lands on it.

That cost a generation on 2026-08-26: fts.xlsx was replaced before anyone
copied it aside, leaving that generation as a parquet with no workbook and no
way to re-pull it. The parquet is the form every script reads, so nothing was
lost analytically, but the door closed. (It was numbered prev_3 at the time.
The numbering was reset on 2026-08-28 and that generation has since been
deleted - see archive/README.md, which keeps the old table.)

THE ORDER MATTERS, and the .xlsx has the tightest window:

    1. python archive_current.py     <- while all three still agree
    2. drop the new fts.xlsx in, update fts.sql
    3. run the pipeline

Read-only with respect to everything the analysis touches: it copies, never
moves, and writes only inside archive/.
"""

import datetime as dt
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ARCHIVE = ROOT / "archive"
PARTS = ("sql", "xlsx", "parquet")


def next_n():
    """One past the highest fts_prev_N in archive/, whatever the suffix."""
    ns = [int(m.group(1))
          for p in ARCHIVE.glob("fts_prev_*.*")
          if (m := re.match(r"fts_prev_(\d+)\.", p.name))]
    return max(ns, default=0) + 1


def log_generation(n, parts):
    """Add this generation's row to the table in archive/README.md.

    THE FACTS ARE DERIVABLE, THE NARRATIVE IS NOT. Column count and row count
    come off the parquet footer; what changed after a generation is a judgement
    only a person can make, so the last cell is left as a visible placeholder.
    That placeholder is the point. A row nobody finished is a standing reminder
    sitting in the table; a row nobody wrote is indistinguishable from a
    generation that never existed, and the table has silently fallen behind
    before.

    Skipped when the parquet was not archived - a generation with no cache is
    not a row anyone can use - and when the row is already there, so re-running
    cannot duplicate it.

    IT ANCHORS ON THE LAST "| `prev_" LINE, so any OTHER table in that file
    listing generations the same way would capture the insert and swallow the
    duplicate check. archive/README.md keeps a historical table of the numbering
    as it stood before the 2026-08-28 reset; its rows read "| old `prev_1` |"
    for exactly this reason. Keep it that way, or move this to an explicit
    marker.
    """
    if "parquet" not in parts:
        return
    readme = ARCHIVE / "README.md"
    if not readme.exists():
        return
    import pyarrow.parquet as pq
    md = pq.read_metadata(ARCHIVE / f"fts_prev_{n}.parquet")

    raw = readme.read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8").split(nl)
    if any(ln.startswith(f"| `prev_{n}` |") for ln in lines):
        return
    last = max((i for i, ln in enumerate(lines) if ln.startswith("| `prev_")),
               default=None)
    if last is None:
        return
    lines.insert(last + 1,
                 f"| `prev_{n}` | {md.num_columns} | {md.num_rows:,} | "
                 f"*(what changed after it - fill this in)* |")
    readme.write_bytes(nl.join(lines).encode("utf-8"))
    print(f"  archive/README.md: row added for prev_{n} "
          f"({md.num_columns} cols, {md.num_rows:,} rows) - "
          f"the last column is yours")


KEEP = 3
# The distinctive part of what log_generation() writes into the last cell.
# One string in one place - the guard below is only as good as this match.
PLACEHOLDER = "fill this in"


def prune(n, dry=False):
    """Delete generations older than the last KEEP. The ROW always stays.

    DECIDED 2026-09-02, once there were three and the question was real. The
    whole generation goes - the .sql with the .xlsx and the .parquet - and
    archive/README.md keeps its row. That is the same trade this folder
    already made when the pre-reset generations were deleted and their table
    was kept: the record is the half worth keeping, and keeping it costs
    nothing.

    WHAT PRUNING CANNOT LOSE. scope_changes.md is appended on every rebuild
    and is never touched here, so WHAT CHANGED between any two extracts
    survives whatever happens to the files. What goes is the ability to re-run
    an analysis over old data - done once, on 2026-08-26, to settle the
    single-load rule - and it cannot be undone afterwards, because no
    generation here can be re-pulled.

    SAFE AGAINST RENUMBERING. next_n() takes the highest N plus one, so
    deleting the oldest never renumbers what is left and never reissues a
    number that has been used.

    THE GUARD. A generation whose row still carries the placeholder has never
    been described by anybody. Deleting it would take the files AND the last
    chance to say what they were, in a single step - precisely the failure the
    placeholder was put there to prevent. So it is kept and named instead, and
    the next run offers it again.
    """
    present = sorted({int(m.group(1))
                      for p in ARCHIVE.glob("fts_prev_*.*")
                      if (m := re.match(r"fts_prev_(\d+)\.", p.name))} | {n})
    doomed = present[:-KEEP]
    if not doomed:
        return
    readme = ARCHIVE / "README.md"
    lines = (readme.read_bytes().decode("utf-8").splitlines()
             if readme.exists() else [])
    print(f"\n--- retention: the last {KEEP} generations ---")
    for g in doomed:
        files = sorted(ARCHIVE.glob(f"fts_prev_{g}.*"))
        if not files:
            continue
        row = next((ln for ln in lines
                    if ln.startswith(f"| `prev_{g}` |")), None)
        if row is None or PLACEHOLDER in row:
            why = ("has no row in archive/README.md"
                   if row is None else "still has the placeholder in its row")
            print(f"  KEPT   prev_{g} {why}.\n"
                  f"         Deleting it would take the files and the last "
                  f"chance to describe\n"
                  f"         them at once. Fill the row in and this will "
                  f"offer it again.")
            continue
        mb = sum(f.stat().st_size for f in files) / 1e6
        if not dry:
            for f in files:
                f.unlink()
        print(f"  {'would prune' if dry else 'pruned'}  fts_prev_{g}.*  "
              f"{mb:.1f} MB - its row stays in archive/README.md")


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    force = {a.split("=", 1)[1] for a in argv if a.startswith("--force=")}
    if force - set(PARTS):
        sys.exit(f"--force= takes one of {'/'.join(PARTS)}")
    ARCHIVE.mkdir(exist_ok=True)
    src = {p: ROOT / f"fts.{p}" for p in PARTS}
    missing = [p for p, f in src.items() if not f.exists()]
    if missing:
        sys.exit(f"missing fts.{'/'.join(missing)} - nothing to archive")

    # THE GUARD, and the whole point of running this first. The parquet is the
    # anchor: it is the only one of the three the pipeline itself wrote, so its
    # timestamp says which generation is actually in hand. If the .sql or the
    # .xlsx is NEWER, that file has already moved on to the next generation and
    # archiving it here would file the new query - or the new extract - against
    # the old data, which is worse than not archiving it at all. A mislabelled
    # generation is a lie that looks like a record.
    #
    # Both traps have been sprung. On 2026-08-26 fts.xlsx AND fts.sql were
    # both replaced before anything was archived, so that generation's query had
    # to be recovered from a working copy and its workbook was gone for good.
    # A MTIME GUARD IS NOT A CONTENT GUARD, and the .sql trips it easily: edit
    # a comment and the timestamp moves while the query does not. That is why
    # --force=sql exists. Use it only when you know the query itself is the one
    # that produced the parquet - an .xlsx is never worth forcing, because a
    # newer workbook really is a different extract.
    anchor = src["parquet"].stat().st_mtime + 1
    stale = [p for p in ("sql", "xlsx")
             if src[p].stat().st_mtime > anchor and p not in force]
    n = next_n()

    if stale:
        what = " and ".join(f"fts.{p}" for p in stale)
        print(f"WARNING - {what} {'is' if len(stale) == 1 else 'are'} NEWER "
              f"than fts.parquet.\n"
              f"  That means the next generation has already started: the file"
              f"{'' if len(stale) == 1 else 's'} above no longer\n"
              f"  describe{'s' if len(stale) == 1 else ''} the extract the "
              f"parquet holds. Archiving them would mislabel\n"
              f"  generation {n}, so they are skipped.\n"
              f"  {'The .xlsx is not recoverable. ' if 'xlsx' in stale else ''}"
              f"{'The .sql may still be in git history. ' if 'sql' in stale else ''}\n"
              f"  Run this BEFORE editing the query or dropping a new extract in.\n"
              + "".join(f"  --force={s} to archive it anyway"
                        f"{' (only if the query itself is unchanged)' if s == 'sql' else ''}.\n"
                        for s in stale))
    parts = [p for p in PARTS if p not in stale]

    for p in parts:
        dst = ARCHIVE / f"fts_prev_{n}.{p}"
        if dst.exists():
            sys.exit(f"{dst} already exists - refusing to overwrite")
        mt = dt.datetime.fromtimestamp(src[p].stat().st_mtime)
        size = src[p].stat().st_size / 1e6
        if not dry:
            shutil.copy2(src[p], dst)
        print(f"  {'would archive' if dry else 'archived'}  "
              f"fts_prev_{n}.{p:<8} {size:>7.2f} MB   from {mt:%d %b %H:%M}")

    prune(n, dry=dry)

    if dry:
        print(f"\ndry run - nothing written. Generation {n} is what WOULD be "
              f"created.")
        return
    log_generation(n, parts)
    print(f"\ngeneration {n} archived. Safe to drop the new extract in.")
    print("Fill in the last column of its row in archive/README.md - what "
          "changed after it.")


if __name__ == "__main__":
    main()
