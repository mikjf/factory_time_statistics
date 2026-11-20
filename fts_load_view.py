"""
Run times for HRS/LOAD operations - part and family level, all operations at once.

HRS/LOAD is not PCS/HR with a different filter. The master is not a rate but a
STEP FUNCTION of quantity:

    HOURS_MASTER = CEILING(TOTAL_QTY / LOAD_SIZE_QTY) * RUN

RUN is hours per load and LOAD_SIZE_QTY is how many pieces fit in one. A job of
1048 pieces against a load size of 500 needs three machine cycles and is charged
3 x RUN, whatever the pieces weigh or how full the last basket is.

Three consequences, and each one changes the answer.

1. THE CORRECTION MULTIPLIES. RUN is a duration per load, like SETUP_HRS, so
   RUN_new = RUN_old * ratio. RUN_MASTER for PCS/HR is a rate and divides.
   Getting this backwards yields plausible numbers wrong in both directions.

2. LOAD_SIZE_QTY IS TAKEN AS CORRECT. It is a physical fact about the machine
   and the part - how many pieces fit in a basket - and labour tickets contain
   no evidence about it. They measure elapsed time. Correcting it from timing
   data would be inventing a number rather than measuring one, so RUN is the
   only parameter this analysis moves.

3. ONLY SINGLE-LOAD WORK ORDERS ARE TESTED. The model assumes the second basket
   costs the same as the first, and it does not: over the fiscal year,
   single-load jobs run over their standard while multi-load jobs run well
   under theirs. Fitting HOURS_WORKED = a + b * loads puts the marginal load at
   a different share of RUN on every operation, and one operation has too few
   work orders to support a fit at all. (04_MultiLoadGap carries the fit;
   fts_checks.py reads it from there rather than refitting, because refitting
   it by eye gives a different answer.)

   There is no single multiplier, so a single RUN cannot fit both regimes: fit
   it to everything pooled and the single-load population, most of the steps,
   moves to a standard set by the rest.

   Normalising both sides by the load count does NOT help - the ratio is already
   dimensionless, so dividing numerator and denominator by the same number
   cancels and every test statistic comes out identical. It is a shape problem,
   not a scale problem.

   The multi-load gap is therefore reported separately (04_MultiLoadGap): the
   capacity reserved and not used is a routing question rather than a
   standards one, and it goes to different people.

   The figures behind this block are kept in the private findings notes and
   recomputed by fts_checks.py on every run.

THE SINGLE-LOAD TEST IS `HOURS_MASTER == RUN_MASTER`, not TOTAL_QTY <=
LOAD_SIZE_QTY. The two cannot disagree. The query computes HOURS_MASTER as
CEILING(TOTAL_QTY / LOAD_SIZE_QTY) * RUN, so HOURS_MASTER == RUN is true exactly
when CEILING(qty / load) == 1, which is true exactly when 0 < qty <= load - an
identity, given the RUN > 0 and HOURS_MASTER > 0 the scope already requires.
Every extract so far confirms it, with zero disagreement either way.

An earlier version of this paragraph claimed they disagreed on dozens of steps,
and README.md carried the same claim with different numbers. Neither is
reproducible under any query in this repo. Removed rather than refreshed, and
fts_checks.py now re-tests the identity on every run so a future change to the
HOURS_MASTER expression shows up as a non-zero count instead of as stale prose.

The form is kept as HOURS_MASTER == RUN_MASTER because it stays correct if the
master hours ever stop being derived from the quantity.

Output: FTS_RUN_HRS_LOAD.xlsx
"""

import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore", category=RuntimeWarning)

CACHE = "fts.parquet"
OUT = "FTS_RUN_HRS_LOAD.xlsx"

# Must match the hard-coded date in fts.sql - see the note in
# fts_analysis.py. Change every copy together or the extract and the filter
# describe different years.
FY_START = "2025-10-01"
MIN_WO = 6            # a two-sided Wilcoxon cannot reach p<0.05 below this
ALPHA = 0.05
MIN_EFFECT = 0.10
MIN_HOURS_IMPACT = 20
MAD_K = 3.0
# A booking at or below this closed the ticket without recording a duration.
# Same cut as TOKEN_HOURS in fts_analysis.py and SKIP_CUT in the setup view.
TOKEN_HOURS = 0.02
# Flagged on the RATIO, not the percentage change: the change is asymmetric
# (a cut can never read worse than -100%) so it catches only increases.
REVIEW_LO, REVIEW_HI = 0.25, 4.0

# PART_TYPE splits the family pool: a family holds both the semi-finished
# article and the finished one, and at a shared step those are different jobs.
UNIT = ["FAMILY", "PART_TYPE", "PART_ID", "OPERATION_SEQUENCE", "OPERATION"]
FAM = ["FAMILY", "PART_TYPE", "OPERATION_SEQUENCE", "OPERATION"]


def load():
    """Work-order level, split into the single-load set and the rest."""
    df = pd.read_parquet(CACHE)
    for c in ["FAMILY", "PART_TYPE", "PART_ID", "WORK_ORDER", "OPERATION",
              "LT_TYPE", "RUN_TYPE_MASTER", "ANALYTIC_SECTION"]:
        df[c] = df[c].str.strip()
    df["ANALYTIC_SECTION"] = df.ANALYTIC_SECTION.str.title()
    df["LAST_TRANSACTION_DATE"] = pd.to_datetime(df["LAST_TRANSACTION_DATE"],
                                                 errors="coerce")
    scope = ((df.LT_TYPE == "Run")
             & (df.RUN_TYPE_MASTER == "HRS/LOAD")
             # rework carries no standard by design and is excluded everywhere
             & ~df.OPERATION.str.upper().str.startswith("S RIL")
             & (df.LAST_TRANSACTION_DATE >= pd.Timestamp(FY_START))
             & (df.RUN_MASTER > 0)
             & (df.HOURS_MASTER > 0)
             & (df.HOURS_WORKED > 0)
             # the step must still exist in the current master routing
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

    wo = (df[scope]
          .groupby(UNIT + ["WORK_ORDER"], dropna=False)
          .agg(ANALYTIC_SECTION=("ANALYTIC_SECTION", "first"),
               MASTER_OPS_ROWID=("MASTER_OPS_ROWID", "first"),
               RUN_MASTER=("RUN_MASTER", "max"),
               LOAD_SIZE=("LOAD_SIZE_QTY_MASTER", "max"),
               LAST_DATE=("LAST_TRANSACTION_DATE", "max"),
               TOTAL_QTY=("TOTAL_QTY", "sum"),
               HOURS_WORKED=("HOURS_WORKED", "sum"),
               HOURS_MASTER=("HOURS_MASTER", "sum"))
          .reset_index())

    # Work orders closed with a token booking rather than a measured duration:
    # a flat 0.01 h whatever the quantity. There is no time in them to correct,
    # and where they are the majority of a pool the MAD filter centres on them
    # and trims the real work orders away instead. Dropped, so the MIN_WO floor
    # decides. Matches TOKEN_HOURS in fts_analysis.py and SKIP_CUT in
    # fts_setup_view.py.
    tok = wo.HOURS_WORKED <= TOKEN_HOURS
    if tok.any():
        print(f"token bookings dropped: {tok.sum():,} work orders "
              f"({tok.mean():.1%}), {wo.loc[tok, 'HOURS_WORKED'].sum():.2f} h "
              f"booked, {wo.loc[tok, 'TOTAL_QTY'].sum():,} pieces")
    wo = wo[~tok].copy()

    # Loads the master actually charged for, which is the quantity that matters.
    # Deriving it from HOURS_MASTER rather than from the quantity is deliberate:
    # see the module docstring on why the two cannot disagree.
    wo["LOADS_CHARGED"] = (wo.HOURS_MASTER / wo.RUN_MASTER).round(2)
    wo["SINGLE_LOAD"] = np.isclose(wo.HOURS_MASTER, wo.RUN_MASTER, atol=0.001)
    wo["LOG_RATIO"] = np.log(wo.HOURS_WORKED / wo.HOURS_MASTER)

    # Cut each part+sequence to its own latest RUN, so no superseded standard
    # leaks into a pooled figure.
    key = ["PART_ID", "OPERATION_SEQUENCE", "OPERATION"]
    latest = (wo.sort_values("LAST_DATE").groupby(key).RUN_MASTER
                .last().rename("_CUR"))
    wo = wo.join(latest, on=key)
    wo = wo[wo.RUN_MASTER == wo._CUR].drop(columns="_CUR")
    return wo[wo.SINGLE_LOAD].copy(), wo[~wo.SINGLE_LOAD].copy()


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


def pooled(g, prefix=""):
    """Descriptives and test for a pool of single-load work orders."""
    p = f"{prefix}_" if prefix else ""
    # PART_ID is a grouping key at part level, so groupby(...).apply drops it -
    # a part-level pool is one part by construction
    n_parts = g.PART_ID.nunique() if "PART_ID" in g.columns else 1
    res = {f"{p}PARTS": n_parts, f"{p}WO": len(g)}
    blank = ["HOURS_MASTER", "HOURS_WORKED", "HOURS_DELTA", "RATIO",
             "RATIO_OUTLIERS_REMOVED", "RUN_CURRENT", "RUN_SUGGESTED",
             "N_DISTINCT_RUN", "CI95_LOW", "CI95_HIGH", "P_WILCOXON",
             "P_RAW_TTEST"]
    if len(g) == 0:
        res.update({f"{p}{k}": np.nan for k in blank})
        return res

    lr = g.LOG_RATIO.to_numpy()
    med = float(np.median(lr))
    mad = float(np.median(np.abs(lr - med))) * 1.4826
    keep = np.abs(lr - med) <= MAD_K * mad if mad > 0 else np.ones(len(lr), bool)
    gk = g[keep]

    hm, hw = g.HOURS_MASTER.sum(), g.HOURS_WORKED.sum()
    hmk, hwk = gk.HOURS_MASTER.sum(), gk.HOURS_WORKED.sum()
    ratio = hwk / hmk if hmk > 0 else np.nan
    # every row here is one load, so the effective current RUN is just the
    # hours-weighted average of the master values in the pool
    run_cur = hm / len(g)

    res.update({
        f"{p}HOURS_MASTER": round(hm, 2),
        f"{p}HOURS_WORKED": round(hw, 2),
        f"{p}HOURS_DELTA": round(hw - hm, 2),
        f"{p}RATIO": hw / hm if hm > 0 else np.nan,
        f"{p}RATIO_OUTLIERS_REMOVED": ratio,
        # left at 3 on purpose: this is hm / len(g), an AVERAGE over the pool,
        # and on a pool running several different RUN values it is not an ERP
        # value at all. N_DISTINCT_RUN below says when that is the case, and a
        # third decimal is the visible sign of it.
        f"{p}RUN_CURRENT": round(run_cur, 3),
        # RUN is hours per load - a DURATION - so the correction MULTIPLIES
        #
        # TWO DECIMALS BECAUSE THE ERP FIELD HOLDS TWO, since 2026-09-02. This
        # was 3, and 154 of the 198 rows of 04_LoadUpdateList carried a third
        # decimal - a figure nobody can key. Whoever entered them rounded by
        # hand, so the rounding happened anyway, 154 times and invisibly,
        # instead of once and in the open here.
        #
        # It costs nothing measurable. Checked over all 198 rows before the
        # change: largest shift 0.005 h, largest relative shift 0.15%, and
        # nothing collapses to zero - the smallest suggested value is 1.953.
        # This is hours per LOAD, not fractions of a minute per piece.
        #
        # Flat 2, matching PCS/HR run (fts_analysis, RUN_MASTER_SUGGESTED).
        # Setup is the odd one out - it coarsens to 1 decimal above 10 hours -
        # and that is deliberately NOT copied here. Two of the three analyses
        # now agree, and the rule would touch 4 rows.
        f"{p}RUN_SUGGESTED": round(run_cur * ratio, 2) if ratio == ratio else np.nan,
        f"{p}N_DISTINCT_RUN": int(g.RUN_MASTER.nunique()),
        f"{p}CI95_LOW": np.nan, f"{p}CI95_HIGH": np.nan,
        f"{p}P_WILCOXON": np.nan, f"{p}P_RAW_TTEST": np.nan,
    })

    if len(lr) >= MIN_WO and np.any(lr != 0):
        try:
            res[f"{p}P_WILCOXON"] = float(
                stats.wilcoxon(lr, zero_method="wilcox")[1])
        except ValueError:
            pass
        t = stats.ttest_1samp(lr, 0.0)
        res[f"{p}P_RAW_TTEST"] = float(t.pvalue)
        lo, hi = t.confidence_interval(1 - ALPHA)
        res[f"{p}CI95_LOW"], res[f"{p}CI95_HIGH"] = float(np.exp(lo)), float(np.exp(hi))
    return res


def verdict(n, sig, ratio, delta, label, floor):
    if n < floor:
        return f"insufficient data (n<{floor})"
    if not sig:
        return "no change - not statistically different"
    if abs(ratio - 1) < MIN_EFFECT:
        return f"no change - deviation below {MIN_EFFECT:.0%}"
    if abs(delta) < MIN_HOURS_IMPACT:
        return f"no change - impact below {MIN_HOURS_IMPACT}h"
    return label


def main():
    one, multi = load()
    print(f"HRS/LOAD run work orders in scope : {len(one) + len(multi):,}")
    print(f"  single load (tested)            : {len(one):,}")
    print(f"  multi load  (reported only)     : {len(multi):,}")

    # ---- part level -------------------------------------------------------
    part = (one.groupby(UNIT, dropna=False)
               .apply(lambda g: pd.Series({
                   **pooled(g),
                   "SECTION": g.ANALYTIC_SECTION.iat[0],
                   "MASTER_OPS_ROWID": g.MASTER_OPS_ROWID.iat[0],
                   "LOAD_SIZE": g.LOAD_SIZE.max(),
                   "MEDIAN_QTY": g.TOTAL_QTY.median(),
                   "FIRST_DATE": g.LAST_DATE.min(),
                   "LAST_DATE": g.LAST_DATE.max()}), include_groups=False)
               .reset_index())
    part["Q_VALUE_FDR"] = bh_fdr(part.P_RAW_TTEST)
    part["IS_SIGNIFICANT"] = ((part.Q_VALUE_FDR < ALPHA)
                              & (part.P_WILCOXON < ALPHA)
                              & ((part.CI95_LOW > 1) | (part.CI95_HIGH < 1)))
    part["VERDICT"] = [verdict(r.WO, r.IS_SIGNIFICANT, r.RATIO_OUTLIERS_REMOVED,
                               r.HOURS_DELTA, "UPDATE RUN", MIN_WO)
                       for r in part.itertuples()]
    part["RUN_SUGGESTED"] = part.RUN_SUGGESTED.where(part.VERDICT == "UPDATE RUN")
    flagged = set(part.loc[part.VERDICT == "UPDATE RUN", "PART_ID"])
    print(f"\nparts flagged UPDATE and stripped from the family pools: {len(flagged)}")

    # ---- family level, with the flagged parts removed ----------------------
    rows = []
    for k, g in one.groupby(FAM, dropna=False):
        ex = g[~g.PART_ID.isin(flagged)]
        rows.append({**dict(zip(FAM, k)),
                     "SECTION": g.ANALYTIC_SECTION.iat[0],
                     "PARTS_FLAGGED_UPDATE": g.loc[
                         g.PART_ID.isin(flagged), "PART_ID"].nunique(),
                     **pooled(g, "FAM"), **pooled(ex, "FAMEX")})
    fam = pd.DataFrame(rows)
    fam["FAMEX_Q_VALUE"] = bh_fdr(fam.FAMEX_P_RAW_TTEST)
    fam["FAMEX_SIGNIFICANT"] = ((fam.FAMEX_Q_VALUE < ALPHA)
                                & (fam.FAMEX_P_WILCOXON < ALPHA)
                                & ((fam.FAMEX_CI95_LOW > 1)
                                   | (fam.FAMEX_CI95_HIGH < 1)))
    fam["FAMEX_VERDICT"] = [
        verdict(r.FAMEX_WO, r.FAMEX_SIGNIFICANT, r.FAMEX_RATIO_OUTLIERS_REMOVED,
                r.FAMEX_HOURS_DELTA, "UPDATE FAMILY RUN", MIN_WO)
        for r in fam.itertuples()]
    fam["FAMEX_RUN_SUGGESTED"] = fam.FAMEX_RUN_SUGGESTED.where(
        fam.FAMEX_VERDICT == "UPDATE FAMILY RUN")

    # ---- the list to key in ----------------------------------------------
    # part route first; family route covers every remaining part of an acted-on
    # family, each corrected by the family factor against its own stored RUN
    pr = part[part.VERDICT == "UPDATE RUN"].assign(ROUTE="PART")
    lst = [pr[UNIT + ["ROUTE", "MASTER_OPS_ROWID", "RUN_CURRENT",
                      "RUN_SUGGESTED", "RATIO_OUTLIERS_REMOVED", "WO",
                      "LAST_DATE"]]
           .rename(columns={"RUN_SUGGESTED": "RUN_RECOMMENDED",
                            "RATIO_OUTLIERS_REMOVED": "RATIO",
                            "WO": "N_WO_EVIDENCE"})]
    fa = fam[fam.FAMEX_VERDICT == "UPDATE FAMILY RUN"]
    for _, f in fa.iterrows():
        m = part[(part.FAMILY == f.FAMILY) & (part.PART_TYPE == f.PART_TYPE)
                 & (part.OPERATION_SEQUENCE == f.OPERATION_SEQUENCE)
                 & (part.OPERATION == f.OPERATION)
                 & ~part.PART_ID.isin(flagged)]
        if not len(m):
            continue
        lst.append(pd.DataFrame({
            **{c: m[c] for c in UNIT}, "ROUTE": "FAMILY",
            "MASTER_OPS_ROWID": m.MASTER_OPS_ROWID,
            "RUN_CURRENT": m.RUN_CURRENT,
            # 2 decimals for the same reason as RUN_SUGGESTED above - this
            # is the other end that writes into 04_LoadUpdateList, and the
            # family route is where most of the rows come from.
            "RUN_RECOMMENDED": (m.RUN_CURRENT
                                * f.FAMEX_RATIO_OUTLIERS_REMOVED).round(2),
            "RATIO": f.FAMEX_RATIO_OUTLIERS_REMOVED,
            "N_WO_EVIDENCE": f.FAMEX_WO, "LAST_DATE": m.LAST_DATE}))
    db = pd.concat(lst, ignore_index=True) if lst else pd.DataFrame()
    if len(db):
        db["CHANGE_PCT"] = ((db.RUN_RECOMMENDED / db.RUN_CURRENT - 1) * 100).round(1)
        db["TARGET_COLUMN"] = "RUN"
        db["RUN_TYPE_MASTER"] = "HRS/LOAD"
        db["REVIEW_FLAG"] = np.where(
            (db.RATIO < REVIEW_LO) | (db.RATIO > REVIEW_HI),
            f"NEEDS REVIEW - ratio outside {REVIEW_LO}-{REVIEW_HI}", "")
        db["ROWID_FOUND"] = db.MASTER_OPS_ROWID.notna()
        db = db.sort_values(["OPERATION", "FAMILY", "OPERATION_SEQUENCE",
                             "ROUTE", "PART_ID"])

    # ---- the multi-load gap, reported not corrected ------------------------
    gap = (multi.groupby(["OPERATION"], dropna=False)
                .agg(WORK_ORDERS=("WORK_ORDER", "nunique"),
                     STEPS=("WORK_ORDER", "size"),
                     PARTS=("PART_ID", "nunique"),
                     MEDIAN_LOADS=("LOADS_CHARGED", "median"),
                     MAX_LOADS=("LOADS_CHARGED", "max"),
                     HOURS_MASTER=("HOURS_MASTER", "sum"),
                     HOURS_WORKED=("HOURS_WORKED", "sum"))
                .reset_index())
    gap["OVER_RESERVED_HOURS"] = (gap.HOURS_MASTER - gap.HOURS_WORKED).round(1)
    gap["RATIO"] = (gap.HOURS_WORKED / gap.HOURS_MASTER).round(3)
    # what a second load actually costs: fit HOURS_WORKED = a + b * loads
    fit = []
    for op, g in multi.groupby("OPERATION"):
        allg = pd.concat([one[one.OPERATION == op], g])
        allg = allg[allg.LOADS_CHARGED <= 6]
        if allg.LOADS_CHARGED.nunique() >= 3:
            b, a = np.polyfit(allg.LOADS_CHARGED, allg.HOURS_WORKED, 1)
            fit.append((op, round(a, 2), round(b, 2),
                        round(b / g.RUN_MASTER.median(), 3)))
        else:
            fit.append((op, np.nan, np.nan, np.nan))
    gap = gap.merge(pd.DataFrame(fit, columns=["OPERATION", "FIXED_HOURS_A",
                                               "MARGINAL_LOAD_B",
                                               "MARGINAL_AS_PCT_OF_RUN"]),
                    on="OPERATION", how="left")

    acts = len(pr) + len(fa)
    summary = pd.DataFrame([
        ("Scope", "HRS/LOAD run, current FY, single-load work orders", np.nan),
        ("Work orders in scope", len(one) + len(multi),
         round((one.HOURS_WORKED.sum() + multi.HOURS_WORKED.sum()), 1)),
        ("  ...single load, tested", len(one), round(one.HOURS_WORKED.sum(), 1)),
        ("  ...multi load, reported only", len(multi),
         round(multi.HOURS_WORKED.sum(), 1)),
        ("Part x sequence units", len(part), np.nan),
        (f"  ...testable (n >= {MIN_WO})", int((part.WO >= MIN_WO).sum()), np.nan),
        ("  ...flagged UPDATE RUN", len(pr), round(pr.HOURS_DELTA.sum(), 1)),
        ("Family x sequence groups", len(fam), np.nan),
        (f"  ...testable (n >= {MIN_WO})", int((fam.FAMEX_WO >= MIN_WO).sum()), np.nan),
        ("  ...flagged UPDATE FAMILY RUN", len(fa),
         round(fa.FAMEX_HOURS_DELTA.sum(), 1)),
        ("Actions total", acts, np.nan),
        ("Parts to edit", db.PART_ID.nunique() if len(db) else 0, np.nan),
        ("Pooled ratio, single load",
         round(one.HOURS_WORKED.sum() / one.HOURS_MASTER.sum(), 4), np.nan),
        ("Pooled ratio, multi load",
         round(multi.HOURS_WORKED.sum() / multi.HOURS_MASTER.sum(), 4), np.nan),
        ("Capacity reserved and not used, multi load", np.nan,
         round(multi.HOURS_MASTER.sum() - multi.HOURS_WORKED.sum(), 1)),
    ], columns=["METRIC", "VALUE", "HOURS"])

    with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="00_Summary", index=False)
        (part.sort_values("HOURS_DELTA", key=abs, ascending=False)
             .to_excel(xl, sheet_name="01_PartUnits", index=False))
        (fam.sort_values("FAMEX_HOURS_DELTA", key=abs, ascending=False)
            .to_excel(xl, sheet_name="02_FamilyRollup", index=False))
        db.to_excel(xl, sheet_name="03_LoadUpdateList", index=False)
        gap.sort_values("OVER_RESERVED_HOURS", ascending=False).to_excel(
            xl, sheet_name="04_MultiLoadGap", index=False)
        (one.sort_values(UNIT + ["LAST_DATE"])
            .to_excel(xl, sheet_name="05_Evidence", index=False))

    print(f"\npart units {len(part):,} | testable {(part.WO >= MIN_WO).sum():,}"
          f" | flagged {len(pr):,}")
    print(f"family groups {len(fam):,} | testable {(fam.FAMEX_WO >= MIN_WO).sum():,}"
          f" | flagged {len(fa):,}")
    print(f"\nrows to key in : {len(db):,}"
          f"  ({db.PART_ID.nunique() if len(db) else 0} parts)")
    print(f"pooled ratio, single load : "
          f"{one.HOURS_WORKED.sum() / one.HOURS_MASTER.sum():.3f}")
    print(f"pooled ratio, multi load  : "
          f"{multi.HOURS_WORKED.sum() / multi.HOURS_MASTER.sum():.3f}")
    print(f"capacity reserved and not used, multi load: "
          f"{multi.HOURS_MASTER.sum() - multi.HOURS_WORKED.sum():,.0f} h")
    print("\nfamily verdicts:")
    print(fam.FAMEX_VERDICT.value_counts().to_string())
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
