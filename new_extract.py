"""Take delivery of a new extract. Drop the file in, run this, walk away.

    (put fts_new.xlsx in this folder - and fts_new.sql too, if the query
     changed)

    python new_extract.py --dry-run    # say what would happen, touch nothing
    python new_extract.py              # archive, promote, run the pipeline

WHAT IT DOES, in this order and only in this order:

    1. validates fts_new.xlsx - it opens, it has rows, and it carries every
       column the current parquet does
    2. archives the OUTGOING generation via archive_current.py: fts.sql,
       fts.xlsx and fts.parquet become archive/fts_prev_N.*
    3. promotes fts_new.xlsx -> fts.xlsx, and fts_new.sql -> fts.sql
       if one was supplied
    4. runs run_all.py, which rebuilds the cache, appends to
       archive/scope_changes.md, sweeps every operation, writes the
       deliverable, checks the prose (fts_checks.py) and - when the app's
       clone sits beside this one - the app (app_checks.py)

VALIDATION COMES BEFORE ARCHIVING, and archiving before promotion, because only
step 3 destroys anything. Steps 1 and 2 can fail without costing a file: the
archive is a copy, and until the promotion runs the current generation is still
sitting there intact. A truncated download is caught while it is still just a
file with a funny name.

WHY A NEW .sql IS OPTIONAL BUT A NEW .xlsx IS NOT. The extract is the data; the
query is the record of how it was obtained. A new extract from the same query
is an update - the query is archived again alongside it, unchanged, because a
generation without its query cannot be read later. A new query with no new
extract is refused: there is nothing to analyse, and promoting it would file
the new query against the old data, which is the exact mislabelling
archive_current.py exists to prevent.

THE COLUMN CHECK IS ALSO A QUERY CHECK. If the new extract's columns differ
from the current parquet's and no fts_new.sql was supplied, the query changed
and the record of it did not. That is reported loudly, because the archived
generation would otherwise claim to have been produced by a query that cannot
produce it.

THE ROW COUNT IS CHECKED BOTH WAYS. Same count as the extract in place is
probably the same pull re-saved, which would spend a generation on a duplicate -
a warning. Materially FEWER rows is refused without --allow-shrink, because
rows leaving the extract raise nothing downstream: a truncated export would run
to completion, exit clean, and write a plausible deliverable built on a
fraction of the data.

    --dry-run       plan only, nothing written, nothing moved
    --force-sql     pass --force=sql to archive_current.py, for when fts.sql
                    has a newer mtime than the parquet but the QUERY itself is
                    unchanged (a comment edit moves the timestamp)
    --allow-shrink  accept an extract materially smaller than the one in place -
                    the fiscal window rolled, and you know it did
    --no-run        archive and promote, but stop before run_all.py
"""

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
SRC, SQL, CACHE = "fts.xlsx", "fts.sql", "fts.parquet"
NEW_SRC, NEW_SQL = "fts_new.xlsx", "fts_new.sql"
KNOWN_FLAGS = ("--dry-run", "--force-sql", "--no-run", "--allow-shrink")

# A drop bigger than this is refused without --allow-shrink. The window rolling
# on 1 October is the one legitimate way to lose a large slice at once, and that
# is a deliberate, once-a-year edit to fts.sql and fts_analysis.FY_START
# together - a moment when typing a flag costs nothing. Every other way to
# arrive here is an export that went wrong.
SHRINK_LIMIT = 0.10


def next_generation():
    """One past the highest fts_prev_N in archive/. Mirrors archive_current."""
    import re
    ns = [int(m.group(1))
          for p in glob.glob(os.path.join(ARCHIVE, "fts_prev_*.*"))
          if (m := re.match(r"fts_prev_(\d+)\.", os.path.basename(p)))]
    return max(ns, default=0) + 1


def header(path):
    """The new extract's column names, without parsing 12 MB of cells.

    openpyxl in read_only mode streams the sheet, so this touches the first row
    and stops. pd.read_excel(nrows=5) would still build the whole worksheet
    first, which is a minute of waiting to answer a question about one row.
    """
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
        return [str(c).strip() for c in row if c is not None], ws.max_row
    finally:
        wb.close()


def validate():
    """Refuse the new extract, or return what the caller needs to judge it.

    Returns (columns, incoming data rows or None, missing, extra, current rows).
    """
    try:
        cols, rows = header(NEW_SRC)
    except Exception as e:
        sys.exit(f"{NEW_SRC} will not open as a workbook - {e}\n"
                 f"Nothing was touched. Re-export it.")
    if not cols:
        sys.exit(f"{NEW_SRC} has no header row. Nothing was touched.")
    if rows is not None and rows < 2:
        sys.exit(f"{NEW_SRC} has a header and no data. Nothing was touched.")

    # THE PARQUET FOOTER, not the parquet. read_schema and read_metadata read
    # the block at the end of the file and stop; read_parquet would pull 80,000
    # rows into memory to answer questions about names and counts.
    import pyarrow.parquet as pq
    md = pq.read_metadata(CACHE)
    have = list(pq.read_schema(CACHE).names)
    missing = [c for c in have if c not in cols]
    extra = [c for c in cols if c not in have]
    return (cols, (rows - 1) if rows else None, missing, extra, md.num_rows)


def run(script, *args):
    return subprocess.run([sys.executable, script, *args], check=False)


def main():
    argv = sys.argv[1:]
    os.chdir(ROOT)
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return
    unknown = [a for a in argv if a not in KNOWN_FLAGS]
    if unknown:
        sys.exit("\n".join([
            f"unknown flag: {' '.join(unknown)}",
            f"known flags: {', '.join(KNOWN_FLAGS)}",
            "--help for the full note. NOTHING RAN."]))
    dry = "--dry-run" in argv

    have_xlsx, have_sql = os.path.exists(NEW_SRC), os.path.exists(NEW_SQL)
    if not have_xlsx and not have_sql:
        print(f"nothing to take delivery of - no {NEW_SRC} in {ROOT}.\n"
              f"Put the new extract there and run this again.")
        return
    if have_sql and not have_xlsx:
        sys.exit(f"{NEW_SQL} is here but {NEW_SRC} is not.\n"
                 f"  A new query with no new extract cannot be promoted: there "
                 f"is nothing to\n"
                 f"  analyse, and fts.sql would then describe a query that "
                 f"did not produce\n"
                 f"  fts.parquet. Export the extract first, then run this "
                 f"with both files.\n"
                 f"  Nothing was touched.")
    for f in (SRC, CACHE):
        if not os.path.exists(f):
            sys.exit(f"{f} is missing - there is no current generation to "
                     f"archive. Nothing was touched.")

    cols, new_rows, missing, extra, have_rows = validate()
    n = next_generation()

    print("=" * 70)
    print(f"  incoming   {NEW_SRC}   {os.path.getsize(NEW_SRC) / 1e6:.2f} MB, "
          f"{len(cols)} columns"
          + (f", {new_rows:,} rows" if new_rows is not None else ""))
    print(f"  in place   fts.xlsx       {have_rows:,} rows")
    q = NEW_SQL if have_sql else "not supplied - fts.sql is unchanged"
    print(f"  query      {q}")
    print(f"  archive as generation {n}")
    print("=" * 70)

    if missing:
        sys.exit(f"REFUSED - the new extract is missing "
                 f"{len(missing)} column(s) the current one has:\n"
                 f"    {', '.join(missing)}\n"
                 f"  The pipeline reads these by name and would fail partway, "
                 f"after the\n"
                 f"  promotion had already overwritten fts.xlsx. Nothing was "
                 f"touched.")

    # COLUMNS MOVED BUT THE QUERY DID NOT. Not fatal - a column can be added
    # without any script reading it - but the generation about to be archived
    # would carry a query that cannot produce the extract filed beside it, and
    # that record is the whole point of archive/.
    if extra and not have_sql:
        print(f"\n  WARNING - {len(extra)} column(s) here that the current "
              f"extract does not have:\n"
              f"    {', '.join(extra)}\n"
              f"  The query changed and {NEW_SQL} was not supplied, so "
              f"generation {n} would\n"
              f"  record a query that cannot produce this data. Add the .sql "
              f"and run again,\n"
              f"  or accept that the record is approximate.\n")
    elif extra:
        print(f"\n  {len(extra)} new column(s): {', '.join(extra)}\n")

    # ---- row count, and why it can be trusted in one direction --------------
    # openpyxl's read_only max_row is an UPPER BOUND: trailing formatting can
    # push it above the true count, nothing pushes it below. So a SHRINK it
    # reports is real - the true figure can only be smaller still - and that is
    # the direction worth refusing on. An equal count could in principle be an
    # over-report landing exactly on the old total, which is why that case only
    # warns.
    #
    # WHY A SHRINK MATTERS MORE THAN IT LOOKS. Rows leaving the extract are
    # reported by compare_to_previous() but do NOT raise run_all's exit 2 -
    # only rows that stay and lose MASTER_OPS_ROWID do. A truncated export
    # therefore runs to completion, exits clean, and writes a plausible
    # deliverable built on a fraction of the data. Nothing downstream is going
    # to catch it, so it is caught here, before the promotion.
    if new_rows is not None and have_rows:
        delta = new_rows - have_rows
        if delta == 0:
            print(f"\n  WARNING - {new_rows:,} rows, exactly what is already in "
                  f"place.\n"
                  f"  The same pull re-saved? The pipeline would run in full "
                  f"and reproduce\n"
                  f"  every artefact identically, and generation {n} would be a "
                  f"duplicate -\n"
                  f"  a slot spent on a copy. Harmless, but probably not what "
                  f"you meant.\n")
        elif delta < 0 and abs(delta) >= SHRINK_LIMIT * have_rows:
            msg = (f"the new extract has {abs(delta):,} FEWER rows than the one "
                   f"in place\n"
                   f"  ({new_rows:,} against {have_rows:,}, "
                   f"{abs(delta) / have_rows:.0%} smaller).\n"
                   f"  A drop this size is a rolled query window or an export "
                   f"that went wrong,\n"
                   f"  and only you can tell which. Rows leaving the extract do "
                   f"not raise an\n"
                   f"  error anywhere downstream - the run would finish clean "
                   f"on a fraction of\n"
                   f"  the data.")
            if "--allow-shrink" not in argv:
                sys.exit(f"REFUSED - {msg}\n"
                         f"  --allow-shrink if it is the window. Nothing was "
                         f"touched.")
            print(f"\n  WARNING - {msg}\n  --allow-shrink given; continuing.\n")
        elif delta < 0:
            print(f"\n  {abs(delta):,} fewer rows than the extract in place "
                  f"({abs(delta) / have_rows:.1%} smaller).\n")

    if dry:
        print("dry run - nothing written, nothing moved.")
        print(f"Would archive generation {n}, promote {NEW_SRC}"
              f"{' and ' + NEW_SQL if have_sql else ''}, then run run_all.py.")
        return

    # ---- 2. archive, and prove it landed ------------------------------------
    print("\n--- archiving the outgoing generation ---")
    before = set(glob.glob(os.path.join(ARCHIVE, f"fts_prev_{n}.*")))
    run("archive_current.py", *(["--force=sql"] if "--force-sql" in argv
                                else []))
    # archive_current.py SKIPS a part it thinks is mislabelled rather than
    # failing, which is right for a tool run by hand and wrong to trust blindly
    # here: promoting on top of a half-archived generation loses the half that
    # was skipped. So the parts are checked, not the exit code.
    banked = [p for p in ("sql", "xlsx", "parquet")
              if os.path.exists(os.path.join(ARCHIVE, f"fts_prev_{n}.{p}"))]
    if len(banked) < 3:
        gone = [p for p in ("sql", "xlsx", "parquet") if p not in banked]
        # ROLL THE HALF-GENERATION BACK. Left in place it is a permanent stub:
        # next_generation() would step past it, so the retry lands as N+1 and
        # archive/ keeps a numbered generation that never existed. Only files
        # THIS run created are removed, and every one of them is a copy of a
        # file still sitting live in the folder, so nothing is lost by it.
        for f in sorted(set(glob.glob(os.path.join(
                ARCHIVE, f"fts_prev_{n}.*"))) - before):
            os.remove(f)
            print(f"  rolled back {os.path.basename(f)}")
        sys.exit(f"\nSTOP - generation {n} would be incomplete: "
                 f"{', '.join(gone)} not archived.\n"
                 f"  Read the warning above. Promoting now would lose it for "
                 f"good.\n"
                 f"  --force-sql if fts.sql only has a newer TIMESTAMP and "
                 f"the query is\n"
                 f"  genuinely the one that produced fts.parquet.\n"
                 f"  Nothing has been promoted; {NEW_SRC} is still where you "
                 f"put it.")

    # ---- 3. promote ---------------------------------------------------------
    # os.replace, not copy: the "new" name is consumed, so a second run cannot
    # promote the same file twice on top of a generation that already moved on.
    print("\n--- promoting ---")
    if have_sql:
        os.replace(NEW_SQL, SQL)
        print(f"  {NEW_SQL} -> {SQL}")
    os.replace(NEW_SRC, SRC)
    print(f"  {NEW_SRC} -> {SRC}")

    if "--no-run" in argv:
        print(f"\n--no-run given. Generation {n} is archived and the new "
              f"extract is in place.\n  python run_all.py    when you are "
              f"ready.")
        return

    # ---- 4. run -------------------------------------------------------------
    print("\n--- running the pipeline ---")
    # run_all.py DISTINGUISHES ITS EXIT CODES: 2 is "finished, and something
    # needs a person" - a scope shrink, or a notebook that did not re-execute.
    # 1 is a step that actually failed. Treating both as failure said "run_all
    # failed" after a run that completed perfectly, which invites someone to
    # throw away a good result and start again.
    rc = run("run_all.py").returncode
    if rc == 2:
        sys.exit(2)
    if rc:
        sys.exit("run_all.py failed. The archive and the promotion are done, "
                 "so this is\n  safe to re-run on its own once the cause is "
                 "fixed:  python run_all.py")

    print(f"\ngeneration {n} archived, new extract live, pipeline run.")
    print("  cat archive/scope_changes.md   <- what moved between the two")
    print("  then update the table in archive/README.md")


if __name__ == "__main__":
    main()
