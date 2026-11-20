"""
Part + family view for one operation.

One row per part that had enough work orders to be tested on its own. Each row
carries three views side by side:

  1. the part itself      - does this part need a RUN_MASTER change?
  2. the whole family     - overview at the same operation sequence, every part
                            including the ones too thin to test alone
  3. the family with all  - the honest baseline: if what is left still deviates,
     UPDATE parts removed   the family standard is off, not just the flagged
                            parts

Family RUN_MASTER is an EFFECTIVE rate, sum(qty) / sum(hours_master), because
parts within a family carry different RUN_MASTER values. N_DISTINCT_RM and the
min/max say whether a single number is meaningful for that family.

Reads the part-level workbook for the same operation, so run fts_analysis.py
first.

Input:  FTS_<OPERATION>_PARTS.xlsx
Output: FTS_<OPERATION>_FAMILY.xlsx
"""

import os
import re
import sys
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore", category=RuntimeWarning)

CACHE = "fts.parquet"

OPERATION = sys.argv[1] if len(sys.argv) > 1 else "S FRESATURA"
# must match the operation the part-level workbook was built for


def slug(operation):
    """Filename-safe operation tag. Operation names carry dots, slashes and
    the odd mis-encoded character, so strip to alphanumerics."""
    return re.sub(r"[^A-Za-z0-9]+", "_", (operation or "ALL").strip()
                  ).strip("_").upper() or "OP"


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
PART_LEVEL = os.path.join(OUTPUTS, f"FTS_{slug(OPERATION)}_PARTS.xlsx")
OUT = os.path.join(OUTPUTS, f"FTS_{slug(OPERATION)}_FAMILY.xlsx")
# Must match the hard-coded date in fts.sql - see the note in
# fts_analysis.py. Change every copy together or the extract and the filter
# describe different years.
FY_START = "2025-10-01"
MIN_WO = 6
ALPHA = 0.05
MIN_EFFECT = 0.10
MIN_HOURS_IMPACT = 20   # applied at family level, where the change is made
MAD_K = 3.0
# Work orders closed with a token booking rather than a measured duration. Must
# match TOKEN_HOURS in fts_analysis.py: the part-level pass strips the parts
# it flags out of the family pool, so the two have to agree on which work
# orders exist at all. See the note on drop_token_bookings below.
TOKEN_HOURS = 0.02


def load():
    df = pd.read_parquet(CACHE)
    for c in ["FAMILY", "PART_TYPE", "PART_ID", "WORK_ORDER", "WO_REV",
              "OPERATION", "LT_TYPE", "RUN_TYPE_MASTER"]:
        df[c] = df[c].str.strip()
    df["LAST_TRANSACTION_DATE"] = pd.to_datetime(df["LAST_TRANSACTION_DATE"],
                                                 errors="coerce")
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
             & (df.OPERATION == OPERATION)
             & (df.LAST_TRANSACTION_DATE >= pd.Timestamp(FY_START))
             & (df.RUN_MASTER > 0)
             & (df.HOURS_MASTER > 0)
             & (df.HOURS_WORKED > 0)
             # step must still exist in the current routing
             & df.MASTER_OPS_ROWID.notna())
    wo = (df[scope]
          .groupby(["FAMILY", "PART_TYPE", "PART_ID", "OPERATION_SEQUENCE",
                    "OPERATION", "WORK_ORDER"], dropna=False)
          .agg(RUN_MASTER=("RUN_MASTER", "max"),
               LAST_DATE=("LAST_TRANSACTION_DATE", "max"),
               TOTAL_QTY=("TOTAL_QTY", "sum"),
               HOURS_WORKED=("HOURS_WORKED", "sum"),
               HOURS_MASTER=("HOURS_MASTER", "sum"))
          .reset_index())
    # A work order booked at or below TOKEN_HOURS records that the ticket was
    # closed, not how long the job took - a flat 0.01 h whether the order held
    # one piece or five hundred. Pooling makes this worse than it is at part
    # level: a family pool can be a majority tokens, at which point the MAD
    # filter centres ON them and trims the real work orders away as outliers,
    # and the pool then reports a ratio near zero with a tiny interval. Dropping
    # them leaves the MIN_WO floor to reject the pool if too little is left.
    tok = wo.HOURS_WORKED <= TOKEN_HOURS
    if tok.any():
        print(f"token bookings dropped: {tok.sum():,} work orders "
              f"({tok.mean():.1%}), {wo.loc[tok, 'HOURS_WORKED'].sum():.2f} h "
              f"booked, {wo.loc[tok, 'TOTAL_QTY'].sum():,} pieces")
    wo = wo[~tok].copy()
    wo["LOG_RATIO"] = np.log(wo.HOURS_WORKED / wo.HOURS_MASTER)
    # Each part+sequence cut to its own latest RUN_MASTER, so no superseded
    # standard leaks into a pooled figure. Keyed on the sequence as well as the
    # part: one part often runs the same operation at several sequences under
    # different standards, and grouping on PART_ID alone would keep whichever
    # ran most recently and silently drop the rest.
    key = ["PART_ID", "OPERATION_SEQUENCE"]
    latest = (wo.sort_values("LAST_DATE").groupby(key)["RUN_MASTER"]
                .last().rename("_CUR"))
    wo = wo.join(latest, on=key)
    return wo[wo.RUN_MASTER == wo._CUR].drop(columns="_CUR")


def pooled(pool, prefix):
    """Descriptives + test for a pool of work orders."""
    res = {f"{prefix}_PARTS": pool.PART_ID.nunique(),
           f"{prefix}_WO": len(pool)}
    if len(pool) == 0:
        for k in ["RATIO", "RM_CURRENT", "RM_SUGGESTED", "CI95_LOW",
                  "CI95_HIGH", "Q_RAW_TTEST", "P_WILCOXON", "N_DISTINCT_RM",
                  "RM_MIN", "RM_MAX", "HOURS_MASTER", "HOURS_WORKED"]:
            res[f"{prefix}_{k}"] = np.nan
        return res

    lr = pool["LOG_RATIO"].to_numpy()
    med = float(np.median(lr))
    mad = float(np.median(np.abs(lr - med))) * 1.4826
    keep = (np.abs(lr - med) <= MAD_K * mad if mad > 0
            else np.ones(len(lr), bool))
    pk = pool[keep]

    hm, hw, qty = pk.HOURS_MASTER.sum(), pk.HOURS_WORKED.sum(), pk.TOTAL_QTY.sum()
    ratio = hw / hm if hm > 0 else np.nan
    # effective current rate for the pool; parts differ, so this is the
    # hours-weighted blend rather than any one stored RUN_MASTER
    rm_cur = qty / hm if hm > 0 else np.nan

    res.update({
        f"{prefix}_HOURS_MASTER": round(pool.HOURS_MASTER.sum(), 2),
        f"{prefix}_HOURS_WORKED": round(pool.HOURS_WORKED.sum(), 2),
        f"{prefix}_RATIO": ratio,
        f"{prefix}_RM_CURRENT": round(rm_cur, 2),
        f"{prefix}_RM_SUGGESTED": round(rm_cur / ratio, 2) if ratio else np.nan,
        f"{prefix}_N_DISTINCT_RM": int(pool.RUN_MASTER.nunique()),
        f"{prefix}_RM_MIN": float(pool.RUN_MASTER.min()),
        f"{prefix}_RM_MAX": float(pool.RUN_MASTER.max()),
        f"{prefix}_CI95_LOW": np.nan, f"{prefix}_CI95_HIGH": np.nan,
        f"{prefix}_Q_RAW_TTEST": np.nan, f"{prefix}_P_WILCOXON": np.nan,
    })

    if len(lr) >= MIN_WO and np.any(lr != 0):
        try:
            res[f"{prefix}_P_WILCOXON"] = float(
                stats.wilcoxon(lr, zero_method="wilcox")[1])
        except ValueError:
            pass
        t = stats.ttest_1samp(lr, 0.0)
        res[f"{prefix}_Q_RAW_TTEST"] = float(t.pvalue)
        lo, hi = t.confidence_interval(1 - ALPHA)
        res[f"{prefix}_CI95_LOW"] = float(np.exp(lo))
        res[f"{prefix}_CI95_HIGH"] = float(np.exp(hi))
    return res


def bh_fdr(p):
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


def family_rollup(wo, update_parts, tested_parts):
    """Every family x sequence in scope, whether or not any single part of it
    had enough work orders to be tested alone.

    This is the deliverable for thinly-spread operations. Penetrant testing
    runs ~5 hours per part+sequence, so almost no part clears a per-part
    materiality bar - but the family pools thousands of hours and a default
    standard stamped across 30 families shows up immediately.
    """
    rows = []
    # PART_TYPE splits the pool. A family carries both the semi-finished
    # article and the finished one, and at a shared sequence they are two
    # different physical jobs - pooling them averages a part that is half
    # made with one that is complete. Where the two genuinely differ the
    # blend describes neither, and worse, it can land near 1.0 and report a
    # group as clean while both halves are wrong in opposite directions.
    for (fam, ptype, seq), g in wo.groupby(["FAMILY", "PART_TYPE",
                                            "OPERATION_SEQUENCE"]):
        rows.append({
            "FAMILY": fam, "PART_TYPE": ptype, "OPERATION_SEQUENCE": seq,
            "OPERATION": g.OPERATION.iat[0],
            "PARTS_TESTED_ALONE": g.PART_ID.isin(tested_parts).groupby(
                g.PART_ID).any().sum(),
            "PARTS_FLAGGED_UPDATE": g.PART_ID.isin(update_parts).groupby(
                g.PART_ID).any().sum(),
            **pooled(g, "FAM"),
            **pooled(g[~g.PART_ID.isin(update_parts)], "FAMEX")})
    r = pd.DataFrame(rows)

    r["FAMEX_HOURS_DELTA"] = (r.FAMEX_HOURS_WORKED
                              - r.FAMEX_HOURS_MASTER).round(2)
    r["FAMEX_Q_VALUE"] = bh_fdr(r["FAMEX_Q_RAW_TTEST"])
    r["FAMEX_SIGNIFICANT"] = ((r.FAMEX_Q_VALUE < ALPHA)
                              & (r.FAMEX_P_WILCOXON < ALPHA)
                              & ((r.FAMEX_CI95_LOW > 1)
                                 | (r.FAMEX_CI95_HIGH < 1)))
    # the absolute-hours gate belongs at the level the change is actually made,
    # which for a thin operation is the family, not the part
    r["FAMEX_VERDICT"] = np.where(
        r.FAMEX_WO < MIN_WO, f"insufficient data (n<{MIN_WO})",
        np.where(~r.FAMEX_SIGNIFICANT, "family OK - not different",
                 np.where((r.FAMEX_RATIO - 1).abs() < MIN_EFFECT,
                          f"family off but below {MIN_EFFECT:.0%}",
                          np.where(r.FAMEX_HOURS_DELTA.abs() < MIN_HOURS_IMPACT,
                                   f"family off but under {MIN_HOURS_IMPACT}h",
                                   "UPDATE FAMILY STANDARD"))))
    cols = ["FAMILY", "PART_TYPE", "OPERATION_SEQUENCE", "OPERATION",
            "FAMEX_VERDICT",
            "FAM_PARTS", "FAM_WO", "FAM_RATIO",
            "PARTS_TESTED_ALONE", "PARTS_FLAGGED_UPDATE",
            "FAMEX_PARTS", "FAMEX_WO", "FAMEX_HOURS_MASTER",
            "FAMEX_HOURS_WORKED", "FAMEX_HOURS_DELTA", "FAMEX_RATIO",
            "FAMEX_RM_CURRENT", "FAMEX_RM_SUGGESTED",
            "FAMEX_N_DISTINCT_RM", "FAMEX_RM_MIN", "FAMEX_RM_MAX",
            "FAMEX_CI95_LOW", "FAMEX_CI95_HIGH", "FAMEX_P_WILCOXON",
            "FAMEX_Q_VALUE"]
    return r[cols].sort_values("FAMEX_HOURS_DELTA", ascending=False)


def main():
    wo = load()

    # Same case the part-level script guards: an operation whose every work
    # order was a token booking comes back empty, and the rollup below then
    # fails on a column that was never built. Write the empty workbook anyway -
    # leaving the previous one on disk would let the update list read a stale
    # result and report it as current.
    if wo.empty:
        cols = ["FAMILY", "PART_TYPE", "OPERATION_SEQUENCE", "OPERATION",
                "FAMEX_VERDICT", "FAM_PARTS", "FAM_WO", "FAM_RATIO",
                "FAMEX_PARTS", "FAMEX_WO", "FAMEX_HOURS_DELTA", "FAMEX_RATIO"]
        with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
            for sheet in ["01_FamilyRollup", "02_FamilyActions",
                          "03_PartAndFamily", "04_UpdateParts"]:
                pd.DataFrame(columns=cols).to_excel(xl, sheet_name=sheet,
                                                    index=False)
        print(f"wrote {OUT} - nothing in scope, no family pools")
        return

    focal = pd.read_excel(PART_LEVEL, sheet_name="04_Tested_WithEvidence")
    update_parts = set(focal.loc[focal.RECOMMENDATION == "UPDATE RUN_MASTER",
                                 "PART_ID"])
    print(f"parts flagged UPDATE and stripped from the family pools: "
          f"{len(update_parts)}")

    rows = []
    for _, f in focal.iterrows():
        fam = wo[(wo.FAMILY == f.FAMILY)
                 & (wo.PART_TYPE == f.PART_TYPE)
                 & (wo.OPERATION_SEQUENCE == f.OPERATION_SEQUENCE)]
        rows.append({**pooled(fam, "FAM"),
                     **pooled(fam[~fam.PART_ID.isin(update_parts)], "FAMEX")})

    # An operation where no part cleared MIN_WO leaves `focal` empty, so there is
    # nothing to concatenate and every derived column below would raise. The
    # family rollup further down does not depend on any part being testable, so
    # the workbook is still worth writing - only this sheet comes out empty.
    if rows:
        out = pd.concat([
            focal[["FAMILY", "PART_TYPE", "PART_ID", "OPERATION_SEQUENCE",
                   "OPERATION",
                   "N_WORK_ORDERS", "RATIO_OUTLIERS_REMOVED", "RECOMMENDATION",
                   "RUN_MASTER_CURRENT", "RUN_MASTER_SUGGESTED"]]
            .rename(columns={"N_WORK_ORDERS": "PART_WO",
                             "RATIO_OUTLIERS_REMOVED": "PART_RATIO",
                             "RECOMMENDATION": "PART_VERDICT",
                             "RUN_MASTER_CURRENT": "PART_RM_CURRENT",
                             "RUN_MASTER_SUGGESTED": "PART_RM_SUGGESTED"}),
            pd.DataFrame(rows)], axis=1)

        out["FAMEX_Q_VALUE"] = bh_fdr(out["FAMEX_Q_RAW_TTEST"])
        out["FAMEX_SIGNIFICANT"] = ((out.FAMEX_Q_VALUE < ALPHA)
                                    & (out.FAMEX_P_WILCOXON < ALPHA)
                                    & ((out.FAMEX_CI95_LOW > 1)
                                       | (out.FAMEX_CI95_HIGH < 1)))
        out["FAMEX_VERDICT"] = np.where(
            out.FAMEX_WO < MIN_WO, f"insufficient data (n<{MIN_WO})",
            np.where(~out.FAMEX_SIGNIFICANT, "family OK - not different",
                     np.where((out.FAMEX_RATIO - 1).abs() < MIN_EFFECT,
                              f"family off but below {MIN_EFFECT:.0%}",
                              "FAMILY DEVIATES MATERIALLY")))

        # agreement is measured against the family with the UPDATE parts removed
        gap = out.PART_RATIO - out.FAMEX_RATIO
        out["PART_VS_FAMEX_GAP"] = gap.round(3)
        out["AGREEMENT"] = np.where(
            out.FAMEX_WO < MIN_WO, "family too thin to compare",
            np.where(gap.abs() <= 0.05, "agree (within 5%)",
                     np.where(np.sign(out.PART_RATIO - 1)
                              == np.sign(out.FAMEX_RATIO - 1),
                              "same direction, different size",
                              "conflict - opposite directions")))
    else:
        out = pd.DataFrame()

    cols = ["FAMILY", "PART_TYPE", "PART_ID", "OPERATION_SEQUENCE",
            "OPERATION", "PART_WO", "PART_RATIO", "PART_VERDICT",
            "FAM_PARTS", "FAM_WO", "FAM_RATIO",
            "FAMEX_PARTS", "FAMEX_WO", "FAMEX_RATIO",
            "AGREEMENT", "PART_VS_FAMEX_GAP",
            "PART_RM_CURRENT", "PART_RM_SUGGESTED",
            "FAMEX_RM_CURRENT", "FAMEX_RM_SUGGESTED",
            # --- evidence ---
            "FAMEX_VERDICT", "FAMEX_CI95_LOW", "FAMEX_CI95_HIGH",
            "FAMEX_P_WILCOXON", "FAMEX_Q_VALUE",
            "FAMEX_N_DISTINCT_RM", "FAMEX_RM_MIN", "FAMEX_RM_MAX",
            "FAM_HOURS_MASTER", "FAM_HOURS_WORKED",
            "FAMEX_HOURS_MASTER", "FAMEX_HOURS_WORKED"]
    # reindex rather than [cols] so the empty case still carries the schema
    out = (out.reindex(columns=cols)
              .sort_values(["FAMILY", "OPERATION_SEQUENCE", "PART_ID"]))

    roll = family_rollup(wo, update_parts,
                         set(focal.loc[focal.N_WORK_ORDERS >= MIN_WO,
                                       "PART_ID"]))

    with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
        roll.to_excel(xl, sheet_name="01_FamilyRollup", index=False)
        (roll[roll.FAMEX_VERDICT == "UPDATE FAMILY STANDARD"]
            .to_excel(xl, sheet_name="02_FamilyActions", index=False))
        out.to_excel(xl, sheet_name="03_PartAndFamily", index=False)
        (out[out.PART_VERDICT == "UPDATE RUN_MASTER"]
            .to_excel(xl, sheet_name="04_UpdateParts", index=False))

    print("\nfamily x sequence groups covered: %d (was limited to the %d "
          "containing a tested part)" % (len(roll), out.FAMILY.nunique()))
    print("\nfamily verdict once UPDATE parts are removed:")
    print(roll.FAMEX_VERDICT.value_counts().to_string())
    print("\nagreement, part vs family-excluding-UPDATE:")
    print(out.AGREEMENT.value_counts().to_string())
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
