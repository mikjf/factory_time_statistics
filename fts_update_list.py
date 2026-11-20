"""
Consolidated update list across every operation analysed so far.

Picks up each FTS_<OPERATION>_PARTS.xlsx / _FAMILY.xlsx pair in the folder and
merges the two routes into one action list:

  PART   - the part had enough work orders to be judged on its own; it gets its
           own RUN_MASTER
  FAMILY - the part is too thin to judge alone but its family pools enough
           evidence; the family's correction applies to every part of that
           family EXCEPT any already flagged individually (those are removed
           before the family ratio is computed, so the two routes never touch
           the same part)

Sheets
  00_Summary        every operation analysed, run and setup, with the hours
                    stated both signed and sign-free. Operations that came clean
                    are listed with zero actions, so "tested and fine" cannot be
                    mistaken for "not looked at yet".
  01_Actions        one row per decision to review - run and setup together,
                    TARGET says which field it lands on
  02_RunUpdateList  THE definitive run list - one row per affected part, with
                    the exact RUN_MASTER to apply (RM_RECOMMENDED) and the
                    MASTER_OPS_ROWID to apply it to
  03_SetupUpdateList  the same for setup, written only when FTS_SETUP.xlsx
                    exists. Same MASTER_OPS_ROWID, but SETUP_HRS_RECOMMENDED
                    goes in a different field - TARGET_COLUMN says which
  04_LoadUpdateList  the same for HRS/LOAD, written only when
                    FTS_RUN_HRS_LOAD.xlsx exists. It lands on the RUN column
                    like PCS/HR, but RUN is hours per load - a duration - so
                    the correction multiplies
  05_FamilyTotals   the same actions grouped by family instead of by operation:
                    which product line is worst, rather than which process
  06_FamilyOperation  one level down - family x operation
  07_TokenBookings  work orders dropped before testing because they were closed
                    with a token booking instead of a duration. Nothing here is
                    actionable as a standard change - it is the record of what
                    the analysis cannot see, and of the hours the schedule
                    reserved for steps with no evidence they were performed

Three execution sheets, split by the field they write to and by the arithmetic
that produced them. They are deliberately not merged: a single sheet would need
a nullable column per target and would invite keying a duration into a rate.
01_Actions is where they come back together, because a reviewer wants one list
of decisions - RUN_TYPE distinguishes the two RUN flavours there.

There is deliberately no separate part-only or family-only sheet: both were
filters of the sheets above and carried nothing the filter does not.

RM_RECOMMENDED is the value to key. A PART THAT EARNED ITS OWN SUGGESTION KEEPS
IT: if a part cleared all six rules on its own evidence, that figure is what
goes in, and the family never overrides it. The family value applies to the
parts that could NOT be judged alone - and fts_family_view.py removes the
flagged parts from the family pool before computing it, so the two never cover
the same part. HRS/LOAD works the same way and setup is family-only.

RM_SUGGESTED_IF_WHOLE_FAMILY and PART_DIFFERS_FROM_FAMILY stay in the sheet as
context for anyone overriding a row by eye. They decide nothing. Until
2026-08-26 they did - see the long note at the RM_RECOMMENDED assignment for
what that rule was and why it was removed.

MASTER_OPS_ROWID is the master routing operation - one value per part+sequence,
regardless of how many work orders have run the step. That is the row an UPDATE
must target for future work orders to inherit the corrected standard. The
loader asserts the 1:1 relationship and fails loudly if it ever breaks.

Output: FTS_UPDATE_LIST.xlsx
"""

import glob
import os
import re
import sys

import numpy as np
import pandas as pd

CACHE = "fts.parquet"
# THE PER-OPERATION WORKBOOKS LIVE IN outputs_run_pcs_hr/. They are
# intermediates: written by fts_analysis.py and fts_family_view.py, read by
# fts_update_list.py and the review tool, opened by hand by nobody. THE NAME
# STATES THE SCOPE: the analysis filters LT_TYPE 'Run' and RUN_TYPE_MASTER
# 'PCS/HR', so setup and HRS/LOAD are not in here - they have workbooks of
# their own in the root, FTS_SETUP.xlsx and FTS_RUN_HRS_LOAD.xlsx. The folder
# was plain `outputs/` from 2026-08-28, when 188 of the root's 214 entries
# moved into it, and was renamed on 2026-09-10 because `outputs` named the
# mechanism and not the contents. The three DELIVERABLES stay in the root,
# because FTS_UPDATE_LIST.xlsx is the file people are told to open.
OUTPUTS = "outputs_run_pcs_hr"
OUT = "FTS_UPDATE_LIST.xlsx"
SETUP_SRC = "FTS_SETUP.xlsx"          # optional; produced by fts_setup_view.py
LOAD_SRC = "FTS_RUN_HRS_LOAD.xlsx"    # optional; produced by fts_load_view.py
# Must match the hard-coded date in fts.sql - see the note in
# fts_analysis.py. Change every copy together or the extract and the filter
# describe different years.
FY_START = "2025-10-01"
MIN_WO = 6        # must match the analysis scripts; used only for reporting

# Outside this band the suggestion is extreme enough to be worth a human look
# before it is keyed in. Token bookings used to dominate the low end; they are
# now dropped upstream (TOKEN_HOURS in fts_analysis.py), so what remains here
# is mostly the opposite problem - a default standard stamped across families
# that never described the job.
#
# The test is on the RATIO, not on the percentage change, because the change is
# ASYMMETRIC: a rate that quadruples reads +300%, but a rate cut to a quarter
# reads -75% and can never exceed -100% however extreme it gets. Flagging on the
# change caught only increases and left 153 rows proposing cuts of 70% or more
# entirely unmarked.
REVIEW_LO, REVIEW_HI = 0.25, 4.0

# STANDARD WO vs STANDARD TODAY - added 2026-09-28.
#
# Every *_CURRENT value in the three execution sheets is the WORK ORDER's copy:
# fts.sql reads RUN_MASTER, SETUP_HRS and the rest from O, the work order's own
# operation, and *_CURRENT takes it from the most recent completed work order
# (for setup, the largest of the year's). That is the right yardstick for the
# TEST - a job is measured against the standard it ran under - and it stays.
# But the master can move on after those jobs: a new revision, or an edit in
# place. On 2026-09-28 23 of 361 checkable PCS/HR rows had, e.g., jobs at 35
# against a master already at 90, so "current" and the change % were stale and
# keying the suggestion would have LOWERED the master.
#
# Since the same date fts.sql also reads the current master line (O_M) and the
# part's current revision. attach_today() adds, per row, AFTER every existing
# column and without touching any of them:
#   STANDARD_WO       the value the work orders ran with (= *_CURRENT)
#   STANDARD_TODAY    today's master on MASTER_OPS_ROWID - what keying overwrites
#   STANDARD_CHANGE   unchanged / changed within the same revision / changed
#                     with a newer revision / run type changed
#   WO_REV, CURRENT_REV   the most recent work order's revision, the part's now
#   CHANGE_PCT_TODAY  the value to key against STANDARD_TODAY
#   TODAY_STATUS      "already done" when the master has MOVED and the value to
#                     key is within MIN_EFFECT of it - FTS's own rule 5, measured
#                     from what is in the ERP today. Never on an unchanged row:
#                     there the suggestion already cleared rule 5 against it.
#   REVIEW_FLAG_TODAY the REVIEW_LO-REVIEW_HI band again, from today's master,
#                     only where the master moved (unchanged rows are covered
#                     by REVIEW_FLAG already)
# Decided with the user 2026-09-28: no other special handling - where the master
# overshot or moved the other way, the suggestion stands like everywhere else.
MIN_EFFECT = 0.10      # must match MIN_EFFECT in the analysis scripts (rule 5)
SAME_VALUE = 0.005     # the ERP stores 2 decimals; closer than this is equal
TODAY_COLS = ["RUN_TYPE_MASTER_TODAY", "RUN_MASTER_TODAY",
              "LOAD_SIZE_QTY_MASTER_TODAY", "SETUP_HRS_MASTER_TODAY",
              "USER_5_MASTER_TODAY", "CURRENT_REV"]

# Must match TOKEN_HOURS in the analysis scripts. Used here only to report what
# was removed - 04_TokenBookings is a record, not a filter.
TOKEN_HOURS = 0.02

# RATIO and the confidence interval are two different estimators, and the sheets
# put them side by side. RATIO is the pooled, hours-weighted figure that drives
# the correction: sum(HOURS_WORKED) / sum(HOURS_MASTER) over the whole group. The
# interval comes from the t-test on mean(ln ratio), which is the geometric mean
# of the PER WORK ORDER ratios, unweighted. When large and small jobs behave
# differently the two legitimately disagree, and in 19 of 244 actions the pooled
# ratio lands outside its own interval - which reads as a broken number unless
# the columns say what they cover. The interval is only ever used as a test
# (does it exclude 1?), never as error bars on RATIO.
CI_NAMES = {"CI95_LOW": "CI95_LOW_PER_WO", "CI95_HIGH": "CI95_HIGH_PER_WO"}


def _rev(v):
    """A revision label as text, whatever Excel and pandas made of it.

    Labels are letters (A, B ...) or numbers (0, 1 ... 10). A numeric column
    reads back as 10.0 and a mixed one as '10', so both are brought to '10'.
    Only ever compared for EQUALITY - never sorted: as text '10' < '9' and
    '1' < 'A', both wrong (revisions run letters first, then numbers).
    """
    if pd.isna(v):
        return np.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip()
    if s.lower() in ("", "nan", "none"):
        return np.nan
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def load_today():
    """Today's master per MASTER_OPS_ROWID, and the latest work order's revision.

    Returns (today, wo_rev) or None when the extract predates the columns
    (fts.sql before 2026-09-28) - the sheets are then written as they always
    were. `today` is one row per master line; `wo_rev` is the revision of the
    most recent work order per (MASTER_OPS_ROWID, LT_TYPE) in the fiscal year,
    i.e. the work order the *_CURRENT value normally comes from.
    """
    df = pd.read_parquet(CACHE)
    if any(c not in df.columns for c in TODAY_COLS):
        return None
    df = df[df.MASTER_OPS_ROWID.notna()].copy()
    df["MASTER_OPS_ROWID"] = df.MASTER_OPS_ROWID.astype("int64")
    df["RUN_TYPE_MASTER_TODAY"] = df.RUN_TYPE_MASTER_TODAY.map(
        lambda v: np.nan if pd.isna(v) else str(v).strip())
    df["CURRENT_REV"] = df.CURRENT_REV.map(_rev)
    df["WO_REV"] = df.WO_REV.map(_rev)
    df["LT_TYPE"] = df.LT_TYPE.astype(str).str.strip()
    df["LAST_TRANSACTION_DATE"] = pd.to_datetime(df.LAST_TRANSACTION_DATE,
                                                 errors="coerce")

    # One value per master line, or the lookup is meaningless. fts.sql carries
    # them with MAX() over a group that already holds MASTER_OPS_ROWID, so this
    # can only fail if that query changes - and then it must fail loudly.
    n = df.groupby("MASTER_OPS_ROWID")[TODAY_COLS].nunique(dropna=True)
    bad = n[(n > 1).any(axis=1)]
    if len(bad):
        raise ValueError(f"{len(bad)} MASTER_OPS_ROWIDs carry more than one "
                         f"'today' value - the master lookup is not 1:1")
    today = df.groupby("MASTER_OPS_ROWID")[TODAY_COLS].first()

    fy = df[df.LAST_TRANSACTION_DATE >= pd.Timestamp(FY_START)]
    # tail(1), NOT .last(): groupby().last() skips missing values, so a line
    # whose most recent work order has no USER_8 would silently report an
    # OLDER order's revision. The most recent order's revision is wanted, empty
    # or not (7 work orders had none on 2026-09-28). A stable sort makes a
    # same-timestamp tie resolve the same way on every run.
    # ORIGINAL (2026-09-28): (fy.sort_values("LAST_TRANSACTION_DATE")
    #     .groupby(["MASTER_OPS_ROWID", "LT_TYPE"]).WO_REV.last())
    latest = (fy.sort_values("LAST_TRANSACTION_DATE", kind="stable")
                .groupby(["MASTER_OPS_ROWID", "LT_TYPE"]).tail(1))
    wo_rev = latest.set_index(["MASTER_OPS_ROWID", "LT_TYPE"]).WO_REV
    return today, wo_rev


def attach_today(d, current, recommended, today_col, lt_type, run_type,
                 lookup):
    """Append the STANDARD_WO / STANDARD_TODAY columns to one execution sheet.

    current / recommended name the sheet's own columns (the tested standard and
    the value to key); today_col the matching *_TODAY column; run_type the run
    type the sheet's arithmetic assumes (None for setup, a duration whatever
    the run type). Existing columns are read, never written.
    """
    if not len(d) or lookup is None:
        return d
    today, wo_rev = lookup
    # A left merge keeps the sheet's row order exactly; float keys on both
    # sides because a sheet's MASTER_OPS_ROWID is float wherever one is missing.
    rid = pd.to_numeric(d.MASTER_OPS_ROWID, errors="coerce").astype("float64")
    t = pd.DataFrame({"MASTER_OPS_ROWID": rid.to_numpy()}).merge(
        today.reset_index().astype({"MASTER_OPS_ROWID": "float64"}),
        on="MASTER_OPS_ROWID", how="left")
    assert len(t) == len(d), "today lookup changed the row count"
    a = pd.to_numeric(d[current], errors="coerce").to_numpy(dtype=float)
    c = pd.to_numeric(t[today_col], errors="coerce").to_numpy(dtype=float)
    rec = pd.to_numeric(d[recommended], errors="coerce").to_numpy(dtype=float)
    w_rev = [wo_rev.get((int(r), lt_type), np.nan) if pd.notna(r) else np.nan
             for r in rid]
    c_rev = t.CURRENT_REV.to_numpy()

    type_changed = np.zeros(len(d), dtype=bool)
    if run_type is not None:
        rt = t.RUN_TYPE_MASTER_TODAY.to_numpy()
        type_changed = np.array([pd.notna(x) and x != run_type for x in rt])
    same_value = np.abs(a - c) <= SAME_VALUE
    same_rev = np.array([pd.notna(x) and x == y for x, y in zip(w_rev, c_rev)])
    change = np.select(
        [np.isnan(c), type_changed, same_value, same_rev],
        ["no master line today", "run type changed", "unchanged",
         "changed within the same revision"],
        "changed with a newer revision")
    moved = ~np.isin(change, ["unchanged", "no master line today"])

    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.where((c > 0) & ~type_changed, rec / c, np.nan)
    pct = np.round((rel - 1) * 100, 1)

    out = d.copy()
    out["STANDARD_WO"] = d[current]
    out["STANDARD_TODAY"] = c
    out["STANDARD_CHANGE"] = change
    out["WO_REV"] = w_rev
    out["CURRENT_REV"] = c_rev
    out["CHANGE_PCT_TODAY"] = pct
    out["TODAY_STATUS"] = np.where(
        moved & ~type_changed & (np.abs(rel - 1) < MIN_EFFECT),
        f"already done - within {MIN_EFFECT:.0%} of today's master",
        np.where(type_changed, "check - run type changed on today's master",
                 "to key"))
    out["REVIEW_FLAG_TODAY"] = np.where(
        type_changed, "NEEDS REVIEW - run type changed on today's master",
        np.where(moved & ((rel < REVIEW_LO) | (rel > REVIEW_HI)),
                 f"NEEDS REVIEW - outside {REVIEW_LO}-{REVIEW_HI} of "
                 f"today's master", ""))
    return out


def action_progress(acts, db, setup, load):
    """How much of each action today's master already carries - 01_Actions.

    Counted from the execution rows, never re-derived: a row is done when
    attach_today() marked it "already done". Appends, after every existing
    column:
      N_PARTS_ALREADY_DONE  parts of this action already within MIN_EFFECT
      ACTION_STATUS         "already done" (every part) / "partly done - x of
                            n parts" / "to key"
    Decided with the user 2026-09-29: an action stays an action - the headline
    keeps its count and says how many are done - because a partly done action
    still has parts to key, and a done one is worth seeing, not hiding.

    Every execution row belongs to exactly one action (FAMILY rows to their
    family action, PART rows to their part action); that is asserted, so a
    change in how either side is keyed fails here instead of miscounting.
    """
    parts = [d for d in (db, setup, load) if len(d)]
    if not parts or any("TODAY_STATUS" not in d.columns for d in parts):
        return acts
    frames = []
    for d, target, rtype in ((db, "RUN", "PCS/HR"), (setup, "SETUP_HRS", "-"),
                             (load, "RUN", "HRS/LOAD")):
        if len(d):
            x = d[["OPERATION", "FAMILY", "PART_TYPE", "PART_ID", "SEQ", "ROUTE",
                   "TODAY_STATUS"]].copy()
            x["TARGET"], x["RUN_TYPE"] = target, rtype
            frames.append(x)
    r = pd.concat(frames, ignore_index=True)
    r["DONE"] = r.TODAY_STATUS.str.startswith("already done")
    r["LEVEL"] = np.where(r.ROUTE == "PART", "PART", "FAMILY")
    r["MATCH"] = np.where(r.LEVEL == "PART", r.PART_ID.astype(str), "")
    key = ["TARGET", "RUN_TYPE", "LEVEL", "OPERATION", "FAMILY", "PART_TYPE",
           "SEQ", "MATCH"]
    got = r.groupby(key, dropna=False).agg(N=("DONE", "size"),
                                           N_DONE=("DONE", "sum"))

    a = acts.rename(columns={"SEQUENCE": "SEQ"}).copy()
    a["MATCH"] = np.where(a.LEVEL == "PART", a.PART_ID.astype(str), "")
    j = a[key].merge(got.reset_index(), on=key, how="left")
    assert len(j) == len(acts), "action lookup changed the row count"
    covered = int(j.N.fillna(0).sum())
    if covered != len(r):
        raise ValueError(f"{len(r) - covered} execution rows match no action - "
                         f"the action and row keys have drifted apart")

    n, done = j.N.fillna(0).astype(int).to_numpy(), \
        j.N_DONE.fillna(0).astype(int).to_numpy()
    out = acts.copy()
    out["N_PARTS_ALREADY_DONE"] = done
    out["ACTION_STATUS"] = np.where(
        (n > 0) & (done == n), "already done",
        np.where(done > 0,
                 ["partly done - " + f"{x} of {y} parts" for x, y in zip(done, n)],
                 "to key"))
    return out


# What a whole-family action says when its parts do not share one value, by
# the field it edits: the field's name in the instruction, and whether the
# correction divides (a rate) or multiplies (a duration).
SEVERAL = {
    ("RUN", "PCS/HR"): "family holds several RUN_MASTER values - divide each "
                       "part's own RUN_MASTER by RATIO",
    ("SETUP_HRS", "-"): "family holds several SETUP_HRS values - multiply each "
                        "part's own SETUP_HRS by RATIO",
    ("RUN", "HRS/LOAD"): "family holds several RUN (hours per load) values - "
                         "multiply each part's own RUN (hours per load) by "
                         "RATIO",
}
# ...and when they do: the wording each family action was built with.
ONE = {
    ("RUN", "PCS/HR"): "set RUN_MASTER to SUGGESTED_VALUE for every part listed",
    ("SETUP_HRS", "-"): "set SETUP_HRS to SUGGESTED_VALUE for every part "
                        "listed",
    ("RUN", "HRS/LOAD"): "set RUN (hours per load) to SUGGESTED_VALUE for "
                         "every part listed",
}


def family_values(acts, db, setup, load):
    """A whole-family action's CURRENT_VALUE and SUGGESTED_VALUE, read off the
    rows that key it - 01_Actions.

    ONE VALUE OR NONE (user, 2026-10-07). A family action shows a value only
    where every part it covers holds that value on its execution row - the
    standard the work orders ran with, and the value to key. Where the parts
    differ there is no single number to key, both stay blank, and HOW_TO_APPLY
    says to apply RATIO to each part's own value instead. CHANGE_PCT is then
    blank too, as it always was on PCS/HR.

    WHY NOT THE POOL'S OWN FIGURE, which these columns carried until now. It is
    a different number on each analysis - PCS/HR the hours-weighted rate, setup
    the median of the stored values across the setups, HRS/LOAD the average
    per work order - and none of them is a value any routing line holds. On
    the 7 Oct 2026 extract it put a single value on 8 setup and HRS/LOAD
    actions whose parts hold two (one showed 1.58 to key where the rows key
    1.58 and 3.15), and SUGGESTED_VALUE 0.1 away from the keyed value on 5
    more, where pool / ratio and part / ratio rounded to different sides of a
    tenth. 02/03/04 were right throughout; this sheet now reads them.

    Part actions are untouched: each is one row, and its own already.
    """
    src = {("RUN", "PCS/HR"): (db, "RM_CURRENT", "RM_RECOMMENDED"),
           ("SETUP_HRS", "-"): (setup, "SETUP_HRS_CURRENT",
                                "SETUP_HRS_RECOMMENDED"),
           ("RUN", "HRS/LOAD"): (load, "RUN_CURRENT", "RUN_RECOMMENDED")}

    def one(s):
        s = pd.to_numeric(s, errors="coerce")
        return (float(s.iloc[0]) if len(s) and s.notna().all()
                and s.round(4).nunique() == 1 else np.nan)

    out = acts.copy()
    for i in out.index[out.LEVEL == "FAMILY"]:
        r = out.loc[i]
        d, cur, rec = src[(r.TARGET, r.RUN_TYPE)]
        if not len(d):
            continue
        m = d[(d.ROUTE == "FAMILY") & (d.OPERATION == r.OPERATION)
              & (d.FAMILY == r.FAMILY) & (d.PART_TYPE == r.PART_TYPE)
              & (d.SEQ == r.SEQUENCE)]
        c = one(m[cur])
        s = one(m[rec]) if not np.isnan(c) else np.nan
        out.at[i, "CURRENT_VALUE"] = c
        out.at[i, "SUGGESTED_VALUE"] = s
        out.at[i, "CHANGE_PCT"] = (round((s / c - 1) * 100, 1)
                                   if not np.isnan(s) and c else np.nan)
        out.at[i, "HOW_TO_APPLY"] = (SEVERAL if np.isnan(s)
                                     else ONE)[(r.TARGET, r.RUN_TYPE)]
    return out


def tidy(v):
    """Round a suggested rate for keying in.

    One decimal above 10, two below. A flat 1dp would be fine for the big
    values but distorts the small ones badly - 0.74 becomes 0.70, a 5.4% shift,
    which eats half the 10% materiality threshold the row had to clear.
    """
    return np.where(pd.isna(v), np.nan,
                    np.where(np.abs(v) >= 10, np.round(v, 1), np.round(v, 2)))


def load_rowids():
    """The master routing row for each part+sequence.

    MASTER_OPS_ROWID points at the master operation, not at a work order, so
    it is one value per part+sequence however many times the step has run.
    That is the row an UPDATE has to target for future work orders to inherit
    the corrected standard.
    """
    df = pd.read_parquet(CACHE)
    for c in ["FAMILY", "PART_TYPE", "PART_ID", "OPERATION", "LT_TYPE",
              "RUN_TYPE_MASTER"]:
        df[c] = df[c].str.strip()
    df["LAST_TRANSACTION_DATE"] = pd.to_datetime(df["LAST_TRANSACTION_DATE"],
                                                 errors="coerce")
    scope = ((df.LT_TYPE == "Run")
             & (df.RUN_TYPE_MASTER == "PCS/HR")
             & (df.LAST_TRANSACTION_DATE >= pd.Timestamp(FY_START))
             & (df.RUN_MASTER > 0)
             & df.MASTER_OPS_ROWID.notna())
    g = (df[scope].sort_values("LAST_TRANSACTION_DATE")
         .groupby(["OPERATION", "FAMILY", "PART_ID", "OPERATION_SEQUENCE"],
                  dropna=False))
    out = pd.DataFrame({
        "MASTER_OPS_ROWID": g.MASTER_OPS_ROWID.max(),
        "N_ROWIDS": g.MASTER_OPS_ROWID.nunique(),
        "WORK_ORDERS_SEEN": g.WORK_ORDER.nunique(),
        "LAST_DATE": g.LAST_TRANSACTION_DATE.max(),
    }).reset_index()
    bad = out[out.N_ROWIDS > 1]
    if len(bad):
        raise ValueError(f"{len(bad)} part+sequence rows map to more than one "
                         "MASTER_OPS_ROWID - the id is not master-level")
    return out.drop(columns="N_ROWIDS")


def build_token_report():
    """Every work order dropped as a token booking, by operation and family.

    The guard is upstream and silent by design - the affected units simply do
    not produce a suggestion. That silence is exactly what has to be visible,
    because a step booked at a hundredth of an hour is not a step running fast,
    it is a step nobody is recording. The standard may be perfectly wrong and
    this analysis will never say so, and the reason belongs in the workbook
    rather than in a commit message.

    STANDARD_CHARGED is the allowance those work orders carried: hours the
    schedule reserved and the costing charged against jobs that produced no
    evidence they were performed.
    """
    df = pd.read_parquet(CACHE)
    for c in ["FAMILY", "PART_TYPE", "PART_ID", "OPERATION", "LT_TYPE",
              "RUN_TYPE_MASTER"]:
        df[c] = df[c].str.strip()
    df["LAST_TRANSACTION_DATE"] = pd.to_datetime(df["LAST_TRANSACTION_DATE"],
                                                 errors="coerce")
    scope = ((df.LT_TYPE == "Run")
             & (df.LAST_TRANSACTION_DATE >= pd.Timestamp(FY_START))
             & (df.HOURS_MASTER > 0) & (df.HOURS_WORKED > 0)
             & ~df.OPERATION.str.upper().str.startswith("S RIL")
             & df.MASTER_OPS_ROWID.notna())
    # RUN_TYPE_MASTER is in the key because both run analyses apply the guard
    # and they land in different workbooks - without it a reader cannot tell
    # which analysis a row belongs to. S SABBIARE is HRS/LOAD and dominates the
    # counts; only the PCS/HR rows bear on 02_RunUpdateList.
    key = ["RUN_TYPE_MASTER", "OPERATION", "FAMILY", "PART_TYPE",
           "OPERATION_SEQUENCE"]
    wo = (df[scope].groupby(key + ["PART_ID", "WORK_ORDER"], as_index=False)
          .agg(TOTAL_QTY=("TOTAL_QTY", "sum"),
               HOURS_WORKED=("HOURS_WORKED", "sum"),
               HOURS_MASTER=("HOURS_MASTER", "sum")))
    wo["TOKEN"] = wo.HOURS_WORKED <= TOKEN_HOURS

    g = (wo.groupby(key)
         .agg(PARTS=("PART_ID", "nunique"), WO_ALL=("WORK_ORDER", "size"),
              WO_TOKEN=("TOKEN", "sum"))
         .reset_index())
    t = (wo[wo.TOKEN].groupby(key)
         .agg(PIECES=("TOTAL_QTY", "sum"),
              HOURS_BOOKED=("HOURS_WORKED", "sum"),
              STANDARD_CHARGED=("HOURS_MASTER", "sum"))
         .reset_index())
    g = g.merge(t, on=key, how="inner")
    g["TOKEN_RATE"] = (g.WO_TOKEN / g.WO_ALL).round(3)
    g["WO_REAL"] = g.WO_ALL - g.WO_TOKEN
    # the only ones the guard actually changed the answer for: without it these
    # would have been tested, with it they fall short of the floor
    g["BELOW_FLOOR_AFTER"] = g.WO_REAL < MIN_WO
    for c in ["HOURS_BOOKED", "STANDARD_CHARGED"]:
        g[c] = g[c].round(2)
    return g.sort_values(["WO_TOKEN", "STANDARD_CHARGED"],
                         ascending=False)[key + [
                             "PARTS", "WO_ALL", "WO_TOKEN", "WO_REAL",
                             "TOKEN_RATE", "BELOW_FLOOR_AFTER", "PIECES",
                             "HOURS_BOOKED", "STANDARD_CHARGED"]]


def build_family_rollup(acts, edits):
    """The same actions grouped by family instead of by operation.

    00_Summary answers "which operation holds the work". This answers "which
    family is worst", which is the question asked by anyone who owns a product
    line rather than a process.

    GROSS_HOURS sums |HOURS_DELTA|, so a family with one step too fast and
    another too slow counts both - they are two wrong standards. NET is beside
    it only to show how much cancels. PART_TYPE stays in the key because the
    semi-finished and finished halves are never pooled anywhere else either.

    PARTS is distinct parts, taken from the execution sheets rather than summed
    across actions: the same part appears at several operations, so adding
    N_PARTS up would count it once per operation it touches.
    """
    def roll(keys, spread=None):
        g = (acts.groupby(keys)
             .agg(ACTIONS=("HOURS_DELTA", "size"),
                  GROSS_HOURS=("HOURS_DELTA", lambda s: s.abs().sum()),
                  NET_HOURS=("HOURS_DELTA", "sum"))
             .reset_index())
        if len(edits):
            e = (edits.groupby(keys)
                 .agg(PARTS=("PART_ID", "nunique"), EDITS=("PART_ID", "size"))
                 .reset_index())
            g = g.merge(e, on=keys, how="left")
        else:
            g[["PARTS", "EDITS"]] = 0
        if spread:
            col, src = spread
            g = g.merge(acts.groupby(keys)[src].nunique().rename(col)
                        .reset_index(), on=keys, how="left")
        g["SHARE_PCT"] = (100 * g.GROSS_HOURS / g.GROSS_HOURS.sum()).round(1)
        g = g.sort_values("GROSS_HOURS", ascending=False)
        g[["GROSS_HOURS", "NET_HOURS"]] = g[["GROSS_HOURS",
                                             "NET_HOURS"]].round(1)
        total = {k: ("TOTAL" if k == keys[0] else "") for k in keys}
        total.update(ACTIONS=g.ACTIONS.sum(),
                     GROSS_HOURS=round(g.GROSS_HOURS.sum(), 1),
                     NET_HOURS=round(g.NET_HOURS.sum(), 1),
                     PARTS=edits.PART_ID.nunique() if len(edits) else 0,
                     EDITS=len(edits), SHARE_PCT=100.0)
        if spread:
            total[spread[0]] = acts[spread[1]].nunique()
        cols = keys + ["ACTIONS"] + ([spread[0]] if spread else []) + \
            ["PARTS", "EDITS", "GROSS_HOURS", "NET_HOURS", "SHARE_PCT"]
        return pd.concat([g, pd.DataFrame([total])], ignore_index=True)[cols]

    return (roll(["FAMILY", "PART_TYPE"], spread=("OPERATIONS", "OPERATION")),
            roll(["FAMILY", "PART_TYPE", "OPERATION"]))


def build_summary(acts, analysed):
    """One page covering every operation looked at, run and setup.

    Two things it does that the raw groupby does not.

    Both hours columns come from HOURS_DELTA on the action - worked minus
    allowed, over the year, for that group. HOURS_DELTA_NET sums it signed;
    HOURS_DELTA_ABSOLUTE discards the signs first. Netting is the wrong lens: an
    operation whose families are half too fast and half too slow is not half
    right, it is wrong twice, and the net figure hides exactly that. Across the
    whole set the two differ by a factor of twenty.

    Operations that were analysed and came out clean are listed with zero
    actions. Without them "we tested this and the standard holds" is
    indistinguishable from "we never got to it".

    Those split two ways, and the LEVEL text says which. An operation where
    something cleared the MIN_WO floor and nothing material came out really does
    hold. An operation where nothing was testable at all - too few work orders,
    or too few left once token bookings were dropped - has no verdict either
    way, and calling it clean would claim evidence that does not exist.
    """
    g = (acts.groupby(["OPERATION", "TARGET", "LEVEL"], dropna=False)
             .agg(ACTIONS=("PART_ID", "size"),
                  PARTS_AFFECTED=("N_PARTS", "sum"),
                  HOURS_DELTA_ABSOLUTE=("HOURS_DELTA", lambda s: s.abs().sum()),
                  HOURS_DELTA_NET=("HOURS_DELTA", "sum"))
             .reset_index())

    clean = sorted(set(analysed) - set(acts.OPERATION))
    if clean:
        g = pd.concat([g, pd.DataFrame({
            "OPERATION": clean, "TARGET": "-",
            "LEVEL": ["none - tested, holds" if analysed[o]
                      else "none - nothing testable" for o in clean],
            "ACTIONS": 0, "PARTS_AFFECTED": 0,
            "HOURS_DELTA_ABSOLUTE": 0.0, "HOURS_DELTA_NET": 0.0})], ignore_index=True)

    # heaviest operations first - that is the question a summary is opened to
    # answer, and Excel can re-sort alphabetically in one click
    order = g.groupby("OPERATION").HOURS_DELTA_ABSOLUTE.sum().rank(ascending=False)
    g = (g.assign(_o=g.OPERATION.map(order))
           .sort_values(["_o", "OPERATION", "TARGET", "LEVEL"])
           .drop(columns="_o"))

    total = pd.DataFrame([{
        "OPERATION": "TOTAL", "TARGET": "", "LEVEL": "",
        "ACTIONS": g.ACTIONS.sum(), "PARTS_AFFECTED": g.PARTS_AFFECTED.sum(),
        "HOURS_DELTA_ABSOLUTE": g.HOURS_DELTA_ABSOLUTE.sum(), "HOURS_DELTA_NET": g.HOURS_DELTA_NET.sum()}])
    out = pd.concat([g, total], ignore_index=True)
    out[["HOURS_DELTA_ABSOLUTE", "HOURS_DELTA_NET"]] = out[["HOURS_DELTA_ABSOLUTE", "HOURS_DELTA_NET"]].round(0)
    return out


def main():
    rowids = load_rowids()
    actions, per_part, fam_alt = [], [], []
    # every operation looked at -> did anything in it clear the MIN_WO floor.
    # An operation with no action is only "tested, holds" if something in it was
    # actually testable; where the token guard or the floor left nothing to test,
    # saying the standard holds would be a claim the data does not support.
    analysed = {}

    # ZERO WORKBOOKS IS AN ERROR, NOT AN EMPTY LOOP. This glob is the only way
    # the run analysis reaches the deliverable. Point it somewhere with nothing
    # in it - a folder reorganised, the script run from the wrong directory, a
    # sweep that never happened - and every statement below simply has no rows
    # to make. The result is not a crash: it is a complete, well-formed update
    # list carrying setup and HRS/LOAD and no run actions at all, which reads
    # exactly like an extract where nothing needed changing.
    #
    # That is this project's dominant failure shape, and it is the one place a
    # wrong path produces silence instead of a traceback. So it stops here.
    parts_files = sorted(glob.glob(os.path.join(OUTPUTS,
                                                "FTS_*_PARTS.xlsx")))
    if not parts_files:
        sys.exit("no FTS_*_PARTS.xlsx found in "
                 f"{os.getcwd()}\n"
                 "  The run analysis has nothing to consolidate. Writing the "
                 "update list now\n"
                 "  would produce a deliverable with setup and HRS/LOAD only "
                 "and no run\n"
                 "  actions - indistinguishable from an extract where no run "
                 "standard moved.\n"
                 "  Run fts_analysis.py over the operations first "
                 "(python run_all.py).")

    for pf in parts_files:
        tag = re.sub(r"^FTS_|_PARTS\.xlsx$", "",
                     os.path.basename(pf))
        ff = os.path.join(OUTPUTS, f"FTS_{tag}_FAMILY.xlsx")

        allunits = pd.read_excel(pf, sheet_name="05_AllUnits_WithEvidence")
        # An operation can legitimately produce no units at all - every work
        # order a token booking - so the unit sheet is empty and cannot supply
        # the operation name. 00_Summary always carries it, and the operation
        # still has to appear in the summary: "we looked and there was nothing
        # to test" is a different statement from "we never got to it", and the
        # whole point of listing action-free operations is to keep them apart.
        if allunits.empty:
            s = pd.read_excel(pf, sheet_name="00_Summary").set_index("METRIC")
            analysed[str(s.loc["Operation", "VALUE"]).strip()] = False
            continue

        op = allunits.OPERATION.iat[0]
        analysed[op] = bool((allunits.N_WORK_ORDERS >= MIN_WO).any())
        p = allunits[allunits.RECOMMENDATION == "UPDATE RUN_MASTER"]

        # ---- part route -------------------------------------------------
        if len(p):
            actions.append(pd.DataFrame({
                "TARGET": "RUN",
                "LEVEL": "PART", "OPERATION": p.OPERATION, "FAMILY": p.FAMILY,
                "PART_TYPE": p.PART_TYPE,
                "PART_ID": p.PART_ID, "SEQ": p.OPERATION_SEQUENCE,
                "N_PARTS": 1, "N_WO": p.N_WORK_ORDERS,
                "RATIO": p.RATIO_OUTLIERS_REMOVED.round(3),
                "CI95_LOW": p.CI95_LOW.round(3),
                "CI95_HIGH": p.CI95_HIGH.round(3),
                "HOURS_DELTA": p.HOURS_DELTA,
                "CURRENT_VALUE": p.RUN_MASTER_CURRENT,
                "SUGGESTED_VALUE": tidy(p.RUN_MASTER_SUGGESTED),
                "HOW_TO_APPLY": "set this part's RUN_MASTER to SUGGESTED_VALUE"}))
            per_part.append(pd.DataFrame({
                "ROUTE": "PART", "OPERATION": p.OPERATION, "FAMILY": p.FAMILY,
                "PART_TYPE": p.PART_TYPE,
                "PART_ID": p.PART_ID, "SEQ": p.OPERATION_SEQUENCE,
                "N_WO_EVIDENCE": p.N_WORK_ORDERS,
                "RATIO": p.RATIO_OUTLIERS_REMOVED.round(3),
                "CI95_LOW": p.CI95_LOW, "CI95_HIGH": p.CI95_HIGH,
                "RM_CURRENT": p.RUN_MASTER_CURRENT,
                "RM_SUGGESTED": tidy(p.RUN_MASTER_SUGGESTED)}))

        try:
            roll = pd.read_excel(ff, sheet_name="01_FamilyRollup")
        except FileNotFoundError:
            continue
        # every family+sequence, whatever its verdict - the "apply one value to
        # the whole family" option needs a figure even where the family was not
        # itself flagged, and it is based on FAM_RATIO (all parts, including any
        # flagged individually) because that option treats them as one group
        fam_alt.append(roll[["FAMILY", "PART_TYPE", "OPERATION_SEQUENCE",
                             "FAM_RATIO", "FAM_PARTS", "FAM_WO",
                             "FAMEX_VERDICT"]]
                       .assign(OPERATION=op))
        # a family pool can be testable where no single part is
        analysed[op] |= bool((roll.FAMEX_WO >= MIN_WO).any())
        f = roll[roll.FAMEX_VERDICT == "UPDATE FAMILY STANDARD"]
        if not len(f):
            continue

        single = f.FAMEX_N_DISTINCT_RM == 1
        actions.append(pd.DataFrame({
            "TARGET": "RUN",
            "LEVEL": "FAMILY", "OPERATION": f.OPERATION, "FAMILY": f.FAMILY,
            "PART_TYPE": f.PART_TYPE,
            "PART_ID": np.where(
                f.PARTS_FLAGGED_UPDATE > 0,
                "all parts except the " + f.PARTS_FLAGGED_UPDATE.astype(str)
                + " flagged individually", "all parts of this family"),
            "SEQ": f.OPERATION_SEQUENCE, "N_PARTS": f.FAMEX_PARTS,
            "N_WO": f.FAMEX_WO, "RATIO": f.FAMEX_RATIO.round(3),
            "CI95_LOW": f.FAMEX_CI95_LOW.round(3),
            "CI95_HIGH": f.FAMEX_CI95_HIGH.round(3),
            "HOURS_DELTA": f.FAMEX_HOURS_DELTA,
            "CURRENT_VALUE": np.where(single, f.FAMEX_RM_CURRENT, np.nan),
            "SUGGESTED_VALUE": np.where(single, tidy(f.FAMEX_RM_SUGGESTED),
                                        np.nan),
            "HOW_TO_APPLY": np.where(
                single,
                "set RUN_MASTER to SUGGESTED_VALUE for every part listed",
                "family holds several RUN_MASTER values - divide each part's "
                "own RUN_MASTER by RATIO")}))

        # ---- family route, expanded to the individual parts it covers ----
        # Each part keeps its own current RUN_MASTER and is corrected by the
        # family factor, so a family running several values stays consistent
        # and every row ends up with a concrete number to key in.
        flagged = set(p.PART_ID)
        members = allunits[["FAMILY", "PART_TYPE", "PART_ID",
                            "OPERATION_SEQUENCE", "OPERATION",
                            "RUN_MASTER_CURRENT"]]
        for _, fam in f.iterrows():
            # PART_TYPE is part of the match: the family correction was
            # measured on one type only and must not be applied to the other
            m = members[(members.FAMILY == fam.FAMILY)
                        & (members.PART_TYPE == fam.PART_TYPE)
                        & (members.OPERATION_SEQUENCE == fam.OPERATION_SEQUENCE)
                        & ~members.PART_ID.isin(flagged)]
            if not len(m):
                continue
            per_part.append(pd.DataFrame({
                "ROUTE": "FAMILY", "OPERATION": m.OPERATION,
                "FAMILY": m.FAMILY, "PART_TYPE": m.PART_TYPE,
                "PART_ID": m.PART_ID,
                "SEQ": m.OPERATION_SEQUENCE,
                "N_WO_EVIDENCE": fam.FAMEX_WO,
                "RATIO": round(fam.FAMEX_RATIO, 3),
                "RM_CURRENT": m.RUN_MASTER_CURRENT,
                "RM_SUGGESTED": tidy(m.RUN_MASTER_CURRENT / fam.FAMEX_RATIO)}))

    acts = pd.concat(actions, ignore_index=True)
    acts["CHANGE_PCT"] = ((acts.SUGGESTED_VALUE / acts.CURRENT_VALUE - 1)
                          * 100).round(1)

    parts = pd.concat(per_part, ignore_index=True)
    parts["CHANGE_PCT"] = ((parts.RM_SUGGESTED / parts.RM_CURRENT - 1) * 100).round(1)

    # What every part would get if the whole family were set to one value,
    # overriding the part-specific numbers. Lets a reviewer trade precision for
    # a single consistent standard across the family.
    alt = (pd.concat(fam_alt, ignore_index=True)
             .rename(columns={"OPERATION_SEQUENCE": "SEQ",
                              "FAM_RATIO": "FAMILY_RATIO_ALL_PARTS",
                              "FAM_PARTS": "FAMILY_N_PARTS",
                              "FAM_WO": "FAMILY_N_WO",
                              "FAMEX_VERDICT": "FAMILY_VERDICT"})
             .drop_duplicates(["OPERATION", "FAMILY", "PART_TYPE", "SEQ"]))
    parts = parts.merge(alt, on=["OPERATION", "FAMILY", "PART_TYPE", "SEQ"],
                        how="left")
    parts["RM_SUGGESTED_IF_WHOLE_FAMILY"] = tidy(
        parts.RM_CURRENT / parts.FAMILY_RATIO_ALL_PARTS)

    # Does the part actually behave differently from its family, or is the
    # difference sampling noise? If the family ratio falls inside the part's
    # own confidence interval the two are indistinguishable, and keeping a
    # separate standard preserves noise rather than a real process difference.
    inside = (parts.CI95_LOW <= parts.FAMILY_RATIO_ALL_PARTS) &              (parts.FAMILY_RATIO_ALL_PARTS <= parts.CI95_HIGH)
    parts["PART_DIFFERS_FROM_FAMILY"] = np.where(
        parts.ROUTE != "PART", pd.NA, ~inside)

    # ================== THE RULE, AS IT STANDS TODAY ======================
    # A PART THAT EARNED ITS OWN SUGGESTION KEEPS IT. Always. No exceptions.
    #
    # The three analyses agree on this and always have:
    #   - the part route tests a part on its own evidence; if all six rules
    #     pass, that part's own figure is the answer
    #   - the family route pools the parts that did NOT pass alone, and
    #     fts_family_view.py excludes the flagged ones from that pool first
    #     (FAMEX), so the family figure never covers a part that has its own
    #   - HRS/LOAD implements exactly this - 04_LoadUpdateList has a single
    #     RUN_RECOMMENDED column and no substitution at all
    #   - setup is family-only, so the question does not arise there
    #
    # So RM_RECOMMENDED IS RM_SUGGESTED, on BOTH routes. RM_SUGGESTED already
    # carries the right figure either way:
    #
    #   part route   - the part's own evidence
    #   family route - RUN_MASTER_CURRENT / FAMEX_RATIO, per part, where the
    #                  member list is built with ~PART_ID.isin(flagged) and
    #                  FAMEX_RATIO is pooled over the family MINUS the parts
    #                  flagged individually
    #
    # CORRECTED 2026-08-27. Until then the family branch took
    # RM_SUGGESTED_IF_WHOLE_FAMILY, which divides by FAM_RATIO - the pool
    # INCLUDING the flagged parts. That is the same circularity described at
    # length below, on the other route, and it survived the 2026-08-26 fix
    # because that fix only corrected the part branch.
    #
    # It made 02_RunUpdateList contradict 01_Actions and the Review queue on 65
    # rows across 7 family x step units, by -9.4% to +3.6%: the queue read the
    # FAMEX figure from 01_Actions while the execution sheet wrote the
    # all-parts one.
    parts["RM_RECOMMENDED"] = parts.RM_SUGGESTED
    parts["RECOMMENDED_BASIS"] = np.where(parts.ROUTE == "PART",
                                          "part", "family")

    # ============ WHAT USED TO BE HERE, AND WHY IT IS NOT =================
    #
    # READ THIS BEFORE "RESTORING" ANYTHING. The code below is NOT a fallback,
    # NOT a variant to switch back to, and NOT dead code kept for convenience.
    # It is a rule that was WRONG and was removed on 2026-08-26. It is written
    # out only so that nobody re-derives it from scratch believing it to be an
    # improvement. If you are tempted to reinstate it, the paragraph after it
    # explains why it does not work.
    #
    #     fam_ok = parts.FAMILY_VERDICT == "UPDATE FAMILY STANDARD"
    #     keep_part = (parts.ROUTE == "PART") & (
    #         parts.PART_DIFFERS_FROM_FAMILY.fillna(False).astype(bool)
    #         | ~fam_ok)
    #
    # It meant: a flagged part keeps its own value only where the family ratio
    # falls OUTSIDE the part's 95% interval; otherwise the two are
    # statistically indistinguishable and the family value is applied instead,
    # on the reasoning that a separate standard would encode sampling noise.
    #
    # WHY IT IS WRONG. The comparison and the substitute both use
    # FAMILY_RATIO_ALL_PARTS, which is pooled over ALL parts of the family -
    # including the very parts being overwritten. So a part was measured
    # against a figure it had itself helped produce, and then replaced by it.
    # That is the exact circularity the FAMEX exclusion in
    # fts_family_view.py exists to prevent: it drops flagged parts from the
    # family pool precisely so the family figure is not propped up by the
    # parts already known to be off. This rule put them back.
    #
    # Concretely, on the 2026-08-26 extract, one stem family at a final-check
    # step: a few parts flagged individually, FAMEX_RATIO computed over the
    # remaining ones - but the flagged parts were overwritten using FAM_RATIO,
    # computed over all of them, the flagged ones included.
    #
    # It moved about a quarter of the part rows, worth hundreds of hours, and made 01_Actions contradict
    # 02_RunUpdateList in the same workbook: HOW_TO_APPLY said "set this part's
    # RUN_MASTER to SUGGESTED_VALUE" while RM_RECOMMENDED held a different
    # number.
    #
    # PART_DIFFERS_FROM_FAMILY and RM_SUGGESTED_IF_WHOLE_FAMILY are still
    # computed and still published. RM_SUGGESTED_IF_WHOLE_FAMILY is a genuine
    # alternative scenario - "what if we applied ONE value to the whole family,
    # flagged parts included" - and is correctly pooled over all parts for that
    # purpose. It is CONTEXT for a reviewer overriding a row by eye. Neither
    # decides anything, on either route.
    # ======================================================================
    # fall back to the part value where no family figure exists
    no_fam = parts.RM_RECOMMENDED.isna()
    parts.loc[no_fam, "RM_RECOMMENDED"] = parts.loc[no_fam, "RM_SUGGESTED"]
    parts.loc[no_fam, "RECOMMENDED_BASIS"] = "part - no family figure available"
    parts["FAMILY_RATIO_ALL_PARTS"] = parts.FAMILY_RATIO_ALL_PARTS.round(3)
    parts["CHANGE_PCT_RECOMMENDED"] = (
        (parts.RM_RECOMMENDED / parts.RM_CURRENT - 1) * 100).round(1)

    # ---- attach the database row ids --------------------------------------
    # One row per part. A part maps to many OPERATION_ROWIDs (one per work
    # order that ran the step), so they travel as a list on that single row.
    key = ["OPERATION", "FAMILY", "PART_ID", "SEQ"]
    db = (parts.merge(rowids.rename(columns={"OPERATION_SEQUENCE": "SEQ"}),
                      on=key, how="left")
               .sort_values(["OPERATION", "FAMILY", "SEQ", "ROUTE", "PART_ID"]))
    db["ROWID_FOUND"] = db.MASTER_OPS_ROWID.notna()
    # Which field on the routing line this row edits. Run and setup corrections
    # land on the SAME MASTER_OPS_ROWID but different columns, so without this
    # the combined list is ambiguous at exactly the point of execution.
    db["TARGET_COLUMN"] = "RUN"
    db = db[["OPERATION", "FAMILY", "PART_TYPE", "PART_ID", "SEQ", "ROUTE",
             "TARGET_COLUMN",
             "MASTER_OPS_ROWID", "ROWID_FOUND", "RM_CURRENT",
             # --- the one to key in ---
             "RM_RECOMMENDED", "CHANGE_PCT_RECOMMENDED", "RECOMMENDED_BASIS",
             # --- the two options it chose between ---
             "RM_SUGGESTED", "RM_SUGGESTED_IF_WHOLE_FAMILY",
             "PART_DIFFERS_FROM_FAMILY",
             # --- evidence ---
             "RATIO", "CI95_LOW", "CI95_HIGH", "N_WO_EVIDENCE",
             "FAMILY_RATIO_ALL_PARTS", "FAMILY_N_PARTS", "FAMILY_N_WO",
             "FAMILY_VERDICT", "CHANGE_PCT", "WORK_ORDERS_SEEN", "LAST_DATE"]]
    db = db.rename(columns=CI_NAMES)

    # ---- setup, if the setup analysis has been run ------------------------
    # Its update list is kept as its own sheet rather than appended to the run
    # list: the value to key in lives in a different column, the correction
    # multiplies instead of dividing, and the two are reviewed by different
    # people. Merging them would need a nullable column per target and invite
    # keying the wrong number into the wrong field. The *actions*, on the other
    # hand, do belong together - a reviewer wants one list of decisions.
    setup, setup_acts = pd.DataFrame(), pd.DataFrame()
    if os.path.exists(SETUP_SRC):
        setup = pd.read_excel(SETUP_SRC, sheet_name="04_SetupUpdateList")
        setup = setup.rename(columns={"OPERATION_SEQUENCE": "SEQ"})
        setup["ROWID_FOUND"] = setup.MASTER_OPS_ROWID.notna()
        setup = setup[["OPERATION", "FAMILY", "PART_TYPE", "PART_ID", "SEQ",
                       "ROUTE", "TARGET_COLUMN", "MASTER_OPS_ROWID",
                       "ROWID_FOUND",
                       "SETUP_HRS_CURRENT",
                       # --- the one to key in ---
                       "SETUP_HRS_RECOMMENDED", "CHANGE_PCT",
                       # --- evidence ---
                       "RATIO", "N_SETUPS", "LAST_DATE"]]

        sa = pd.read_excel(SETUP_SRC, sheet_name="02_SetupActions")
        # PARTS on the action counts every part in the family's test pool, but
        # a part whose setup was skipped on every work order gets no edit - it
        # has no real setup to correct. Counting the rows that will actually be
        # keyed in keeps the summary honest: 587 tested, 563 edited.
        key = ["OPERATION", "FAMILY", "PART_TYPE", "OPERATION_SEQUENCE"]
        edited = (setup.rename(columns={"SEQ": "OPERATION_SEQUENCE"})
                       .groupby(key).PART_ID.size())
        setup_acts = pd.DataFrame({
            "TARGET": "SETUP_HRS",
            # setup is judged per family+sequence only - there is no part-level
            # setup test, so every setup action is a family action
            "LEVEL": "FAMILY", "OPERATION": sa.OPERATION, "FAMILY": sa.FAMILY,
            "PART_TYPE": sa.PART_TYPE,
            "PART_ID": "every part of this family that performs the setup",
            "SEQ": sa.OPERATION_SEQUENCE,
            "N_PARTS": sa.set_index(key).index.map(edited).fillna(0).astype(int),
            "N_WO": sa.WO_REAL, "RATIO": sa.RATIO_OUTLIERS_REMOVED.round(3),
            "CI95_LOW": sa.CI95_LOW.round(3),
            "CI95_HIGH": sa.CI95_HIGH.round(3),
            "HOURS_DELTA": sa.HOURS_DELTA,
            "CURRENT_VALUE": sa.SETUP_HRS_CURRENT,
            "SUGGESTED_VALUE": sa.SETUP_HRS_SUGGESTED,
            "CHANGE_PCT": sa.CHANGE_PCT,
            "HOW_TO_APPLY": "set SETUP_HRS to SUGGESTED_VALUE for every part "
                            "listed"})
        acts = pd.concat([acts, setup_acts], ignore_index=True)

    # ---- HRS/LOAD, if the load analysis has been run ----------------------
    # Same arrangement as setup: its own execution sheet, but its actions join
    # the one review list. It lands on the RUN column like PCS/HR does, yet the
    # correction MULTIPLIES rather than divides, because RUN is hours per load -
    # a duration - not a rate. RUN_TYPE on every action row is what keeps the
    # two readable side by side.
    load, load_acts = pd.DataFrame(), pd.DataFrame()
    if os.path.exists(LOAD_SRC):
        load = pd.read_excel(LOAD_SRC, sheet_name="03_LoadUpdateList")
        load = load.rename(columns={"OPERATION_SEQUENCE": "SEQ"})
        load = load[["OPERATION", "FAMILY", "PART_TYPE", "PART_ID", "SEQ",
                     "ROUTE", "TARGET_COLUMN", "MASTER_OPS_ROWID",
                     "ROWID_FOUND", "RUN_CURRENT",
                     # --- the one to key in ---
                     "RUN_RECOMMENDED", "CHANGE_PCT",
                     # --- evidence ---
                     "RATIO", "N_WO_EVIDENCE", "RUN_TYPE_MASTER", "LAST_DATE"]]

        lf = pd.read_excel(LOAD_SRC, sheet_name="02_FamilyRollup")
        lf = lf[lf.FAMEX_VERDICT == "UPDATE FAMILY RUN"]
        lp = pd.read_excel(LOAD_SRC, sheet_name="01_PartUnits")
        lp = lp[lp.VERDICT == "UPDATE RUN"]

        # a family action edits only the parts that actually reached the sheet
        key = ["OPERATION", "FAMILY", "PART_TYPE", "SEQ"]
        edited = load[load.ROUTE == "FAMILY"].groupby(key).PART_ID.size()

        load_acts = pd.concat([
            pd.DataFrame({
                "TARGET": "RUN", "RUN_TYPE": "HRS/LOAD", "LEVEL": "FAMILY",
                "OPERATION": lf.OPERATION, "FAMILY": lf.FAMILY,
                "PART_TYPE": lf.PART_TYPE,
                "PART_ID": np.where(
                    lf.PARTS_FLAGGED_UPDATE > 0,
                    "all parts except the "
                    + lf.PARTS_FLAGGED_UPDATE.astype(int).astype(str)
                    + " flagged individually", "all parts of this family"),
                "SEQ": lf.OPERATION_SEQUENCE,
                "N_PARTS": lf.set_index(
                    ["OPERATION", "FAMILY", "PART_TYPE", "OPERATION_SEQUENCE"]
                ).index.map(edited).fillna(0).astype(int),
                "N_WO": lf.FAMEX_WO,
                "RATIO": lf.FAMEX_RATIO_OUTLIERS_REMOVED.round(3),
                "CI95_LOW": lf.FAMEX_CI95_LOW.round(3),
                "CI95_HIGH": lf.FAMEX_CI95_HIGH.round(3),
                "HOURS_DELTA": lf.FAMEX_HOURS_DELTA,
                "CURRENT_VALUE": lf.FAMEX_RUN_CURRENT,
                "SUGGESTED_VALUE": tidy(lf.FAMEX_RUN_SUGGESTED),
                "CHANGE_PCT": ((lf.FAMEX_RUN_SUGGESTED / lf.FAMEX_RUN_CURRENT
                                - 1) * 100).round(1),
                "HOW_TO_APPLY": "set RUN (hours per load) to SUGGESTED_VALUE "
                                "for every part listed"}),
            pd.DataFrame({
                "TARGET": "RUN", "RUN_TYPE": "HRS/LOAD", "LEVEL": "PART",
                "OPERATION": lp.OPERATION, "FAMILY": lp.FAMILY,
                "PART_TYPE": lp.PART_TYPE, "PART_ID": lp.PART_ID,
                "SEQ": lp.OPERATION_SEQUENCE, "N_PARTS": 1, "N_WO": lp.WO,
                "RATIO": lp.RATIO_OUTLIERS_REMOVED.round(3),
                "CI95_LOW": lp.CI95_LOW.round(3),
                "CI95_HIGH": lp.CI95_HIGH.round(3),
                "HOURS_DELTA": lp.HOURS_DELTA,
                "CURRENT_VALUE": lp.RUN_CURRENT,
                "SUGGESTED_VALUE": tidy(lp.RUN_SUGGESTED),
                "CHANGE_PCT": ((lp.RUN_SUGGESTED / lp.RUN_CURRENT - 1)
                               * 100).round(1),
                "HOW_TO_APPLY": "set this part's RUN (hours per load) to "
                                "SUGGESTED_VALUE"})], ignore_index=True)
        acts = pd.concat([acts, load_acts], ignore_index=True)

    # PCS/HR for everything that came through the run analysis, '-' for setup:
    # SETUP_HRS is a duration whatever the run type of the operation, so naming
    # one there would suggest a distinction that does not exist.
    if "RUN_TYPE" not in acts.columns:
        acts["RUN_TYPE"] = np.nan
    acts["RUN_TYPE"] = np.where(
        acts.RUN_TYPE.notna(), acts.RUN_TYPE,
        np.where(acts.TARGET == "RUN", "PCS/HR", "-"))

    acts["DIRECTION"] = np.where(acts.RATIO > 1, "slow down the standard",
                                 "speed up the standard")
    # Family first, because the family is the unit that has to end up correct -
    # this puts every decision affecting one family together, run and setup, so
    # they are reviewed as the set they will be applied as. 00_Summary stays
    # operation-first for the "where is the work" question.
    # TARGET is kept because CURRENT_VALUE and SUGGESTED_VALUE mean different
    # things depending on it: a RUN row holds a rate in pieces per hour, a
    # SETUP_HRS row a duration in hours. Read without it, a setup row moving
    # 1.0 -> 3.1 looks like a run standard being tripled.
    # RUN_TYPE sits beside TARGET because a RUN row means two different things:
    # under PCS/HR it is a rate in pieces per hour and the correction divides,
    # under HRS/LOAD it is hours per load and the correction multiplies. Same
    # field, opposite arithmetic.
    lead = ["FAMILY", "PART_TYPE", "LEVEL", "PART_ID", "SEQ", "OPERATION",
            "TARGET", "RUN_TYPE"]
    acts = acts.sort_values(lead)
    acts = acts[lead + ["N_PARTS", "N_WO", "CURRENT_VALUE", "SUGGESTED_VALUE",
                        "CHANGE_PCT", "HOURS_DELTA", "DIRECTION",
                        "RATIO", "CI95_LOW", "CI95_HIGH", "HOW_TO_APPLY"]]
    acts = acts.rename(columns={**CI_NAMES, "SEQ": "SEQUENCE"})

    # ---- rows nobody should key in unread ---------------------------------
    for d in (db, setup, load):
        if len(d):
            d["REVIEW_FLAG"] = np.where(
                (d.RATIO < REVIEW_LO) | (d.RATIO > REVIEW_HI),
                f"NEEDS REVIEW - ratio outside {REVIEW_LO}-{REVIEW_HI}", "")

    # ---- the standard each row would overwrite, as it stands today --------
    # See the note at MIN_EFFECT near the top. Appended columns only.
    lookup = load_today()
    db = attach_today(db, "RM_CURRENT", "RM_RECOMMENDED", "RUN_MASTER_TODAY",
                      "Run", "PCS/HR", lookup)
    setup = attach_today(setup, "SETUP_HRS_CURRENT", "SETUP_HRS_RECOMMENDED",
                         "SETUP_HRS_MASTER_TODAY", "Setup", None, lookup)
    load = attach_today(load, "RUN_CURRENT", "RUN_RECOMMENDED",
                        "RUN_MASTER_TODAY", "Run", "HRS/LOAD", lookup)
    acts = family_values(acts, db, setup, load)
    acts = action_progress(acts, db, setup, load)

    summary = build_summary(acts, analysed)
    tokens = build_token_report()
    # every part that will be edited, whatever field it lands on - the distinct
    # part counts in the rollup have to come from here, not from summing
    # N_PARTS across actions, which counts a part once per operation
    all_edits = pd.concat(
        [d[["OPERATION", "FAMILY", "PART_TYPE", "PART_ID"]]
         for d in (db, setup, load) if len(d)], ignore_index=True)
    fam_rollup, fam_op_rollup = build_family_rollup(acts, all_edits)

    with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="00_Summary", index=False)
        acts.to_excel(xl, sheet_name="01_Actions", index=False)
        db.to_excel(xl, sheet_name="02_RunUpdateList", index=False)
        if len(setup):
            setup.to_excel(xl, sheet_name="03_SetupUpdateList", index=False)
        if len(load):
            load.to_excel(xl, sheet_name="04_LoadUpdateList", index=False)
        fam_rollup.to_excel(xl, sheet_name="05_FamilyTotals", index=False)
        fam_op_rollup.to_excel(xl, sheet_name="06_FamilyOperation", index=False)
        tokens.to_excel(xl, sheet_name="07_TokenBookings", index=False)

    print(summary.tail(4).to_string(index=False))
    pcs = ((acts.TARGET == "RUN") & (acts.RUN_TYPE == "PCS/HR")).sum()
    hrl = ((acts.TARGET == "RUN") & (acts.RUN_TYPE == "HRS/LOAD")).sum()
    print(f"\nactions reviewed      {len(acts)}"
          f"  ({pcs} run PCS/HR, {hrl} run HRS/LOAD, "
          f"{(acts.TARGET=='SETUP_HRS').sum()} setup)")
    print(f"operations analysed   {len(analysed)}"
          f"  ({summary[summary.OPERATION!='TOTAL'].query('ACTIONS>0').OPERATION.nunique()}"
          f" with an action)")
    print(f"token bookings        {tokens.WO_TOKEN.sum():,} work orders "
          f"across {len(tokens)} steps, "
          f"{tokens.STANDARD_CHARGED.sum():,.0f} h of standard charged"
          f"  ({tokens.BELOW_FLOOR_AFTER.sum()} steps left below the floor)")
    print(f"parts to change       {parts.groupby(['OPERATION','PART_ID','SEQ']).ngroups}")
    print(f"  via PART route      {(parts.ROUTE=='PART').sum()}")
    print(f"  via FAMILY route    {(parts.ROUTE=='FAMILY').sum()}")
    basis = db.RECOMMENDED_BASIS.value_counts()
    print("\nRM_RECOMMENDED basis")
    for k, v in basis.items():
        print(f"  {k:44} {v:>4}")
    print(f"\nrows in 02_RunUpdateList {len(db)}  (one per part)")
    miss = int((~db.ROWID_FOUND).sum())
    print(f"with a MASTER_OPS_ROWID {int(db.ROWID_FOUND.sum())}")
    print(f"WITHOUT one             {miss}" + ("  <- cannot be updated from this list" if miss else ""))
    if len(setup):
        print(f"\nrows in 03_SetupUpdateList {len(setup)}  "
              f"({setup.PART_ID.nunique()} parts, edits SETUP_HRS)")
    else:
        print(f"\nno {SETUP_SRC} found - setup rows not included")
    if len(load):
        print(f"rows in 04_LoadUpdateList  {len(load)}  "
              f"({load.PART_ID.nunique()} parts, edits RUN as hours per load)")
    else:
        print(f"no {LOAD_SRC} found - HRS/LOAD rows not included")
    print(f"\nfamilies with an action   {len(fam_rollup) - 1}"
          f"  ({fam_op_rollup.shape[0] - 1} family x operation cells)")
    flagged = sum(int((d.REVIEW_FLAG != "").sum())
                  for d in (db, setup, load) if len(d))
    print(f"flagged NEEDS REVIEW      {flagged}  "
          f"(ratio outside {REVIEW_LO}-{REVIEW_HI})")
    if lookup is None:
        print("\nno STANDARD_TODAY columns: this extract predates them "
              "(fts.sql before 2026-09-28)")
    else:
        print("\nSTANDARD WO vs STANDARD TODAY")
        for name, d in (("run PCS/HR", db), ("setup", setup),
                        ("run HRS/LOAD", load)):
            if not len(d):
                continue
            vc = d.STANDARD_CHANGE.value_counts()
            done = d.TODAY_STATUS.str.startswith("already done").sum()
            rev = (d.REVIEW_FLAG_TODAY != "").sum()
            print(f"  {name:13s} {len(d):5,} rows | "
                  + ", ".join(f"{k} {v:,}" for k, v in vc.items())
                  + f" | already done {done:,} | review from today {rev:,}")
        if "ACTION_STATUS" in acts.columns:
            st = acts.ACTION_STATUS
            print(f"  actions: {len(acts):,} | already done "
                  f"{(st == 'already done').sum():,} | partly done "
                  f"{st.str.startswith('partly').sum():,} | to key "
                  f"{(st == 'to key').sum():,}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
