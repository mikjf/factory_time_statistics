"""Run the whole pipeline against the extract that is CURRENTLY in place.

TAKING DELIVERY OF A NEW EXTRACT IS NOT THIS SCRIPT'S JOB. Drop the export in
as fts_new.xlsx (and fts_new.sql if the query changed) and run:

    python new_extract.py       <- validates, archives, promotes, then calls this

This script only ever looks at fts.xlsx and fts.parquet. Run it directly
when nothing new has arrived and you need every operation swept again - after
changing an analysis script, say, where every workbook on disk is now built by
code that no longer exists.

The manual route, if you ever want the steps apart:

    python archive_current.py   <- while the outgoing generation is intact
    (rename the new extract to fts.xlsx yourself)
    python run_all.py           <- this

Rebuilds the cache, runs every operation through fts_analysis.py and
fts_family_view.py, then setup, then HRS/LOAD, then the update list, then
re-executes fts_approach.ipynb, looks for orphaned workbooks, runs
fts_checks.py --fix over the prose, drafts the new generation row in
archive/README.md and restages github_ready/ - or, in a clone, where there
is no stager, checks that the commit guard is installed instead - and last,
when the Streamlit app's clone sits beside this one, runs its app_checks.py
against this run's outputs. Around 94
operations x 2 scripts; a script that writes no workbook for an operation with
no rows in scope is the expected outcome there, not a failure.

WHAT IT LEAVES FOR YOU is only what it cannot know: whether a rewritten
sentence still reads correctly around its new number, what a changed query
actually does, and whether a scope shrink was expected. Each one is named in
the !! lines at the end.

EXIT CODES. 0 clean. 1 a step FAILED and the artefacts cannot be trusted. 2
every step succeeded and something needs a person: the scope shrank, the
notebook did not re-execute, the prose no longer quotes this extract's
figures, or the app shows something that does not match them.

WHY THIS FILE EXISTS. README.md carried this as a copy-paste loop, and the loop
had two holes:

  1. It read the operation list from fts.parquet BEFORE anything rebuilt it.
     The list therefore came from the generation being replaced, so an
     operation appearing for the first time in the new extract was never
     analysed - and nothing said so, because a workbook that was never written
     looks exactly like an operation with no rows in scope.

  2. It omitted fts_setup_view.py and fts_load_view.py, then ran
     fts_update_list.py - which reads FTS_SETUP.xlsx and
     FTS_RUN_HRS_LOAD.xlsx.
     A fresh deliverable consolidated from two stale inputs, which is this
     project's standing failure mode wearing a clean shirt.

ONLY fts_analysis.py REBUILDS THE CACHE. fts_setup_view, fts_load_view
and fts_family_view each do a bare pd.read_parquet(CACHE) with no mtime
check, so running any of them first against a new extract silently analyses the
OLD one - no error, no warning, plausible numbers. That is why this starts with
an explicit load() before it reads anything else.

Writes nothing the pipeline does not write itself.

    --no-archive-check   proceed even though the outgoing generation was never
                         banked (see below - use it when you know why)
    --skip-notebook      leave fts_approach.ipynb on the PREVIOUS extract's
                         figures. It is the slowest step by a distance, and
                         also a published artefact - skipping is a decision to
                         publish old numbers, not a shortcut
    --skip-orphan-check  skip the workbook scan at the end
    --no-prose-fix       report drifted figures without rewriting them
    --skip-app-checks    do not run the app's app_checks.py (about seven
                         minutes) - for a quick rerun only, since a skipped
                         check is one nobody ran
"""

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
SRC = "fts.xlsx"
CACHE = "fts.parquet"
NOTEBOOK = "fts_approach.ipynb"
OUTPUTS = "outputs_run_pcs_hr"


def already_banked():
    """Is the OUTGOING cache already a generation in archive/?

    archive_current.py copies with shutil.copy2, which preserves the mtime, so
    an exact (size, mtime) match against any fts_prev_N.parquet means this
    parquet has been banked. Content hashing would be stronger; it would also
    read a 1.7 MB file per generation on every run to answer a question the
    filesystem already answers.

    THE POINT IS THE ORDER, not the copy. fts_prev.parquet is a rolling
    one-deep slot that load() overwrites on every rebuild, and nothing keeps the
    .xlsx at all. Once this run rebuilds, the outgoing generation is gone unless
    archive_current.py banked it first - which is exactly what was lost on
    2026-08-26.
    """
    if not os.path.exists(CACHE):
        return True                     # nothing to lose; first run
    s = os.stat(CACHE)
    for f in glob.glob(os.path.join(ARCHIVE, "fts_prev_*.parquet")):
        t = os.stat(f)
        if t.st_size == s.st_size and int(t.st_mtime) == int(s.st_mtime):
            return True
    return False


def rebuild_pending():
    """True when a new extract is sitting in front of an older cache."""
    return (os.path.exists(SRC) and os.path.exists(CACHE)
            and os.path.getmtime(SRC) > os.path.getmtime(CACHE))


def orphans():
    """Workbooks older than the cache, and _PARTS files with no _FAMILY.

    fts_update_list.py picks its inputs up with glob("FTS_*_PARTS.xlsx"),
    so an operation that has LEFT the extract keeps contributing its last
    workbook to every future deliverable. Nothing overwrites it, because
    nothing runs for that operation any more.
    """
    cut = os.path.getmtime(CACHE)
    stale = sorted(f for f in glob.glob(os.path.join(OUTPUTS,
                                                     "FTS_*.xlsx"))
                   if os.path.getmtime(f) < cut)
    unpaired = sorted(f for f in glob.glob(os.path.join(
            OUTPUTS, "FTS_*_PARTS.xlsx"))
                      if not os.path.exists(f[:-len("_PARTS.xlsx")]
                                            + "_FAMILY.xlsx"))
    return stale, unpaired


def run(script, *args):
    return subprocess.run([sys.executable, script, *args], check=False)


def newest_scope_block(path=None):
    """The last dated block in archive/scope_changes.md, or None.

    compare_to_previous() prints this during the rebuild, which is the first
    thing to happen and then sits under 94 operations of sweep output. The
    finding it carries - a master routing rebuilt in the ERP - is the one
    result of a run that nobody can act on without seeing, so it is read back
    out of the file and printed again at the end.

    Not re-derived, re-read. If the two ever disagree, the file is the record
    and the screen is a copy of it.
    """
    path = path or os.path.join(ARCHIVE, "scope_changes.md")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        blocks = f.read().split("\n## ")
    return ("## " + blocks[-1]).strip() if len(blocks) > 1 else None


def complete_generation_row(block):
    """Fill in the placeholder in archive/README.md's newest generation row.

    THE ROW IS WRITTEN TOO EARLY TO SAY ANYTHING. archive_current.py creates it
    during the archive, before the new extract has even been promoted, so all it
    can know is the column and row counts. What CHANGED is only knowable here,
    once the rebuild has compared the two - which is why the last cell was left
    for a person and why most of what a person then typed was derivable anyway.

    Three facts, and all three are already lying about by now:

        the query      diff the archived .sql against the live one
        the size       the two parquet footers, and the two mtimes
        the scope      the block compare_to_previous() just appended

    WHAT IT WILL NOT WRITE is why a changed query changed. "the Press & Cutting
    forge doubling" is an interpretation of a diff, not a diff, so a changed
    query leaves the marker in place and says so.
    """
    import re
    readme = os.path.join(ARCHIVE, "README.md")
    if not os.path.exists(readme):
        return None
    b = open(readme, "rb").read()
    # the file's own line ending, carried through the round trip - editing
    # one cell must not rewrite every line in the file
    end = "\r\n" if b"\r\n" in b else "\n"
    lines = b.decode("utf-8").split(end)
    hits = [i for i, ln in enumerate(lines)
            if ln.startswith("| `prev_") and "fill this in" in ln]
    if len(hits) != 1:
        return None
    i = hits[0]
    n = int(re.match(r"\| `prev_(\d+)`", lines[i]).group(1))

    bits = []
    old_sql = os.path.join(ARCHIVE, f"fts_prev_{n}.sql")
    if os.path.exists(old_sql) and os.path.exists("fts.sql"):
        same = open(old_sql, "rb").read() == open("fts.sql", "rb").read()
        bits.append("No query change" if same else
                    "QUERY CHANGED - say what it does")
    old_pq = os.path.join(ARCHIVE, f"fts_prev_{n}.parquet")
    if os.path.exists(old_pq) and os.path.exists(CACHE):
        import pyarrow.parquet as pq
        d = pq.read_metadata(CACHE).num_rows - pq.read_metadata(old_pq).num_rows
        gap = (os.path.getmtime(SRC) - os.path.getmtime(old_pq)) / 3600
        when = (f"{gap:.0f}h later" if gap < 48 else
                f"{gap / 24:.0f} days later")
        bits.append(f"{d:+,} rows, {when}")
    if block:
        m = re.search(r"(\d[\d,]*) lost, (\d[\d,]*) gained", block)
        if m:
            lost, gained = m.group(1), m.group(2)
            bits.append(f"{gained} rows gained a master, "
                        + ("none lost" if lost == "0" else f"{lost} lost"))
        if "scope SHRANK" in block:
            bits.append("SCOPE SHRANK - see scope_changes.md")
    if not bits:
        return None

    draft = ". ".join(bits[:1]) + (", " + ", ".join(bits[1:]) if bits[1:] else "")
    lines[i] = re.sub(r"\*\(what changed after it - fill this in\)\*",
                      draft, lines[i])
    open(readme, "wb").write(end.join(lines).encode("utf-8"))
    return n, draft


def run_notebook(path=NOTEBOOK):
    """Re-execute the notebook in place, through nbclient. Raises on failure.

    NOT `jupyter execute`. The CLI reads the file with the Windows locale
    encoding and silently mojibakes every non-ASCII character it writes back,
    which in a notebook this full of arrows and multiplication signs is most of
    the prose. Stating utf-8 on both the read and the write is the entire reason
    this is a function rather than one shell call.

    THE NOTEBOOK IS A PUBLISHED ARTEFACT - stage_for_github.py carries it in
    MANIFEST - so leaving it on the previous extract's figures is exactly the
    staleness this script exists to prevent, one file further out. It is the
    slowest step by a distance, which is what --skip-notebook is for; skipping
    it is a decision to publish old figures, not a shortcut.
    """
    import pathlib

    import nbformat
    from nbclient import NotebookClient

    p = pathlib.Path(path)
    nb = nbformat.reads(p.read_text(encoding="utf-8"), as_version=4)
    NotebookClient(nb, timeout=900, kernel_name="python3").execute()
    p.write_text(nbformat.writes(nb) + "\n", encoding="utf-8")


KNOWN_FLAGS = ("--no-archive-check", "--skip-orphan-check", "--skip-notebook",
               "--no-prose-fix", "--skip-app-checks")


def main():
    argv = sys.argv[1:]
    os.chdir(ROOT)
    # LINE BUFFERING, because this script's output is half its own prints and
    # half its subprocesses'. Piped to a file or a pager, Python block-buffers
    # ours while the children write straight through, so every heading arrives
    # in one lump at the end and the log reads as though the steps produced no
    # output at all. Straight to a terminal it made no difference; through a
    # pipe it made the log a liar.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    # AN UNRECOGNISED ARGUMENT IS NOT CONSENT. This script rewrites 118
    # workbooks and the deliverable; it must not start because someone typed a
    # flag it does not have. `python run_all.py --help` did exactly that once -
    # there was no --help, the argument was ignored, and the sweep began.
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return
    unknown = [a for a in argv if a not in KNOWN_FLAGS]
    if unknown:
        sys.exit("\n".join([
            f"unknown flag: {' '.join(unknown)}",
            f"known flags: {', '.join(KNOWN_FLAGS)}",
            "--help for the full note. NOTHING RAN."]))

    # A _new FILE ON DISK MEANS THIS IS THE WRONG SCRIPT. Nothing here reads
    # fts_new.*, so the sweep would run happily against the extract already in
    # place, report success, and leave the new one sitting untouched beside it -
    # a full green run over data somebody believes they replaced. That is the
    # project's standing failure mode with a fresh coat of paint, so it is a
    # refusal rather than a warning.
    waiting = sorted(f for f in ("fts_new.xlsx", "fts_new.sql")
                     if os.path.exists(f))
    if waiting:
        sys.exit("\n".join([
            f"STOP - {' and '.join(waiting)} "
            f"{'is' if len(waiting) == 1 else 'are'} waiting to be taken in.",
            "  This script does not read them. It would sweep the CURRENT "
            "extract, say",
            "  everything succeeded, and leave the new one untouched.",
            "",
            "      python new_extract.py --dry-run",
            "      python new_extract.py",
            "",
            "  That archives the outgoing generation, promotes the new files, "
            "and calls",
            "  this script itself. NOTHING RAN."]))

    # THE DATA IS NOT IN THE REPO, and that is deliberate: fts.xlsx,
    # fts.parquet, the workbooks and the deliverable are all gitignored, so a
    # fresh clone has every script and nothing to run them on. This is where
    # that shows up, and without the check it showed up badly - the run
    # announced "cache is current" when there was no cache, then died several
    # frames deep inside pandas on a missing workbook.
    #
    # fts_analysis.load() needs the .xlsx whenever it is not reading a fresh
    # cache, and "fresh" requires BOTH files, so a missing .xlsx breaks the run
    # even when the parquet is sitting right there.
    if not os.path.exists(SRC):
        sys.exit("\n".join([
            f"{SRC} is not here ({os.getcwd()}).",
            "  Nothing to analyse. The extract, the cache, the workbooks and "
            "the deliverable",
            "  are all gitignored, so a clone of the repo has the scripts and "
            "none of the",
            "  data - by design; it holds part numbers, customer family names "
            "and real",
            "  job durations.",
            "",
            "  Run this in the working folder that has the extract, or put "
            f"{SRC} here.",
            "  NOTHING RAN."]))

    if rebuild_pending() and not already_banked():
        print("STOP - fts.xlsx is newer than fts.parquet, and the parquet "
              "it is about to\n"
              "  replace is not in archive/. Rebuilding now discards the "
              "outgoing generation:\n"
              "  fts_prev.parquet is a one-deep rolling slot and the old "
              ".xlsx is already\n"
              "  overwritten.\n\n"
              "      python archive_current.py --dry-run\n"
              "      python archive_current.py\n\n"
              "  Then run this again. --no-archive-check proceeds anyway.")
        if "--no-archive-check" not in argv:
            sys.exit(1)
        print("  --no-archive-check given; continuing.\n")

    # IMPORTED, NOT SUBPROCESSED, so the rebuild happens once and in the open
    # rather than as a side effect of whichever operation ran first. sys.argv is
    # blanked because fts_analysis reads argv[1] as FOCUS_OPERATION at import
    # time, and this script's own flags are not operation names.
    # CAPTURED BEFORE load(), because load() is what makes it false. Only a run
    # that actually rebuilt has a scope block of its own; without this the tail
    # would reprint the PREVIOUS extract's block and present it as this run's.
    rebuilt = rebuild_pending()
    print("=" * 70)
    print("rebuilding the cache" if rebuilt else
          "building the cache for the first time"
          if not os.path.exists(CACHE) else
          "cache is current - no rebuild, no scope report")
    print("=" * 70)
    keep, sys.argv = sys.argv, sys.argv[:1]
    import fts_analysis as an
    an.load()
    sys.argv = keep

    import pandas as pd
    ops = sorted(pd.read_parquet(CACHE).OPERATION.str.strip().unique())
    print(f"\n{len(ops)} operations in the extract\n")

    for i, op in enumerate(ops, 1):
        print(f"[{i}/{len(ops)}] {op}")
        run("fts_analysis.py", op)
        run("fts_family_view.py", op)

    # ORDER IS FIXED, not incidental. The update list reads the setup and load
    # workbooks, so both have to be rewritten against this extract before it
    # consolidates them. It goes last, always.
    for s in ("fts_setup_view.py", "fts_load_view.py",
              "fts_update_list.py"):
        print(f"\n=== {s} ===")
        if run(s).returncode:
            sys.exit(f"{s} failed - stopping before anything reads its output")

    # AFTER the update list, because the notebook reads the artefacts it writes.
    # A failure here does NOT abort: the pipeline is finished and correct at
    # this point, and losing the orphan check and the closing summary over a
    # kernel that would not start would be a worse trade. It is reported at the
    # end instead, and the exit code carries it.
    nb_failed = False
    if "--skip-notebook" in argv:
        print(f"\n--skip-notebook given - {NOTEBOOK} keeps the PREVIOUS "
              f"extract's figures")
    elif not os.path.exists(NOTEBOOK):
        print(f"\n{NOTEBOOK} is not here - nothing to re-execute")
    else:
        print(f"\n=== {NOTEBOOK} ===")
        try:
            run_notebook()
            print("  re-executed against this extract")
        except Exception as e:
            nb_failed = True
            print(f"  FAILED - {type(e).__name__}: {e}")

    if "--skip-orphan-check" not in argv:
        stale, unpaired = orphans()
        print("\n" + "=" * 70)
        if stale or unpaired:
            for f in stale:
                print(f"  ORPHAN  {f}  older than the cache - the operation "
                      f"may have left the extract")
            for f in unpaired:
                print(f"  UNPAIRED {f}  has no matching _FAMILY workbook")
            print("  Delete an orphan or explain it. fts_update_list.py "
                  "globs these and\n  cannot tell a stale one from a current "
                  "one.")
        else:
            print("  no orphans - every workbook is newer than the cache and "
                  "paired")

    # THE PROSE IS AN ARTEFACT TOO. fts_checks.py recomputes the figures
    # the private FINDINGS.md quotes (until 2026-11-20 README.md and the
    # module docstrings), and exits 2 naming any the
    # prose no longer contains. Run here rather than left to the reader,
    # because "then run the checker" is exactly the step that gets skipped -
    # and a README quoting the previous extract is stale in the way that
    # actually reaches people.
    # --fix UNLESS TOLD NOT TO. It rewrites the drifted figure and prints the
    # before/after, and it still reports drift, so the run still ends at exit 2
    # and the diff still gets read. Replacing a number is safe while it moves
    # proportionally and unsafe when the number IS the claim - see prose_drift()
    # - so the edit is offered, never concluded.
    print("\n=== fts_checks.py ===")
    fix = [] if "--no-prose-fix" in argv else ["--fix"]
    prose_drifted = run("fts_checks.py", *fix).returncode == 2

    problems = []
    if prose_drifted:
        problems.append(
            ("PROSE REWRITTEN - read the before/after above and check the "
             "SENTENCE still\n     reads correctly, not just the number in it."
             if fix else
             "PROSE DRIFTED - figures quoted in the private FINDINGS.md"
             "\n     no longer match this extract. The list is above, under"
             "\n     \"does the prose still say this?\""))
    if nb_failed:
        problems.append(f"{NOTEBOOK} did not re-execute - it still carries the "
                        f"PREVIOUS extract's figures")

    # THE SCOPE BLOCK IS THE LAST THING ON SCREEN, and a shrink is the last
    # thing in the exit code. Whether a shrink is expected - the query window
    # moved - or alarming - engineering rebuilt ten families' routings - is a
    # judgement no script should make: a confident wrong answer here would stop
    # the one person who would have looked. So it is surfaced, never decided.
    if rebuilt:
        block = newest_scope_block()
        if block:
            print("\n" + "=" * 70)
            print("SCOPE CHANGE FOR THIS EXTRACT   (archive/scope_changes.md)")
            print("=" * 70)
            print(block)
            if "scope SHRANK" in block:
                problems.append(
                    "SCOPE SHRANK - work orders kept their place and lost "
                    "MASTER_OPS_ROWID.\n     A master routing was rebuilt in "
                    "the ERP. Results are NOT comparable\n     with the "
                    "previous run on the families named above. Decide whether "
                    "that\n     was expected before anyone reads the update "
                    "list.")

    # THE GENERATION ROW, now that the comparison exists to fill it. Only after
    # the scope block, because that is one of the three facts it drafts from.
    drafted = complete_generation_row(block if rebuilt else None)
    if drafted:
        n_, text = drafted
        print("\n" + "=" * 70)
        print(f"archive/README.md - prev_{n_} row drafted:")
        print(f"  {text}")
        if "QUERY CHANGED" in text:
            problems.append(
                "THE QUERY CHANGED and only you can say what it does. The "
                f"prev_{n_} row in\n     archive/README.md says so and is "
                "waiting for the rest of the sentence.")

    # STAGING IS NOT A DECISION, so it is not left as one. It writes only into
    # github_ready/, which is gitignored, and it either sweeps clean or deletes
    # the folder and says why. Running it every time costs nothing and means the
    # staged copy is never a version behind the working one.
    #
    # IN A CLONE THERE IS NO STAGER, and that is the design rather than a gap:
    # stage_for_github.py carries customer names in its sweep pattern, so it
    # lives only in the private working folder. A clone commits directly, and
    # the stager's two protections - the identifier sweep and the stripped
    # notebook - become a pre-commit hook and a clean filter under .git/, which
    # git never pushes. So in a clone the check is that the GUARD is there: a
    # clone without it is one `git add .` away from publishing the notebook's
    # outputs, and that is a finding, not a footnote.
    staged = os.path.exists("stage_for_github.py")
    if staged:
        print("\n=== stage_for_github.py ===")
        if run("stage_for_github.py").returncode:
            problems.append("staging failed - see above. The pipeline output "
                            "is fine; github_ready/ is not\n     current.")
    else:
        hook = os.path.join(".git", "hooks", "pre-commit")
        guarded = (os.path.exists(hook)
                   and "fts_guard" in open(hook, encoding="utf-8").read())
        print("\nno stage_for_github.py here - a clone, committing directly. "
              f"commit guard: {'installed' if guarded else 'MISSING'}")
        if not guarded:
            problems.append("NO COMMIT GUARD - this clone has no fts_guard "
                            "pre-commit hook, so nothing stops\n     the "
                            "notebook's outputs or an identifier reaching "
                            "GitHub. Install it\n     before committing "
                            "anything.")

    # THE APP IS CHECKED TOO, LAST (2026-10-06). The Streamlit app reads what
    # this run just wrote, and its app_checks.py compares what the pages and
    # the downloads say with the update list - so the goal holds of one
    # command, new_extract.py, building AND checking everything. Last, because
    # it reads the final artefacts: the update list, the per-operation
    # workbooks and the cache.
    #
    # FOUND, NOT CONFIGURED: the app's clone beside this one, the layout both
    # repositories are published in. FTS_ROOT points it at THIS folder
    # explicitly rather than leaving it to find one. Absent, it is said and
    # skipped - a pipeline run without the app is still a whole run. A FAIL is
    # a finding (exit 2), like drifted prose: the outputs are fine, and the app
    # disagrees with them, which a person has to look at.
    app = os.path.join(os.path.dirname(ROOT),
                       "factory_time_statistics_streamlit_app")
    app_checked = False
    if "--skip-app-checks" in argv:
        print("\napp_checks.py skipped (--skip-app-checks)")
    elif os.path.exists(os.path.join(app, "app_checks.py")):
        print("\n=== app_checks.py (the Streamlit app beside this clone) ===")
        rc_app = subprocess.run([sys.executable, "app_checks.py"], cwd=app,
                                env=dict(os.environ, FTS_ROOT=ROOT),
                                check=False).returncode
        app_checked = rc_app == 0
        if rc_app:
            problems.append("APP CHECKS FAILED - the app shows something that "
                            "does not match this\n     update list. The FAIL "
                            "lines are above, under === app_checks.py ===.")
    else:
        print(f"\nno app_checks.py at {app} - the app was not checked")

    print("\nnext:")
    if problems:
        print("  read the !! lines below, then commit")
    else:
        print("  nothing - artefacts, notebook and prose all match this "
              "extract, " + ("the app's checks pass, " if app_checked else "")
              + "and " + ("github_ready/ is current" if staged
                          else "the commit guard is installed"))

    # EXIT 2, NOT 1, AND THE DISTINCTION IS THE POINT. 1 means a step failed and
    # the artefacts on disk cannot be trusted. 2 means every step SUCCEEDED and
    # a person now has to look at something - a scope shrink, or a notebook that
    # did not re-execute. Collapsing the two made new_extract.py report "run_all
    # failed" after a run that had in fact finished perfectly, which is a lie in
    # the direction that gets a good result thrown away.
    if problems:
        print("\n" + "\n".join(f"  !! {p}" for p in problems))
        print("\n  The pipeline itself completed. Nothing above is a failure - "
              "it is a finding.")
        sys.exit(2)


if __name__ == "__main__":
    main()
