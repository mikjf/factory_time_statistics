"""Recompute the one-off figures that the findings notes quote.

WHERE THE FIGURES LIVE (2026-11-20). Until the repository went public they were
quoted in README.md and in the module docstrings. The plant's own figures are not
published, so they moved to FINDINGS.md in the private twin beside this repository
(../factory_time_statistics_private/), and every claim below is checked there. In
a clone with no private twin the claims are skipped, not failed. Where the notes
below say README, read FINDINGS.md.

WHY THIS EXISTS. Three claims in the README could not be reproduced from the
artefacts, because they are COUNTERFACTUALS - "what the numbers would have been
under a different rule" - and the artefacts only record the pipeline as it
actually runs. They were computed once by hand and written into prose, and prose
does not recompute. One of them had drifted into two copies that disagreed with
each other (README said 47 steps, fts_load_view's docstring said 61), and a
second was refreshed by hand against the wrong population and came out wrong.

Nothing here is part of the pipeline and nothing here is imported by it:

    python fts_checks.py          run_all.py runs it near the end

Every figure prints with the README sentence it belongs to. The load-bearing
ones are also CLAIMED - see claim() below - which means the prose files are
searched for them and the script exits 2 naming any that have gone. Diffing by
eye was the step that got skipped, and skipping it is how a stale percentage for
one operation outlived every figure around it.

Read-only: opens the parquet, the workbooks and the prose, writes nothing.
"""

import pathlib
import re
import sys

import numpy as np
import pandas as pd

import fts_analysis as an
import fts_load_view as lv
import fts_setup_view as sv

UPDATE_LIST = "FTS_UPDATE_LIST.xlsx"
CACHE = "fts.parquet"

# The prose every figure is claimed against - see the module docstring. Never in this
# repository: the private twin is not published.
FINDINGS = str(pathlib.Path(__file__).resolve().parent.parent
               / "factory_time_statistics_private" / "FINDINGS.md")

SALE, SEMI = "Articolo di vendita", "Articolo non di vendita"
FAM_KEY = ["FAMILY", "OPERATION", "OPERATION_SEQUENCE"]



# ---- claims, and whether the prose still contains them -----------------------
# WHY THIS EXISTS AT ALL. Printing a figure next to the sentence it belongs to
# still leaves a human to compare the two, and that comparison is the step that
# gets skipped - which is how a stale percentage for one operation survived while
# every other figure around it was refreshed.
#
# So a section can CLAIM a figure: the exact string as the prose spells it, and
# the file it should appear in. At the end the file is searched for that string.
# Present -> the prose agrees. Absent -> the prose has drifted, or someone
# reworded the sentence, and either way a person should look.
#
# A PRESENCE TEST, NOT A PARSE. It cannot tell a correct sentence from one that
# happens to contain the right digits, and it says nothing about figures nobody
# claimed. It is a smoke alarm, not an auditor - but the alarm fires on the
# exact failure this file was written for, which printing alone never did.
CLAIMS = []


def claim(value, where, what, near=None):
    """Record that `value` should appear verbatim in `where`.

    near: a literal string, unique in the file, sitting on the SAME LINE as the
    figure and before it. Given one, --fix can rewrite the number in place.
    Without one the claim is still checked, just not fixable - which is the
    right answer for a figure that spans a line break, and there are a few.

    Or a (section, anchor) pair, for a line shape the file repeats. README.md
    has two tables with a "| `PCS/HR` | " row in them; the section is a literal
    from the table's own header, and the anchor is then matched on the FIRST
    line after it. Without that the anchor hits twice and is refused rather
    than guessed at.
    """
    CLAIMS.append((str(value), where, what, near))


# A number token as the prose spells one: 1,234 / 1.234 / 12 / 345.
_NUM = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def _repair(line, near, value):
    """Rewrite the first number after `near` on this line. None if it cannot.

    ONLY THE NUMBER IS SUBSTITUTED, not the whole claimed value. A claim like
    "653 hours" carries its unit so the presence check verifies the unit too;
    writing that string over the digits gave "653 hours hours".
    """
    want = _NUM.search(value)
    if not want:
        return None
    at = line.find(near)
    if at < 0:
        return None
    head, tail = line[:at + len(near)], line[at + len(near):]
    m = _NUM.search(tail)
    if not m or m.group() == want.group():
        return None
    return head + tail[:m.start()] + want.group() + tail[m.end():]


def prose_drift(fix=False):
    """Report every claimed figure the prose no longer contains.

    With fix=True the fixable ones are REWRITTEN and the change printed as a
    before/after pair. It still returns True, so the run still exits 2 and you
    still read the diff. That is deliberate.

    WHY IT DOES NOT JUST FIX AND FALL SILENT. Replacing a number is safe while
    it drifts proportionally - 1,480 to 1,481 - and unsafe when the number IS
    the claim. "they agree on N of N work orders" asserts an identity;
    if that ever became fewer than N of N, rewriting the first figure leaves a
    sentence still reading as agreement while the data says the identity broke,
    and the check would go quiet on exactly the finding that mattered most. So
    the edit is offered, never concluded.
    """
    print("\n-- does the prose still say this? ----------------------------")
    # BYTES AND A DETECTED LINE ENDING, not read_text/write_text. read_text
    # translates CRLF to LF on the way in and write_text(newline="") does not
    # translate back, so a one-figure edit silently rewrote every line ending in
    # the file. This project has both kinds - README.md is LF,
    # fts_setup_view.py is CRLF - so the file's own ending has to survive the
    # round trip.
    cache, endings, missing = {}, {}, []
    absent = sorted({w for _, w, _, _ in CLAIMS if not pathlib.Path(w).exists()})
    for w in absent:
        print(f"  SKIPPED  {pathlib.Path(w).name:22s} not found - no private twin "
              f"here, so its claims are not checked")
    for value, where, what, near in CLAIMS:
        if where in absent:
            continue
        if where not in cache:
            b = pathlib.Path(where).read_bytes()
            endings[where] = "\r\n" if b"\r\n" in b else "\n"
            cache[where] = b.decode("utf-8")
        if value not in cache[where]:
            missing.append((value, where, what, near))
    if not missing:
        checked = sum(1 for _, w, _, _ in CLAIMS if w not in absent)
        print(f"  all {checked} checked figures still appear in the prose"
              + (f" ({len(CLAIMS) - checked} skipped)" if checked < len(CLAIMS) else ""))
        return False, 0

    edits = {}
    for value, where, what, near in missing:
        if not (fix and near):
            print(f"  DRIFTED  {pathlib.Path(where).name:22s} no longer contains {value!r}  "
                  f"({what})"
                  + ("" if near or not fix else "  [no anchor - fix by hand]"))
            continue
        lines = cache[where].split(endings[where])
        section, anchor = near if isinstance(near, tuple) else (None, near)
        start = 0
        if section:
            at = [i for i, ln in enumerate(lines) if section in ln]
            if len(at) != 1:
                print(f"  DRIFTED  {pathlib.Path(where).name:22s} {value!r} ({what}) - section "
                      f"{section!r} matches {len(at)} lines, fix by hand")
                continue
            start = at[0]
        hits = [i for i, ln in enumerate(lines[start:], start) if anchor in ln]
        if not hits or (section is None and len(hits) != 1):
            print(f"  DRIFTED  {pathlib.Path(where).name:22s} {value!r} ({what}) - anchor "
                  f"{anchor!r} matches {len(hits)} lines, fix by hand")
            continue
        i = hits[0]
        fixed = _repair(lines[i], anchor, value)
        if fixed is None:
            print(f"  DRIFTED  {pathlib.Path(where).name:22s} {value!r} ({what}) - no number "
                  f"found after the anchor, fix by hand")
            continue
        print(f"  FIXED    {pathlib.Path(where).name:22s} ({what})")
        print(f"      -  {lines[i].strip()[:100]}")
        print(f"      +  {fixed.strip()[:100]}")
        lines[i] = fixed
        cache[where] = endings[where].join(lines)
        edits[where] = cache[where]

    for where, text in edits.items():
        pathlib.Path(where).write_bytes(text.encode("utf-8"))

    if edits:
        print(f"  {len(edits)} file(s) rewritten. READ THE DIFF ABOVE: a "
              f"figure that moved a lot may")
        print(f"  mean the sentence around it is now wrong, not just the "
              f"number in it.")
    else:
        print(f"  {len(missing)} of {len(CLAIMS)} - fix the prose, or the "
              f"claim if the sentence was reworded.")
    return True, len(edits)


def _extract():
    """The read-only half of fts_analysis.load(), never the rebuild branch.

    Same reasoning as evidence.read_extract() in the app: load() rotates
    fts_prev.parquet when the xlsx is newer, and a checking script must never
    be able to destroy the scope-comparison baseline.
    """
    df = pd.read_parquet(CACHE)
    for c in ["FAMILY", "PART_TYPE", "PART_ID", "WORK_ORDER", "OPERATION",
              "LT_TYPE", "RUN_TYPE_MASTER"]:
        df[c] = df[c].str.strip()
    df["LAST_TRANSACTION_DATE"] = pd.to_datetime(df["LAST_TRANSACTION_DATE"],
                                                 errors="coerce")
    return df


def run_scope(df):
    """The PCS/HR run population, matching fts_analysis' own filters."""
    return df[(df.LT_TYPE == "Run")
              & (df.RUN_TYPE_MASTER == "PCS/HR")
              & ~df.OPERATION.str.upper().str.startswith("S RIL")
              & (df.LAST_TRANSACTION_DATE >= pd.Timestamp(an.FY_START))
              & (df.RUN_MASTER > 0) & (df.HOURS_MASTER > 0)
              & (df.HOURS_WORKED > 0)
              # keeps this in step with fts_analysis.main(): one row in the
              # whole run scope, but a scope that "matches" except for a term
              # is the kind of thing that stops matching quietly
              & ~((df.CREW_EXEMPT == 1) & (df.USER_5.fillna(1) > 1))
              & df.MASTER_OPS_ROWID.notna()]


def single_load_rule():
    """README: "The filter is HOURS_MASTER == RUN_MASTER rather than ..."

    The two rules CANNOT disagree. fts.sql computes, for HRS/LOAD,

        HOURS_MASTER = CEILING(TOTAL_QTY / LOAD_SIZE_QTY) * RUN

    so HOURS_MASTER == RUN is true exactly when CEILING(qty / load) == 1, which
    is true exactly when 0 < qty <= load. It is an algebraic identity given
    RUN > 0 and HOURS_MASTER > 0, both of which the scope already requires.

    Checked against all three extracts on 2026-08-26: zero disagreement in
    every one, and all three queries carry the same CEILING expression. The
    "47 steps" (README) and "61 steps" (fts_load_view) figures describe
    something that cannot exist under this query.
    """
    one, multi = lv.load()
    wo = pd.concat([one, multi])
    fits = wo.TOTAL_QTY <= wo.LOAD_SIZE
    a = int((fits & ~wo.SINGLE_LOAD).sum())
    b = int((~fits & wo.SINGLE_LOAD).sum())
    print("\n-- single-load rule ------------------------------------------")
    print(f"  work orders in scope                       : {len(wo):>7,}")
    print(f"  fits one load, master charged more         : {a:>7}")
    print(f"  needs more than one, master charged one    : {b:>7}")
    print(f"  the two rules agree on                     : "
          f"{int((fits == wo.SINGLE_LOAD).sum()):>7,} "
          f"({(fits == wo.SINGLE_LOAD).mean():.2%})")
    print("  EXPECT 0 and 0 - it is an identity, see the docstring.")
    return a, b


def part_type_split(df):
    """README's "Splitting by part type" table, and the M.193 sale-row count.

    The counterfactual is "what if PART_TYPE were not in the pooling key".
    Semi-finished and finished parts of the same family run the same operation
    at very different rates, so pooling them measures neither.
    """
    r = run_scope(df)
    wide = r.groupby(FAM_KEY + ["PART_TYPE"]).WORK_ORDER.nunique()
    narrow = r.groupby(FAM_KEY).agg(WO=("WORK_ORDER", "nunique"),
                                    TYPES=("PART_TYPE", "nunique"))
    # Setup uses its own view and its own floor, and SKIPPED rows are excluded
    # before grouping - that is the population the original figure was computed
    # on, and computing it on all rows instead is how a hand refresh of this
    # very sentence came out wrong.
    s = sv.load("PCS/HR")
    s = s[~s.SKIPPED]
    s_wide = s.groupby(["OPERATION", "FAMILY", "PART_TYPE",
                        "OPERATION_SEQUENCE"]).WORK_ORDER.nunique()
    s_narrow = s.groupby(["OPERATION", "FAMILY", "OPERATION_SEQUENCE"]).agg(
        WO=("WORK_ORDER", "nunique"), TYPES=("PART_TYPE", "nunique"))
    mn = getattr(sv, "MIN_SETUPS", an.MIN_WO)

    print("\n-- PART_TYPE never pooled ------------------------------------")
    print(f"  run   groups pooled without PART_TYPE      : {len(narrow):>7,}")
    print(f"        of those, mixed                      : "
          f"{int((narrow.TYPES > 1).sum()):>7}")
    print(f"        testable ({an.MIN_WO}+ WO)  pooled -> split      : "
          f"{int((narrow.WO >= an.MIN_WO).sum()):>4} -> "
          f"{int((wide >= an.MIN_WO).sum())}")
    print(f"  setup groups pooled without PART_TYPE      : {len(s_narrow):>7,}")
    print(f"        of those, mixed                      : "
          f"{int((s_narrow.TYPES > 1).sum()):>7}")
    print(f"        testable ({mn}+)     pooled -> split      : "
          f"{int((s_narrow.WO >= mn).sum()):>4} -> "
          f"{int((s_wide >= mn).sum())}")

    # "no group became untestable" - stated in the README and worth proving
    # rather than repeating. A pool of 8 can split 5/3 and lose both halves.
    lost = 0
    for k in narrow[narrow.WO >= an.MIN_WO].index:
        halves = wide.loc[k] if k in wide.index.droplevel(-1) else None
        try:
            halves = wide.xs(k, level=tuple(range(len(FAM_KEY))))
        except (KeyError, TypeError):
            continue
        if (halves >= an.MIN_WO).sum() == 0:
            lost += 1
    print(f"  pooled-testable groups losing BOTH halves  : {lost:>7}")

    # CLAIMED SINCE 2026-10-07. This paragraph's figures were written down once
    # and never watched, and drifted until two of its SENTENCES were false -
    # "splitting costs nothing", "the testable count rises". It is now a table,
    # one figure per row behind its own label, so each one is claimed and
    # --fix can rewrite it; the sentences around it make no claim a figure
    # could falsify.
    sale_193 = int((df.PART_ID.astype(str).str.startswith("M.193")
                    & (df.PART_TYPE == SALE)).sum())
    off = {p: int((df.PART_ID.astype(str).str.startswith(p)
                   & (df.PART_TYPE != SALE)).sum()) for p in ("M.393", "M.395")}
    print(f"  M.193 rows that are sale articles          : {sale_193:>7,}")
    print(f"  M.393 / M.395 rows NOT sale articles       : "
          f"{off['M.393']:>7} / {off['M.395']}   (README: without exception)")
    claim(f"**{sale_193:,} `M.193` rows", FINDINGS,
          "M.193 rows that are sale articles", "without exception, but **")
    # "WITHOUT EXCEPTION" IS A YES/NO, NOT A FIGURE, so --fix has nothing to
    # rewrite. When it stops being true, a claim the README cannot contain is
    # registered instead: it reports as DRIFTED, makes this script exit 2, and
    # run_all turns that into a !! line - the sentence needs a person.
    if off["M.393"] or off["M.395"]:
        claim(f"<{off['M.393']} M.393 and {off['M.395']} M.395 rows that are "
              f"not sale articles>", FINDINGS,
              "'without exception' is FALSE now - reword the M.193 / M.393 "
              "sentence by hand")
    for label, n in (
            ("Run groups pooled without `PART_TYPE`", len(narrow)),
            ("Run groups mixing both types", int((narrow.TYPES > 1).sum())),
            ("Setup groups pooled without `PART_TYPE`", len(s_narrow)),
            ("Setup groups mixing both types", int((s_narrow.TYPES > 1).sum())),
            ("Run groups testable, pooled", int((narrow.WO >= an.MIN_WO).sum())),
            ("Run groups testable once split", int((wide >= an.MIN_WO).sum())),
            ("Setup groups testable, pooled", int((s_narrow.WO >= mn).sum())),
            ("Setup groups testable once split", int((s_wide >= mn).sum())),
            ("Groups testable pooled but in neither half", lost)):
        claim(f"| {label} | {n:,} |", FINDINGS, label, f"| {label} | ")


def half_groups(df):
    """README's "Splitting by part type" table: the half-groups and parts spared.

    A mixed group that produced an action applied it to every part in the pool,
    both types. Split, only the half carrying the evidence is corrected - so
    the parts on the other side are spared a correction measured entirely on
    parts unlike them. That is the concrete cost of pooling, and the reason the
    key separates the two types.
    """
    r = run_scope(df)
    g = r.groupby(FAM_KEY).PART_TYPE.nunique()
    m = r.set_index(FAM_KEY).loc[g[g > 1].index].reset_index()
    acts = pd.read_excel(UPDATE_LIST, sheet_name="01_Actions")
    seq = next(c for c in acts.columns if "SEQ" in c.upper())
    key = set(map(tuple, acts[["FAMILY", "OPERATION", seq, "PART_TYPE"]]
                  .drop_duplicates().values))

    halves = m.groupby(FAM_KEY + ["PART_TYPE"]).PART_ID.nunique()
    acting = {t: 0 for t in (SEMI, SALE)}
    for (*k, t) in halves.index:
        if (*k, t) in key:
            acting[t] = acting.get(t, 0) + 1

    spared, groups = set(), 0
    for k, grp in m.groupby(FAM_KEY):
        if (*k, SEMI) in key and (*k, SALE) not in key:
            groups += 1
            spared |= set(grp[grp.PART_TYPE == SALE].PART_ID)

    print("\n-- the mixed groups, split ------------------------------------")
    print(f"  mixed groups                               : {len(g[g > 1]):>7}")
    print(f"  half-groups                                : "
          f"{len(g[g > 1]) * 2:>7}")
    print(f"  halves carrying an action                  : "
          f"{sum(acting.values()):>7}"
          f"   ({acting.get(SEMI, 0)} semi-finished, {acting.get(SALE, 0)} sale)")
    print(f"  groups acting on semi-finished only        : {groups:>7}")
    print(f"  sale-article parts spared a correction     : {len(spared):>7}")
    # Claimed on README's table rows - see part_type_split.
    for label, n in (
            ("Half-groups the mixed run groups split into", len(g[g > 1]) * 2),
            ("Half-groups carrying an action", sum(acting.values())),
            ("Of those, on the semi-finished side", acting.get(SEMI, 0)),
            ("Groups where only the semi-finished half acts", groups),
            ("Sale-article parts spared a correction", len(spared))):
        claim(f"| {label} | {n:,} |", FINDINGS, label, f"| {label} | ")


# The eight sheets, each with the stem of its row's description in README's
# table. WHY THE STEM IS PART OF THE CLAIM. A claim is a presence test on a
# literal string, so that string has to be unique in the file, and a bare row
# count is not: "| 200 | " is also a cell in the HRS/LOAD table further down,
# and 178, 238 and 113 all occur in the Status paragraph. The sheet name
# cannot supply the uniqueness either - "01_Actions" leads with digits, so
# _repair would rewrite the "01" and leave the count untouched. The stem can.
# Reword one and the claim reports drift instead of fixing it, which is the
# documented behaviour and the right one for generated prose.
SHEET_ROWS = (
    ("00_Summary", "every operation analysed"),
    ("01_Actions", "one row per decision"),
    ("02_RunUpdateList", "**the `PCS/HR` run list"),
    ("03_SetupUpdateList", "the setup list"),
    ("04_LoadUpdateList", "the `HRS/LOAD` run list"),
    ("05_FamilyTotals", "the same actions by family"),
    ("06_FamilyOperation", "one level down"),
    ("07_TokenBookings", "steps closed without a duration"),
)
# The one row the table bolds, so both of its cells carry asterisks.
BOLD_ROW = ("02_RunUpdateList",)
EXEC_SHEETS = ("02_RunUpdateList", "03_SetupUpdateList", "04_LoadUpdateList")


def update_list_sheets():
    """README: the eight-sheet table, and the Status headline above it.

    THE MOST MECHANICAL FIGURES IN THE FILE, AND NOTHING CLAIMED THEM. On
    2026-09-02 seven of the eight row counts were wrong. Two of them rule out
    the extract merely growing: 00_Summary read 66 against 113, and
    03_SetupUpdateList read 464 against 424 - drift in BOTH directions inside
    one table. They had been copied in by hand at different moments, which is
    also why the headline claimed 2,422 field edits while the table's own
    three execution sheets summed to 2,404. Both figures are len(all_edits) in
    fts_update_list.main(); neither was ever recomputed.

    THE OPERATION COUNTS TOOK A REWORDING FIRST, not a renumbering. The block
    used to read "All 58 in-scope PCS/HR operations analysed ... plus three
    that are HRS/LOAD only. Of those 61", and no population this pipeline
    computes reaches 58 or 3. 00_Summary lists 96 operations; 86 of them carry
    some PCS/HR work and 9 are HRS/LOAD with none; the PCS/HR run scope itself
    is 49. Those figures were computed by hand under filters nothing
    reproduces, so raising 61 to 96 would have left a sentence about the
    IN-SCOPE operations quoting a count of ALL of them. The sentence now
    describes what 00_Summary actually publishes, and the four claims below
    hold it there. The three-way split is asserted to cover every operation,
    because a sentence that accounts for all of them is the point.
    """
    xl = pd.ExcelFile(UPDATE_LIST)
    rows = {sh: len(pd.read_excel(xl, sh)) for sh in xl.sheet_names}
    print("\n-- FTS_UPDATE_LIST.xlsx, sheet by sheet ----------------------")
    for sheet, stem in SHEET_ROWS:
        if sheet not in rows:
            # 03 and 04 are written only when they have rows.
            print(f"  {sheet:<20s} : {'absent':>7}")
            continue
        b = "**" if sheet in BOLD_ROW else ""
        print(f"  {sheet:<20s} : {rows[sheet]:>7,}")
        claim(f"{b}{rows[sheet]:,}{b} | {stem}", FINDINGS,
              f"{sheet} rows", f"| {b}`{sheet}`{b} | ")

    acts = pd.read_excel(xl, "01_Actions")
    edits = pd.concat([pd.read_excel(xl, s) for s in EXEC_SHEETS
                       if s in rows], ignore_index=True)
    parts = edits.PART_ID.nunique()
    by = acts.groupby(["TARGET", "RUN_TYPE"]).size()
    pcs = int(by.get(("RUN", "PCS/HR"), 0))
    load = int(by.get(("RUN", "HRS/LOAD"), 0))
    setup = int((acts.TARGET == "SETUP_HRS").sum())
    flag = edits.get("REVIEW_FLAG")
    review = 0 if flag is None else int((flag.fillna("") != "").sum())
    print("\n-- the Status headline ---------------------------------------")
    print(f"  PCS/HR run actions                         : {pcs:>7,}")
    print(f"  setup actions                              : {setup:>7,}")
    print(f"  HRS/LOAD run actions                       : {load:>7,}")
    print(f"  actions in total                           : {len(acts):>7,}")
    print(f"  field edits, the three exec sheets         : {len(edits):>7,}")
    print(f"  distinct parts edited                      : {parts:>7,}")
    print(f"  rows flagged NEEDS REVIEW                  : {review:>7,}")
    # ANCHORS. Three of these sit at the START of their line, where there is no
    # literal in front of them to anchor on, so they take the (section, anchor)
    # form: a unique literal on a nearby line, then the first "**" at or after
    # it. The section is what stops "**" matching the wrong paragraph.
    # A PLAIN IN-LINE ANCHOR, after the tuple form failed. This used to anchor
    # on the section "15 were tested and held", a phrase in the Status sentence
    # - and that sentence was rewritten on 2026-09-07, deleting it. The claim
    # went on PASSING, because a section is only consulted once a figure
    # DRIFTS, so the break stayed invisible until the count moved on
    # 2026-09-08 and the fix had nowhere to land. README.md now opens that
    # sentence with "The list holds " purely so this number has a stable
    # literal in front of it: an anchor that lives on the figure's own line
    # cannot be orphaned by an edit somewhere else in the paragraph.
    claim(f"{pcs:,} suggested", FINDINGS, "PCS/HR run actions",
          "The list holds ")
    claim(f"{setup:,} setup changes", FINDINGS, "setup actions",
          "run changes**, plus ")
    claim(f"{load:,} `HRS/LOAD`", FINDINGS, "HRS/LOAD run actions",
          "setup changes and ")
    claim(f"{len(acts):,} actions", FINDINGS, "actions in total",
          (" field edits** across ", "**"))
    claim(f"{len(edits):,} field edits", FINDINGS, "field edits",
          " actions, ")
    claim(f"across {parts:,} parts", FINDINGS, "parts edited",
          " field edits** across ")
    claim(f"{review:,} are flagged", FINDINGS, "rows needing review",
          ("and can be executed directly.", "**"))

    # ALREADY DONE - added 2026-09-29, see action_progress() and attach_today()
    # in fts_update_list.py. Only for a list built from an extract that carries
    # today's master; an older list has neither column and claims nothing here.
    if "ACTION_STATUS" in acts.columns:
        st = acts.ACTION_STATUS.fillna("")
        full = int((st == "already done").sum())
        partly = int(st.str.startswith("partly").sum())
        done_rows = int(edits.TODAY_STATUS.fillna("")
                        .str.startswith("already").sum())
        print(f"  actions already done                       : {full:>7,}")
        print(f"  actions partly done                        : {partly:>7,}")
        print(f"  part edits already done                    : {done_rows:>7,}")
        # REWORDED 2026-10-08: "**N actions are already done**" read "1 actions
        # are" the day the count was one. The figure now follows a label, so
        # the sentence reads right for any count and a claim cannot break it.
        claim(f"already done in today's master: **{full:,}**", FINDINGS,
              "actions already done", "already done in today's master: **")
        claim(f"partly done: {partly:,}.", FINDINGS, "actions partly done",
              "**, partly done: ")
        claim(f"On {done_rows:,} of the", FINDINGS, "part edits already done",
              ("Actions already done in today's master:", ". On "))

    # THE STATUS OPERATION COUNTS. 00_Summary carries a TOTAL row, so it is
    # excluded here - counting it made the operations read 97 and pushed the
    # acting count to 33, which is how a summary row turns into a fact.
    summ = pd.read_excel(xl, "00_Summary")
    ops = summ[summ.OPERATION != "TOTAL"]
    n_ops = ops.OPERATION.nunique()
    acting = ops[ops.ACTIONS > 0].OPERATION.nunique()
    held = ops[ops.LEVEL == "none - tested, holds"].OPERATION.nunique()
    noth = ops[ops.LEVEL == "none - nothing testable"].OPERATION.nunique()
    assert acting + held + noth == n_ops, (
        f"the split does not cover every operation: {acting} + {held} + "
        f"{noth} != {n_ops}")
    print("\n-- operations in 00_Summary ----------------------------------")
    print(f"  operations analysed                        : {n_ops:>7,}")
    print(f"  produced at least one action               : {acting:>7,}")
    print(f"  tested, and the standard holds             : {held:>7,}")
    print(f"  nothing testable                           : {noth:>7,}")
    claim(f"{n_ops:,} operations", FINDINGS, "operations analysed",
          ("## Status", "**"))
    claim(f"Of those, {acting:,} produced", FINDINGS,
          "operations with an action", "Of those, ")
    claim(f"Tested and held: {held:,}", FINDINGS,
          "operations tested that held", "Tested and held: ")
    claim(f"Nothing testable: {noth:,}", FINDINGS,
          "operations with nothing testable", "Nothing testable: ")


def module_docstrings():
    """The figures quoted in the pipeline modules' own docstrings.

    A second stale-prose surface, found only because the README sweep went
    looking. fts_load_view's docstring and README.md carried the SAME claim
    with DIFFERENT numbers, and neither had been recomputed in weeks. Anything
    a docstring asserts about the data belongs here too.

    POPULATIONS ARE PINNED EXPLICITLY, because that is what went wrong: the
    setup figures depend on whether HRS/LOAD setups are included and the run
    figures on whether SKIPPED rows are, and neither prose copy said which.
    """
    one, multi = lv.load()
    ratio = lambda x: x.HOURS_WORKED.sum() / x.HOURS_MASTER.sum()
    print("\n-- fts_load_view docstring ---------------------------------")
    print(f"  one load   {len(one):>6,} steps   ratio {ratio(one):.3f}")
    print(f"  2+ loads   {len(multi):>6,} steps   ratio {ratio(multi):.3f}")
    # ANCHORS ARE PER FILE, because the two spell the same figure differently:
    # README.md has a markdown table row, the docstring a fixed-width line. Each
    # anchor is unique in its file and sits on the figure's own line, which is
    # what makes --fix able to rewrite it.
    # THE RATIO ANCHOR CARRIES THE COUNT, because "steps   ratio " alone sits
    # on both rows and an anchor matching twice is refused rather than guessed.
    # The count is claimed FIRST, so on a run where both moved the count is
    # already repaired in the working copy by the time the ratio is looked up.
    for n_, r_, lbl, md, doc in (
            (len(one), ratio(one), "one load", "| One load | ", "one load  "),
            (len(multi), ratio(multi), "2+ loads", "| Two or more | ",
             "2+ loads  ")):
        # EVERY CLAIMED VALUE CARRIES CONTEXT, never a bare number. The
        # ratio was claimed as "1.113" alone until 2026-09-10, and this
        # docstring quotes 1.113 in a paragraph about figures that are NOT
        # reproducible - so the presence test was satisfied by the historical
        # sentence while the live line still read 1.114. _repair still writes
        # only the first number in the value, so carrying context is free.
        claim(f"{md}{n_:,}", FINDINGS, f"{lbl} steps", md)
        claim(f"**{r_:.3f}**", FINDINGS, f"{lbl} pooled ratio",
              f"{md}{n_:,} | ")
        claim(f"{n_:,} steps", FINDINGS, f"{lbl} steps", doc)
        claim(f"ratio {r_:.3f}", FINDINGS, f"{lbl} pooled ratio",
              f"{n_:,} steps   ratio ")
        # THE READING BESIDE THE RATIO IS DERIVED FROM IT (2026-10-08). Both
        # copies carried typed words - "the standard is about right" in the
        # README and "jobs run over the standard" in the docstring, about the
        # same 1.100 - so they already disagreed. Now the direction comes from
        # the ratio and the size is a percentage: the percentage is claimed
        # and --fix keeps it current, and the direction words sit in the
        # ANCHOR, so the day a ratio crosses 1 the anchor stops matching and
        # the claim reports DRIFTED for a person instead of a wrong word.
        # Claimed after the count and the ratio, so both are already repaired
        # in the working copy when this anchor is looked up.
        if r_ >= 1:
            pct, lead, md_t, doc_lead, doc_t = (
                f"{r_ - 1:.0%}", "jobs take", "longer than the standard allows",
                "jobs take", "longer")
        else:
            pct, lead, md_t, doc_lead, doc_t = (
                f"{1 / r_ - 1:.0%}", "the standard allows",
                "more than the jobs take", "standard allows", "more")
        claim(f"— {lead} {pct} {md_t}", FINDINGS, f"{lbl} reading",
              f"{md}{n_:,} | **{r_:.3f}** — {lead} ")
        claim(f"<- {doc_lead} {pct} {doc_t}", FINDINGS,
              f"{lbl} reading", f"{n_:,} steps   ratio {r_:.3f}   <- {doc_lead} ")
    # CAPACITY RESERVED AND NOT USED on multi-load work orders - fts_load_view
    # prints it on every run, and both prose copies quoted a figure, unclaimed,
    # from an older extract while the run printed another (found 2026-10-08).
    cap = multi.HOURS_MASTER.sum() - multi.HOURS_WORKED.sum()
    print(f"  capacity reserved and not used, multi load : {cap:>7,.0f} h")
    claim(f"**{cap:,.0f} hours of capacity reserved", FINDINGS,
          "multi-load capacity reserved and not used",
          ("reported rather than corrected (`04_MultiLoadGap`)", "— **"))
    claim(f"capacity reserved and not used: {cap:,.0f} h", FINDINGS,
          "multi-load capacity reserved and not used",
          "capacity reserved and not used: ")
    print(f"  single-load share of steps                 : "
          f"{len(one) / (len(one) + len(multi)):>7.0%}")
    # From the workbook, not refitted here. main() fits HOURS_WORKED = a + b*loads
    # over one+multi capped at 6 loads and divides b by the MULTI median RUN;
    # refitting it by eye gave a different answer three times, which is the
    # whole reason this file exists.
    gap = pd.read_excel("FTS_RUN_HRS_LOAD.xlsx",
                      sheet_name="04_MultiLoadGap")
    print("  marginal load as % of RUN (from 04_MultiLoadGap):")
    for _, g in gap.sort_values("OPERATION").iterrows():
        print(f"     {g.OPERATION:22s} {g.MARGINAL_AS_PCT_OF_RUN:>7.0%}")
        # S LAVAGGIO US is deliberately NOT claimed: both prose copies say it
        # has too few work orders to support a fit, and quote no percentage.
        # fts_load_view.py DID quote a percentage until 2026-09-10 - the note there says
        # why it went rather than being refreshed.
        if g.OPERATION != "S LAVAGGIO US":
            # EACH CLAIM CARRIES ITS OPERATION NAME. These were three
            # percentages inside one flowing sentence, claimed as a bare
            # percentage - which the presence test found in the NEIGHBOURING
            # operation's figure, so one operation sat at a stale value through
            # two extracts with its claim passing. A percentage is far too
            # small a token to be searched for on its own. Both copies are now
            # a table (README) and an indented block (here) so the name and
            # the figure share a line, which also makes them --fix-able; the
            # old arrangement wrapped and could not be anchored at all.
            pct = f"{g.MARGINAL_AS_PCT_OF_RUN:.0%}"
            claim(f"| `{g.OPERATION}` | {pct}", FINDINGS,
                  f"marginal load, {g.OPERATION}", f"| `{g.OPERATION}` | ")
            claim(f"{g.OPERATION}: {pct}", FINDINGS,
                  f"marginal load, {g.OPERATION}", f"{g.OPERATION}: ")

    print("\n-- fts_setup_view docstring --------------------------------")
    for label, frames in [("PCS/HR only", ["PCS/HR"]),
                          ("both run types", ["PCS/HR", "HRS/LOAD"])]:
        s = pd.concat([sv.load(rt) for rt in frames])
        pc = s[s.ANALYTIC_SECTION.astype(str).str.contains("Preform Cutting")]
        print(f"  {label:16s} rows {len(s):>6,}  skipped {s.SKIPPED.mean():>6.1%}"
              f"  | Preform Cutting  with {ratio(pc):.3f}  without "
              f"{ratio(pc[~pc.SKIPPED]):.3f}")
    print("  The docstring quotes the BOTH-run-types figures.")


def setup_booking_band(df):
    """FINDINGS: the share of setup tickets at or below the cut, and the gap above it

    A WIDER POPULATION THAN THE TEST USES, on purpose. This paragraph is about
    the shape of the booking distribution - is the 0.02 h cut sitting in a
    sparse gap, or slicing through a crowd - and that question is about how the
    shop books setups, not about which rows the test happens to keep. So it
    stops at HOURS_MASTER > 0 and does NOT apply the master-routing filter.

    Pinned here because the difference is not obvious and the two populations
    give visibly different answers (different row counts and shares).
    Anyone recomputing this from sv.load() will get the narrower one and think
    the README has drifted. It has not.
    """
    s = df[(df.LT_TYPE == "Setup")
           & (df.LAST_TRANSACTION_DATE >= pd.Timestamp(an.FY_START))
           & ~df.OPERATION.str.upper().str.startswith("S RIL")
           & (df.HOURS_MASTER > 0)]
    h = s.HOURS_WORKED
    print("\n-- setup booking band ----------------------------------------")
    print(f"  setup tickets (HOURS_MASTER > 0, no routing filter) : {len(s):>6,}")
    print(f"  at or below the {sv.SKIP_CUT} h cut                      : "
          f"{int((h <= sv.SKIP_CUT).sum()):>6,}  ({(h <= sv.SKIP_CUT).mean():.1%})")
    print(f"  at 0.10 h or above                                 : "
          f"{int((h >= 0.10).sum()):>6,}")
    print(f"  in between (the cut sits in this gap)              : "
          f"{int(((h > sv.SKIP_CUT) & (h < 0.10)).sum()):>6,}")


def skip_rate_population():
    """FINDINGS: the setup hours charged for setups never performed, per run type

    ANOTHER POPULATION TRAP, and this one is visible to any reader who opens
    the workbook. fts_setup_view.py:395 filters `skip.WO_ALL >= MIN_SETUPS`
    before writing 03_SkipRate, so the SHEET shows only units with enough
    setups to be worth listing. The README quotes the UNFILTERED total, which
    is the right number for "how much setup time was charged and not worked" -
    a unit with three setups still charged for them.

    The two differ by roughly 200 work orders. Anyone reconciling the README
    against the sheet will not get there, so both are printed here.
    """
    total_s = total_h = 0
    print("\n-- setup skip rate -------------------------------------------")
    for rt in ("PCS/HR", "HRS/LOAD"):
        sv.CACHE = CACHE
        sk = sv.skip_rate(sv.load(rt), rt)
        total_s += int(sk.WO_SKIPPED.sum())
        total_h += sk.STANDARD_CHARGED_SKIPPED.sum()
        print(f"  {rt:9s} all units      : {int(sk.WO_SKIPPED.sum()):>6,} skipped"
              f"  {sk.STANDARD_CHARGED_SKIPPED.sum():>7,.0f} h   <- the README figure")
        # SECTION-ANCHORED: README.md carries a second table further down with
        # the same "| `PCS/HR` | " row shape, so the table's own header line
        # picks out which one is meant.
        head = "| | Work orders skipped | Standard charged |"
        # THE CELL, NOT THE BARE COUNT. A bare count is a substring of a ratio
        # like it in the operations table, so the presence test passed
        # while this row still said an older count - and the h claim below, whose anchor
        # embeds this count, then had nothing to match and reported by hand.
        # Found on the 2026-09-08 extract. The run-type names carry no digits,
        # so _NUM still finds the count first.
        claim(f"| `{rt}` | {int(sk.WO_SKIPPED.sum()):,} |", FINDINGS,
              f"{rt} work orders skipped", (head, f"| `{rt}` | "))
        claim(f"{sk.STANDARD_CHARGED_SKIPPED.sum():,.0f} h", FINDINGS,
              f"{rt} standard charged for skipped setups",
              (head, f"| `{rt}` | {int(sk.WO_SKIPPED.sum()):,} | "))
        cut = sk[sk.WO_ALL >= sv.MIN_SETUPS]
        print(f"  {'':9s} in 03_SkipRate: {int(cut.WO_SKIPPED.sum()):>6,} skipped"
              f"  {cut.STANDARD_CHARGED_SKIPPED.sum():>7,.0f} h   "
              f"(WO_ALL >= {sv.MIN_SETUPS}, {len(cut)} of {len(sk)} units)")
    print(f"  TOTAL, all units                : {total_s:>6,} skipped  {total_h:>7,.0f} h")
    claim(f"{total_h:,.0f} hours", FINDINGS,
          "headline: setup hours planned for setups never performed",
          "whether or not a setup happened, so ")


def superseded_standards(df):
    """How many standards moved mid-period, and what each analysis drops.

    A STANDARD THAT MOVED MID-PERIOD is tested on the work orders run under
    the value in force, by the run path and - since 2026-10-09 - by setup,
    which until then kept the LARGEST value a part's setups had carried. The
    two print side by side so the rule reads as one block.
    """
    print("\n-- standards that moved mid-period ---------------------------")

    wo = an.to_wo_level(run_scope(df))
    wo, _ = an.drop_token_bookings(wo, " (superseded-standard check)")
    wo["LOG_RATIO"] = np.log(wo.HOURS_WORKED / wo.HOURS_MASTER)
    res = (wo.groupby(an.UNIT, dropna=False)
             .apply(an.test_group, include_groups=False).reset_index())
    ch = res[res.MASTER_CHANGED]
    cur = ch[ch.BASIS == "current master only"]
    print(f"  run PCS/HR : {len(res):>6,} units, {len(ch)} changed master "
          f"({len(ch) / len(res):.1%}), up to {int(ch.N_MASTERS.max())} masters")
    print(f"  {'':11s} {len(cur):>6,} tested on the current master alone, "
          f"{int((cur.N_WO_ALL - cur.N_WO_CURRENT_MASTER).sum())} work orders "
          f"dropped")
    print(f"  {'':11s} {len(ch) - len(cur):>6,} parked as "
          f"\"watch - master changed recently\" before any statistical gate")

    sv.CACHE = CACHE
    perf = (lambda w: w[~w.SKIPPED])(sv.load("PCS/HR"))
    mism = int((~perf.CREW_OK).sum())
    print(f"  setup      : {len(perf):>6,} performed setups, {mism} crew "
          f"mismatch ({mism / len(perf):.1%})  <- excluded, not rescaled")
    crew = perf[perf.CREW_OK]
    n = (crew.groupby(["PART_ID", "OPERATION_SEQUENCE", "OPERATION"])
         .SETUP_HRS_MASTER.nunique())
    old = crew[~crew.ON_CURRENT]
    print(f"  {'':11s} {int((n > 1).sum()):>6,} of {len(n):,} part+seq+op "
          f"groups ran under >1 SETUP_HRS")
    print(f"  {'':11s} {len(old):>6,} crew-matched setups run under a value "
          f"since replaced, in {old.groupby(sv.UNIT).ngroups} units  "
          f"<- not tested")


def main():
    argv = sys.argv[1:]
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return
    unknown = [a for a in argv if a != "--fix"]
    if unknown:
        sys.exit("\n".join([f"unknown flag: {' '.join(unknown)}",
                            "known flags: --fix",
                            "--help for the full note. NOTHING RAN."]))
    fix = "--fix" in argv

    df = _extract()
    print("=" * 64)
    print("One-off figures quoted in the findings notes (FINDINGS.md, private).")
    print("Recomputed here; the prose does not recompute." if not fix else
          "Recomputed here, and the prose rewritten where it drifted.")
    print("=" * 64)
    single_load_rule()
    part_type_split(df)
    half_groups(df)
    setup_booking_band(df)
    skip_rate_population()
    superseded_standards(df)
    update_list_sheets()
    module_docstrings()
    # SAY WHAT ACTUALLY HAPPENED. This read "prose rewritten where noted" on
    # every --fix run, including the ones that rewrote nothing - the same class
    # of untruth the file exists to catch.
    drifted, edited = prose_drift(fix=fix)
    done = f"{edited} file(s) rewritten" if edited else "nothing written"
    print(f"\ndone - {done}.\n")
    # EXIT 2 ON DRIFT, matching run_all.py: every figure recomputed fine and a
    # person now has to fix a sentence. 1 stays reserved for this script itself
    # failing.
    if drifted:
        sys.exit(2)


if __name__ == "__main__":
    main()
