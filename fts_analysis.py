"""
Master time validation, focused on one operation.

Reads fts.xlsx (output of fts.sql, already restricted to 4-digit operation
sequences, i.e. post-overhaul routings) and produces the review table:

  FAMILY | PART_ID | OPERATION_SEQUENCE | OPERATION | N_WORK_ORDERS | LT_TYPE
  | HOURS_MASTER | HOURS_WORKED | HOURS_DELTA
  | RATIO_HOURS_WORKED_TO_HOURS_MASTER
  | RUN_TYPE_MASTER | RUN_MASTER_CURRENT | RUN_MASTER_SUGGESTED

followed by the statistics the suggestion rests on.

SCOPE: LT_TYPE = 'Run', RUN_TYPE_MASTER = 'PCS/HR', current fiscal year.
  For PCS/HR the master is a rate: HOURS_MASTER = TOTAL_QTY / RUN_MASTER,
  so RUN_MASTER_SUGGESTED = RUN_MASTER_CURRENT / ratio.

Output: FTS_<OPERATION>_PARTS.xlsx
"""

import datetime as dt
import os
import re
import sys
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore", category=RuntimeWarning)

SRC = "fts.xlsx"
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
# the extract this one replaced, kept only so a rebuild can report what moved
PREV_CACHE = "fts_prev.parquet"
# WHERE THE REPORT IS KEPT. Anchored on __file__, unlike SRC and CACHE above:
# those are READ, so pointing the module at the wrong folder fails loudly and
# immediately. This one is WRITTEN, and a log that lands quietly in the wrong
# place is worse than no log at all.
SCOPE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "archive", "scope_changes.md")

# ---- tunables ---------------------------------------------------------------
FOCUS_OPERATION = sys.argv[1] if len(sys.argv) > 1 else "S FRESATURA"
# pass the operation as the first argument; None would run every operation
# Current fiscal year. THIS HAS A SECOND END: fts.sql hard-codes the same
# date in its WHERE clause, and the two must be changed together. The query
# used to derive its window from GETDATE(); that meant on 1 October the extract
# would silently start a year later than every script filtering it, and the
# analysis would run on a few weeks of data believing it had a year. Both are
# fixed now so the drift cannot happen, at the cost of a deliberate edit each
# year - in here, in the other four scripts, and in the query.
FY_START = "2025-10-01"
# 6 is a hard floor, not a preference: with fewer than 6 work orders a
# two-sided Wilcoxon cannot produce p < 0.05 even when every single one falls
# on the same side of the standard (n=5 bottoms out at p=0.0625).
MIN_WO = 6
ALPHA = 0.05          # BH-FDR level
MIN_EFFECT = 0.10     # |ratio-1| must exceed this to be worth changing
MIN_HOURS_IMPACT = 20 # |worked - master| in hours over the period
MAD_K = 3.0           # robust outlier cut on log-ratio (in MAD units)
# A work order booked at or below this records that the ticket was closed, not
# how long the job took. See drop_token_bookings.
#
# The value matches SKIP_CUT in fts_setup_view.py, but not for the reason
# given there: setup bookings have a clean gap at 0.02 and run bookings do not.
# Run has one spike at exactly 0.01 on a quantised tail of 0.02, 0.03,
# 0.04 ... and the median ratio climbs smoothly from near zero in the
# 0.01-0.02 band to about 1 above 0.20. Under-recording is a continuum, so any
# cut is a judgement. This one is the conservative end: below it a flat 0.02 h
# appears against hundreds of pieces, the same non-measurement as 0.01,
# while the 0.05-0.10 band sits well below 1 and could be genuinely fast work.
# The choice is close to inert - across 0.01 to 0.05 only a handful of family
# pools change testability, and between 0.01 and 0.02 exactly one does.
TOKEN_HOURS = 0.02

# One result per part per routing step. OPERATION_SEQUENCE is in the key
# because the same operation name recurs at several points of one routing while
# being a different job each time. WO_REV is not: a revision is bumped for any
# edit anywhere in the routing, so it says nothing about this operation's time.
# PART_TYPE is a property of the part, not a grouping decision - every PART_ID
# carries exactly one value, so listing it here cannot split a part. It is in
# the key so it survives into the workbook, where the family view needs it: a
# family holds both the semi-finished article and the finished one, and those
# are different physical jobs that must not be pooled.
UNIT = ["FAMILY", "PART_TYPE", "PART_ID", "OPERATION_SEQUENCE", "OPERATION",
        "LT_TYPE", "RUN_TYPE_MASTER"]


def slug(operation):
    """Filename-safe operation tag. Operation names carry dots, slashes and
    the odd mis-encoded character, so strip to alphanumerics."""
    return re.sub(r"[^A-Za-z0-9]+", "_", (operation or "ALL").strip()
                  ).strip("_").upper() or "OP"


def _log_scope_change(lines):
    """Append one dated block to archive/scope_changes.md. Never overwrites.

    THE REPORT USED TO BE PRINT-ONLY, which left the one output that would have
    caught the near-miss described below sitting in the terminal scrollback of
    whoever happened to run the rebuild. It cannot be recovered by re-running
    anything: load() is the only function with a rebuild branch, it fires once
    per new extract, and the same rebuild overwrites the very baseline it just
    compared against. A line missed was a line gone.

    Append-only, and it lives outside everything the analysis reads - so it can
    neither overwrite an earlier run's entry nor feed back into a result.
    """
    head = (
        "# Scope changes between extracts\n"
        "\n"
        "Appended by fts_analysis.compare_to_previous() on every cache"
        " rebuild -\n"
        "one block per new fts.xlsx, oldest first. Nothing here is read by"
        " the\n"
        "pipeline; it exists so a scope change can be found AFTER the run that"
        "\ncaused it.\n"
        "\n"
        "WHAT TO LOOK FOR: rows that LOST MASTER_OPS_ROWID while staying in"
        " the\n"
        "extract. That is a master routing rebuilt in the ERP, and it moves"
        " results\n"
        "without moving the row count - two extracts can agree on every row"
        " and\n"
        "disagree on which of them are testable.\n")
    os.makedirs(os.path.dirname(SCOPE_LOG), exist_ok=True)
    first = not os.path.exists(SCOPE_LOG)
    # THE EXTRACT'S OWN DATE, not just the run's. Two rebuilds of one workbook
    # are a re-run; two different workbooks are two generations, and only the
    # mtime tells them apart once both are in the file.
    src = (dt.datetime.fromtimestamp(os.path.getmtime(SRC))
             .strftime("%Y-%m-%d %H:%M") if os.path.exists(SRC) else "unknown")
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(SCOPE_LOG, "a", encoding="utf-8") as f:
        if first:
            f.write(head)
        f.write("\n## " + stamp + "  (fts.xlsx dated " + src + ")\n\n")
        # indented four spaces, so the block reads as the console output it is
        # and markdown leaves its alignment alone
        f.write("\n".join("    " + ln.rstrip() for ln in lines) + "\n")


def compare_to_previous(new, prev_path=PREV_CACHE):
    """Report what moved between the last extract and this one.

    This exists because of a specific near-miss. An extract was re-run to fix
    an arithmetic bug in the SQL, came back with an identical row count, and
    was assumed to be the same data with the bug corrected. It was not: the
    master routings of ten families had been rebuilt in the ERP in the
    meantime, thousands of rows lost their MASTER_OPS_ROWID, and their hours
    dropped out of scope. The suggestion list shrank by a quarter for a reason that had
    nothing to do with the fix, and nothing in the output said so.

    Row counts are not a check. Two extracts can agree on every row and
    disagree on which of them are testable. The columns that decide scope are
    the ones worth watching, and MASTER_OPS_ROWID is the volatile one - it is
    the only field here sourced from a table that engineering edits daily.

    Every line below is printed AND appended to archive/scope_changes.md, so
    the answer to "what changed when we pulled this extract?" survives the run
    that produced it. See _log_scope_change().
    """
    if not os.path.exists(prev_path):
        return
    old = pd.read_parquet(prev_path)
    if len(old) == 0:
        return
    k = ["WORK_ORDER", "PART_ID", "OPERATION_SEQUENCE", "OPERATION",
         "LT_TYPE", "RESOURCE_ID"]
    a = new.groupby(k, dropna=False).MASTER_OPS_ROWID.max().rename("n")
    b = old.groupby(k, dropna=False).MASTER_OPS_ROWID.max().rename("o")
    j = pd.concat([a, b], axis=1)

    # "Present" and "still has a rowid" are different questions, and only the
    # outer join makes them look alike: a key that left the extract altogether
    # arrives here with n = NaN, exactly like a key whose routing step was
    # deleted. Separating them matters both ways round. Rows that left because
    # the query window moved are bookkeeping and say nothing about scope, while
    # rows still in the extract that lost their rowid are the failure this
    # function was written for - and reading one as the other either raises a
    # false alarm or, worse, buries a real one inside it.
    #
    # It also fixes a crash. The FAMILY lookup below used to run against `new`
    # for every lost key; while the window was fixed those keys were always
    # still in `new`, so it worked. Tightening the fiscal-year filter dropped
    # 270 rows out of the extract for the first time and the lookup raised
    # KeyError on keys that no longer existed.
    here = j.index.isin(a.index)
    dropped = int((~here & j.o.notna()).sum())
    lost = int((here & j.n.isna() & j.o.notna()).sum())
    gained = int((j.n.notna() & j.o.isna()).sum())
    moved = int((j.n.notna() & j.o.notna() & (j.n != j.o)).sum())

    # say() instead of print(): the terminal still gets every line exactly as
    # before, and the same lines go to the file. One list, so the two can never
    # drift into saying different things about the same rebuild.
    out = []

    def say(line):
        print(line)
        out.append(line)

    say(f"extract: {len(new):,} rows (previous {len(old):,})")

    if dropped:
        f = old.set_index(k).loc[j.index[~here & j.o.notna()], "FAMILY"]
        top = f.value_counts().head(3)
        say(f"  {dropped:,} rows are no longer in the extract at all - "
            + ", ".join(f"{i} ({v:,})" for i, v in top.items()))
        say("  Expected after a query-window change; suspicious otherwise.")

    if lost or gained or moved:
        say(f"  MASTER_OPS_ROWID moved on {lost + gained + moved:,} rows "
            f"still in the extract - {lost:,} lost, {gained:,} gained, "
            f"{moved:,} re-issued")
        if lost:
            f = new.set_index(k).loc[j.index[here & j.n.isna() & j.o.notna()],
                                     "FAMILY"]
            top = f.value_counts().head(3)
            say("  scope SHRANK. Master routings changed for: "
                + ", ".join(f"{i} ({v:,})" for i, v in top.items()))
            say("  Results are not comparable with the previous run on those "
                "families.")

    # A QUIET REBUILD IS ALSO A FINDING, and it has to be written down as one.
    # Left out, the log would carry nothing for that generation, which reads
    # identically to the rebuild never having happened - and "no entry" is the
    # one thing this file must never be ambiguous about.
    if not dropped and not (lost or gained or moved):
        say("  no scope change - every key kept the MASTER_OPS_ROWID it had")

    _log_scope_change(out)


def load():
    # Rebuild when the extract is newer than the cache, rather than waiting for
    # somebody to delete the parquet. The manual step was itself a stale-data
    # hazard - drop in a new fts.xlsx, forget the delete, and every script
    # silently analyses the previous extract while looking perfectly normal.
    fresh = (os.path.exists(CACHE) and os.path.exists(SRC)
             and os.path.getmtime(CACHE) >= os.path.getmtime(SRC))
    if fresh:
        df = pd.read_parquet(CACHE)
    else:
        df = pd.read_excel(SRC, sheet_name=0)
        for c in df.columns:
            if df[c].dtype == object:
                df[c] = df[c].astype(str)
        # keep the outgoing cache so the rebuild can report what moved
        if os.path.exists(CACHE):
            os.replace(CACHE, PREV_CACHE)
        df.to_parquet(CACHE)
        compare_to_previous(df)

    for c in ["FAMILY", "PART_TYPE", "PART_ID", "PART_DESCRIPTION",
              "WORK_ORDER", "WO_REV", "OPERATION", "LT_TYPE",
              "RUN_TYPE_MASTER", "ANALYTIC_SECTION"]:
        df[c] = df[c].str.strip()
    df["ANALYTIC_SECTION"] = df["ANALYTIC_SECTION"].str.title()
    df["LAST_TRANSACTION_DATE"] = pd.to_datetime(df["LAST_TRANSACTION_DATE"],
                                                 errors="coerce")
    return df


def to_wo_level(df):
    """One row per unit x work order. Rare multi-resource splits are summed."""
    return (df.groupby(UNIT + ["WORK_ORDER"], dropna=False)
              .agg(PART_DESCRIPTION=("PART_DESCRIPTION", "first"),
                   ANALYTIC_SECTION=("ANALYTIC_SECTION", "first"),
                   WO_REV=("WO_REV", "first"),
                   RUN_MASTER=("RUN_MASTER", "max"),
                   LAST_DATE=("LAST_TRANSACTION_DATE", "max"),
                   TOTAL_QTY=("TOTAL_QTY", "sum"),
                   HOURS_WORKED=("HOURS_WORKED", "sum"),
                   HOURS_MASTER=("HOURS_MASTER", "sum"))
              .reset_index())


def drop_token_bookings(wo, label=""):
    """Remove work orders closed with a token booking; return both halves.

    A few hundred labour tickets carry a flat 0.01 h whatever the quantity -
    one piece or five hundred, the same hundredth of an hour. That is a ticket
    closed to move the order along, not a measurement, and there is no duration
    inside it to correct.

    They defeat the MAD filter instead of being caught by it. The filter trims
    outliers, and where the tokens are the majority they ARE the centre: on one
    unit almost every work order was a token, the median ratio settled near
    zero, and the filter then discarded the rows far from it - most of them
    further tokens - leaving a confident suggestion resting on three real jobs.

    Dropping them hands the decision to the MIN_WO floor, which is where it
    belongs: a unit either still has enough real work orders to be judged, or
    it leaves the analysis as insufficient data. Nothing is capped or imputed,
    because a booking that recorded no time says nothing about how long the job
    takes and any substitute value would be invented.

    fts_setup_view.py has done this since it was written (SKIP_CUT), where
    the same bookings mean a setup that was never performed.
    """
    tok = wo.HOURS_WORKED <= TOKEN_HOURS
    if tok.any():
        qty = f", {wo.loc[tok, 'TOTAL_QTY'].sum():,} pieces" \
            if "TOTAL_QTY" in wo.columns else ""
        print(f"token bookings dropped{label}: {tok.sum():,} work orders "
              f"({tok.mean():.1%}), {wo.loc[tok, 'HOURS_WORKED'].sum():.2f} h "
              f"booked{qty}")
    return wo[~tok].copy(), wo[tok].copy()


def test_group(g):
    """One unit -> test result + the master value the correction applies to."""
    # When RUN_MASTER changed during the period, the work orders run under the
    # master currently in force are the honest basis for correcting it - the
    # earlier ones describe a standard that no longer exists.
    cur = g.loc[g["LAST_DATE"].idxmax(), "RUN_MASTER"]
    on_cur = g["RUN_MASTER"] == cur
    changed = g["RUN_MASTER"].nunique() > 1
    enough_on_cur = int(on_cur.sum()) >= MIN_WO

    res = {
        "N_WO_ALL": len(g),
        "N_MASTERS": int(g["RUN_MASTER"].nunique()),
        "MASTER_CHANGED": bool(changed),
        "N_WO_CURRENT_MASTER": int(on_cur.sum()),
        "RUN_MASTER_CURRENT": float(cur),
        "WO_REVS": ",".join(sorted({str(v) for v in g["WO_REV"].dropna()})),
        "FIRST_DATE": g["LAST_DATE"].min(),
        "LAST_DATE": g["LAST_DATE"].max(),
    }

    if changed and enough_on_cur:
        g, res["BASIS"] = g[on_cur], "current master only"
    elif changed:
        # too few work orders under the new master to judge it; pooling in the
        # old-master orders would pull the estimate back toward a standard that
        # was already rejected, so this is reported but not acted on
        res["BASIS"] = "insufficient data on current master"
    else:
        res["BASIS"] = "single master"

    lr = g["LOG_RATIO"].to_numpy()
    n = len(lr)

    # robust centre and spread; the tail of work orders with a booked quantity
    # but almost no booked hours is mis-reporting, not speed
    med = float(np.median(lr))
    mad = float(np.median(np.abs(lr - med))) * 1.4826
    keep = np.abs(lr - med) <= MAD_K * mad if mad > 0 else np.ones(n, bool)
    gk = g[keep]

    hw, hm = g["HOURS_WORKED"].sum(), g["HOURS_MASTER"].sum()
    hw_k, hm_k = gk["HOURS_WORKED"].sum(), gk["HOURS_MASTER"].sum()

    res.update({
        "N_WORK_ORDERS": n,
        "N_WO_KEPT": int(keep.sum()),
        "TOTAL_QTY": int(g["TOTAL_QTY"].sum()),
        "HOURS_WORKED": round(hw, 2),
        "HOURS_MASTER": round(hm, 2),
        "HOURS_DELTA": round(hw - hm, 2),
        # headline ratio divides exactly the two hours columns next to it, so
        # the table is internally consistent when read by eye
        "RATIO_HOURS_WORKED_TO_HOURS_MASTER": hw / hm if hm > 0 else np.nan,
        # ...but the suggestion is driven by the outlier-resistant version, so
        # one badly booked work order cannot move a standard
        "RATIO_OUTLIERS_REMOVED": hw_k / hm_k if hm_k > 0 else np.nan,
        "HOURS_WORKED_KEPT": round(hw_k, 2),
        "HOURS_MASTER_KEPT": round(hm_k, 2),
        "RATIO_MEDIAN_PER_WO": float(np.exp(med)),
        "LOG_SD": float(np.std(lr, ddof=1)) if n > 1 else np.nan,
        "P_VALUE_WILCOXON": np.nan,
        "P_VALUE_TTEST": np.nan,
        "CI95_LOW": np.nan,
        "CI95_HIGH": np.nan,
    })

    if n >= MIN_WO and np.any(lr != 0):
        # primary: distribution-free. H0 = median log-ratio is 0.
        try:
            res["P_VALUE_WILCOXON"] = float(
                stats.wilcoxon(lr, zero_method="wilcox")[1])
        except ValueError:
            pass
        # secondary: parametric, and the source of the confidence interval
        t = stats.ttest_1samp(lr, 0.0)
        res["P_VALUE_TTEST"] = float(t.pvalue)
        lo, hi = t.confidence_interval(1 - ALPHA)
        res["CI95_LOW"], res["CI95_HIGH"] = float(np.exp(lo)), float(np.exp(hi))

    return pd.Series(res)


def bh_fdr(p):
    """Benjamini-Hochberg adjusted p-values."""
    p = np.asarray(p, float)
    ok = np.isfinite(p)
    q = np.full_like(p, np.nan)
    if ok.sum() == 0:
        return q
    pv = p[ok]
    order = np.argsort(pv)
    m = len(pv)
    adj = pv[order] * m / np.arange(1, m + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1].clip(0, 1)
    out = np.empty(m)
    out[order] = adj
    q[ok] = out
    return q


def recommend(r):
    """Plain-language verdict per row."""
    if r.N_WORK_ORDERS < MIN_WO:
        return f"insufficient data (n<{MIN_WO})"
    if r.BASIS == "insufficient data on current master":
        return "watch - master changed recently, too few work orders on it"
    if not r.IS_SIGNIFICANT:
        return "no change - not statistically different"
    if abs(r.RATIO_OUTLIERS_REMOVED - 1) < MIN_EFFECT:
        return f"no change - deviation below {MIN_EFFECT:.0%}"
    if abs(r.HOURS_DELTA) < MIN_HOURS_IMPACT:
        return f"no change - impact below {MIN_HOURS_IMPACT}h"
    return "UPDATE RUN_MASTER"


def main():
    df = load()
    scope = ((df.LT_TYPE == "Run")
             & (df.RUN_TYPE_MASTER == "PCS/HR")
             # Rework carries no standard by design - thousands of worked
             # hours against HOURS_MASTER = 0 - so the gate above already
             # removes it. Naming it explicitly anyway: the exclusion is a
             # decision, not a side effect of a zero, and a rework step that
             # ever acquired a standard would otherwise walk into the analysis
             # unannounced. Matches the same filter in fts_setup_view.py and
             # fts_load_view.py.
             & ~df.OPERATION.str.upper().str.startswith("S RIL")
             & (df.LAST_TRANSACTION_DATE >= pd.Timestamp(FY_START))
             & (df.RUN_MASTER > 0)
             & (df.HOURS_MASTER > 0)
             & (df.HOURS_WORKED > 0)
             # The step must still exist in the current master routing. A blank
             # MASTER_OPS_ROWID means either a later revision deleted the step,
             # or the slot now holds a different operation - the routing edits
             # the line in place and keeps its ROWID. Either way the work order
             # measures a process that is no longer in force. Must match the
             # same filter in fts_family_view.py or the two disagree on which
             # parts belong to a family.
             # CREW-EXEMPT ROWS WITH A PLANNED CREW > 1 ARE NOT COMPARABLE.
             # fts.sql divides HOURS_WORKED by USER_5 unless SR.USER_7 is
             # LATHING/MILLING/TBP CELL or the employee's department is MEC. On
             # an exempt row the hours are MAN-HOURS while the standard is
             # ELAPSED, so the ratio is inflated by the crew size and measures
             # nothing. Where the crew is 1 the division is a no-op and the
             # exemption is harmless, which is why the filter needs both terms.
             #
             # ADDED 2026-08-26, once CREW_EXEMPT made this visible at all. It
             # is 3 rows in the whole extract, all Press & cutting, all via the
             # MEC arm - but two of them carry setup ratios of 11.7 and 8.3
             # against pool medians near 1.0, and one pushes its family to 3.438
             # where only the n<6 floor stops it becoming a false action.
             # Exclude, never correct: there is no crew size to divide by that
             # the extract can vouch for.
             & ~((df.CREW_EXEMPT == 1) & (df.USER_5.fillna(1) > 1))
             & df.MASTER_OPS_ROWID.notna())
    if FOCUS_OPERATION:
        scope &= df.OPERATION == FOCUS_OPERATION
    d = df[scope].copy()

    wo = to_wo_level(d)
    wo, tokens = drop_token_bookings(wo)

    # An operation can lose every work order it had - S CONTR. ANTIOX has four
    # in scope and all four are token bookings. Without this, test_group never
    # runs, the result frame comes back with no P_VALUE_TTEST column, and the
    # script dies on a KeyError several lines later.
    #
    # Crashing is not the real damage. The previous workbook stays on disk, the
    # update list reads it, and a stale result is reported as current with
    # nothing to indicate it. So the workbook is written either way: an empty
    # one that says plainly there was nothing to test beats a fresh-looking one
    # from the last extract.
    if wo.empty:
        path = os.path.join(OUTPUTS,
                            f"FTS_{slug(FOCUS_OPERATION)}_PARTS.xlsx")
        os.makedirs(OUTPUTS, exist_ok=True)
        summary = pd.DataFrame([
            ("Operation", FOCUS_OPERATION or "(all)", np.nan),
            ("Fiscal year from", FY_START, np.nan),
            ("Work orders in scope", d.WORK_ORDER.nunique(),
             d.HOURS_WORKED.sum()),
            (f"  ...token bookings dropped (<= {TOKEN_HOURS} h)", len(tokens),
             tokens.HOURS_WORKED.sum()),
            ("Units (part x sequence)", 0, 0.0),
            ("NOTHING TESTABLE", "every work order was a token booking",
             np.nan),
        ], columns=["METRIC", "VALUE", "HOURS"])
        empty = pd.DataFrame(columns=UNIT + ["N_WORK_ORDERS", "RECOMMENDATION"])
        with pd.ExcelWriter(path, engine="openpyxl") as xl:
            summary.to_excel(xl, sheet_name="00_Summary", index=False)
            for sheet in ["01_Table", "02_Table_Tested", "03_Actions",
                          "04_Tested_WithEvidence", "05_AllUnits_WithEvidence",
                          "06_ActionDetail"]:
                empty.to_excel(xl, sheet_name=sheet, index=False)
        print(summary.to_string(index=False))
        print(f"\nwrote {path} - nothing testable, no units")
        return

    wo["LOG_RATIO"] = np.log(wo.HOURS_WORKED / wo.HOURS_MASTER)

    res = (wo.groupby(UNIT, dropna=False)
             .apply(test_group, include_groups=False)
             .reset_index())
    # Multiplicity control runs on the t-test p-value, not the Wilcoxon one.
    # Wilcoxon is discrete: at n=7 the smallest p it can ever return is 0.0156
    # even when every work order is far off the standard, so after FDR it
    # blocks real findings for reasons of test granularity rather than
    # evidence. The t-test p is continuous and has no such floor.
    res["Q_VALUE_FDR"] = bh_fdr(res["P_VALUE_TTEST"])
    # Wilcoxon still has to agree at the raw level - that is the guard against
    # a skewed sample driving the mean while the median says otherwise.
    res["IS_SIGNIFICANT"] = ((res["Q_VALUE_FDR"] < ALPHA)
                             & (res["P_VALUE_WILCOXON"] < ALPHA)
                             & ((res["CI95_LOW"] > 1) | (res["CI95_HIGH"] < 1)))
    res["DIRECTION"] = np.where(
        res["RATIO_OUTLIERS_REMOVED"] > 1,
        "master too tight (job takes longer)",
        "master too loose (job is faster)")

    # PCS/HR RUN_MASTER is a rate, so hours = qty / RUN. Working the correction
    # factor back onto the parameter means dividing, not multiplying.
    res["RUN_MASTER_SUGGESTED"] = (
        res.RUN_MASTER_CURRENT / res.RATIO_OUTLIERS_REMOVED).round(2)
    res["RECOMMENDATION"] = res.apply(recommend, axis=1)
    res["IS_MATERIAL"] = res.RECOMMENDATION == "UPDATE RUN_MASTER"
    # keep the arithmetic visible on every tested row - the recommendation says
    # whether to act on it - but never emit a number for an untested row
    res["RUN_MASTER_SUGGESTED"] = res.RUN_MASTER_SUGGESTED.where(
        res.N_WORK_ORDERS >= MIN_WO)

    res = res.merge(wo.groupby(UNIT, dropna=False)
                      .agg(PART_DESCRIPTION=("PART_DESCRIPTION", "first"),
                           ANALYTIC_SECTION=("ANALYTIC_SECTION", "first"))
                      .reset_index(), on=UNIT, how="left")

    # requested column order first, supporting evidence after
    cols = ["FAMILY", "PART_TYPE", "PART_ID", "PART_DESCRIPTION",
            "OPERATION_SEQUENCE",
            "OPERATION", "N_WORK_ORDERS", "LT_TYPE", "HOURS_MASTER",
            "HOURS_WORKED", "HOURS_DELTA",
            "RATIO_HOURS_WORKED_TO_HOURS_MASTER", "RUN_TYPE_MASTER",
            "RUN_MASTER_CURRENT", "RUN_MASTER_SUGGESTED", "RECOMMENDATION",
            # --- evidence ---
            "RATIO_OUTLIERS_REMOVED", "HOURS_WORKED_KEPT", "HOURS_MASTER_KEPT",
            "ANALYTIC_SECTION", "TOTAL_QTY", "N_WO_KEPT", "N_WO_ALL",
            "N_MASTERS", "MASTER_CHANGED", "N_WO_CURRENT_MASTER", "BASIS",
            "WO_REVS", "FIRST_DATE", "LAST_DATE", "RATIO_MEDIAN_PER_WO", "CI95_LOW", "CI95_HIGH", "LOG_SD",
            "P_VALUE_WILCOXON", "P_VALUE_TTEST", "Q_VALUE_FDR",
            "IS_SIGNIFICANT", "IS_MATERIAL", "DIRECTION"]
    out = res[cols].sort_values(["IS_MATERIAL", "HOURS_DELTA"],
                                ascending=[False, False])

    tested = out[out.N_WORK_ORDERS >= MIN_WO]
    actions = out[out.IS_MATERIAL]

    # the requested table, nothing else
    plain = out[["FAMILY", "PART_TYPE", "PART_ID", "OPERATION_SEQUENCE",
                 "OPERATION",
                 "N_WORK_ORDERS", "LT_TYPE", "HOURS_MASTER", "HOURS_WORKED",
                 "HOURS_DELTA", "RATIO_HOURS_WORKED_TO_HOURS_MASTER",
                 "RATIO_OUTLIERS_REMOVED", "RUN_TYPE_MASTER",
                 "RUN_MASTER_CURRENT",
                 "RUN_MASTER_SUGGESTED", "RECOMMENDATION"]].rename(
        columns={"OPERATION_SEQUENCE": "SEQ", "N_WORK_ORDERS": "N_WO"})

    detail = (wo.merge(res.loc[res.IS_MATERIAL, UNIT], on=UNIT)
                .assign(RATIO=lambda x: x.HOURS_WORKED / x.HOURS_MASTER)
                .sort_values(UNIT + ["LAST_DATE"])
                [UNIT + ["WORK_ORDER", "WO_REV", "LAST_DATE", "RUN_MASTER",
                         "TOTAL_QTY", "HOURS_MASTER", "HOURS_WORKED", "RATIO"]])

    summary = pd.DataFrame([
        ("Operation", FOCUS_OPERATION or "(all)", np.nan),
        ("Fiscal year from", FY_START, np.nan),
        ("Work orders in scope", d.WORK_ORDER.nunique(), d.HOURS_WORKED.sum()),
        (f"  ...token bookings dropped (<= {TOKEN_HOURS} h)", len(tokens),
         tokens.HOURS_WORKED.sum()),
        ("Units (part x sequence)", len(out), out.HOURS_WORKED.sum()),
        (f"  ...testable (n >= {MIN_WO})", len(tested),
         tested.HOURS_WORKED.sum()),
        ("  ...statistically different", int(out.IS_SIGNIFICANT.sum()),
         out.loc[out.IS_SIGNIFICANT, "HOURS_WORKED"].sum()),
        (f"  ...material (>={MIN_EFFECT:.0%} and >={MIN_HOURS_IMPACT}h)",
         len(actions), actions.HOURS_WORKED.sum()),
        ("Pooled ratio, all units", round(
            out.HOURS_WORKED.sum() / out.HOURS_MASTER.sum(), 4), np.nan),
        ("Net hours delta, actions", np.nan, actions.HOURS_DELTA.sum()),
    ], columns=["METRIC", "VALUE", "HOURS"])

    os.makedirs(OUTPUTS, exist_ok=True)
    path = os.path.join(OUTPUTS,
                        f"FTS_{slug(FOCUS_OPERATION)}_PARTS.xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="00_Summary", index=False)
        plain.to_excel(xl, sheet_name="01_Table", index=False)
        plain[plain.N_WO >= MIN_WO].to_excel(
            xl, sheet_name="02_Table_Tested", index=False)
        actions.to_excel(xl, sheet_name="03_Actions", index=False)
        tested.to_excel(xl, sheet_name="04_Tested_WithEvidence", index=False)
        out.to_excel(xl, sheet_name="05_AllUnits_WithEvidence", index=False)
        detail.to_excel(xl, sheet_name="06_ActionDetail", index=False)

    print(summary.to_string(index=False))
    print(f"\nwrote {path}")
    print(out.RECOMMENDATION.value_counts().to_string())


if __name__ == "__main__":
    main()
