"""
Setup times, family level, every PCS/HR operation at once.

Setup is not the run analysis with a different filter. Three things differ, and
each of them changes the answer:

  1. SETUP_HRS is a duration, not a rate, so the correction MULTIPLIES:
     SETUP_HRS_new = SETUP_HRS_old * ratio. RUN_MASTER divides.

  2. A setup booked at ~0 hours was not performed. When several work orders of
     the same family run back to back on one machine, only the first needs a
     full setup. That is normal, not a booking error - but pooling those rows
     measures "how often we skip" rather than "how long a setup takes", and the
     two answers are far apart: on one section, against a standard that
     holds, including them reads as a cut of two thirds. The population matters, and is checked in
     fts_checks.py.

  3. Part level is hopeless. Setup units average under 4 worked hours over a
     whole fiscal year, so nothing clears a materiality gate alone. Families
     pool enough work orders to be judged; individual parts never do.

Skipped setups are reported separately (03_SkipRate) rather than folded into the
suggestions. The ERP charges the standard on every work order whether or not a
setup happened, so a family skipping half its setups carries roughly double the
setup time it consumes - a routing and scheduling question, not a standards one,
and it goes to different people.

03_SkipRate covers BOTH run types, and it is the only sheet that does. Skipping
is a counting question, so it needs no test and no minimum sample: HRS/LOAD
setup carries a standard on one operation only, with three performed setups in
the whole year against a floor of six, which makes it untestable but not
unreportable. The RUN_TYPE column keeps the two apart. Every other sheet here
is PCS/HR.

Output: FTS_SETUP.xlsx
"""

import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore", category=RuntimeWarning)

CACHE = "fts.parquet"
OUT = "FTS_SETUP.xlsx"

# Must match the hard-coded date in fts.sql - see the note in
# fts_analysis.py. Change every copy together or the extract and the filter
# describe different years.
FY_START = "2025-10-01"
# hours below which a setup ticket records that no setup happened. The booked
# distribution has a clean gap here - the 20th percentile is 0.010 and the 25th
# is 0.070 - so the cut is not sensitive to where in that gap it sits.
SKIP_CUT = 0.02
MIN_SETUPS = 6        # real setups needed before a family is judged
ALPHA = 0.05
MIN_EFFECT = 0.10
MIN_HOURS_IMPACT = 5  # setup groups carry ~12h of standard a year, not ~90
MAD_K = 3.0
# two setup values closer than this are the same value - must match
# fts_update_list.SAME_VALUE. SETUP_HRS_MASTER is read from the operation at 4
# decimals, so in practice the values compared here are equal or far apart.
SAME_VALUE = 0.005

# PART_TYPE splits the pool for the same reason it does in the run analysis:
# a family holds both the semi-finished article and the finished one, and
# setting up for one is not setting up for the other. Everything downstream
# keys off UNIT, so adding it here propagates to the rollup, the actions and
# the member list without further change.
UNIT = ["OPERATION", "FAMILY", "PART_TYPE", "OPERATION_SEQUENCE"]


def load(run_type="PCS/HR"):
    """Setup work orders for one run type.

    PCS/HR is the analysed scope, matching the run analysis. HRS/LOAD is loaded
    for the skip report only - it carries a standard on exactly one operation
    and three performed setups in the whole year, far below MIN_SETUPS, so
    there is nothing to test. What it does carry is 154 hours charged for
    setups that never happened, and that belongs in 03_SkipRate with the rest.
    """
    df = pd.read_parquet(CACHE)
    if "SETUP_HRS_MASTER" not in df.columns:
        raise ValueError(
            f"{CACHE} has no SETUP_HRS_MASTER column - it was built from an "
            "extract older than 9 Oct 2026. Run the current fts.sql and take "
            "it in with new_extract.py.")
    for c in ["FAMILY", "PART_TYPE", "PART_ID", "WORK_ORDER", "OPERATION",
              "LT_TYPE", "RUN_TYPE_MASTER", "ANALYTIC_SECTION"]:
        df[c] = df[c].str.strip()
    df["ANALYTIC_SECTION"] = df.ANALYTIC_SECTION.str.title()
    df["LAST_TRANSACTION_DATE"] = pd.to_datetime(df["LAST_TRANSACTION_DATE"],
                                                 errors="coerce")
    scope = ((df.LT_TYPE == "Setup")
             # One run type at a time. The two are never pooled: SETUP_HRS is
             # the same field, but HRS/LOAD sets it on a different kind of
             # operation and its Heat Treatment rows are where the extreme 0.05
             # ratios sat.
             & (df.RUN_TYPE_MASTER == run_type)
             & ~df.OPERATION.str.upper().str.startswith("S RIL")
             & (df.LAST_TRANSACTION_DATE >= pd.Timestamp(FY_START))
             & (df.HOURS_MASTER > 0)
             & (df.HOURS_WORKED > 0)
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

    # ---- did the crew that booked match the crew the routing plans for? -----
    # NOT a scope filter: these rows stay in wo, because skip_rate has to keep
    # counting what the ERP charged whether or not the standard describes it.
    # test_family drops them from the TEST instead.
    #
    # ONE CONDITION: EMP_DISTINCT == USER_5, the planned crew is who actually
    # booked. A 1-person 1-hour setup performed by two people is not a fast or
    # slow version of that plan, it is a different job.
    #
    # THERE WERE TWO until 2026-10-09. The second, LT_ROWS == EMP_DISTINCT
    # ("nobody booked twice"), existed because HOURS_MASTER was rebuilt from
    # the tickets - SETUP_HRS/USER_5 credited once per ticket group - so a
    # second ticket from one person doubled the master and halved the ratio.
    # Measured on the 13 Aug 2026 extract, 73 rows would have passed a
    # crew-only test with a doubled master. The test now reads
    # SETUP_HRS_MASTER, the work order's own value, which no ticket count can
    # move, so the condition protected nothing any more and excluded real
    # setups: on the 9 Oct 2026 extract, 89 performed setups by the right crew,
    # one of whom had booked in two goes. They are evidence now. LT_ROWS stays
    # in the extract.
    #
    # Evaluated per PARQUET ROW and then required of all of them (.all()),
    # because a work order x step can span several resources and the counts are
    # per resource row. Summing EMP_DISTINCT across resources would double-count
    # anyone who worked on two.
    #
    # This says whether the crew that booked is the crew the routing plans
    # for. It does NOT make HOURS_MASTER equal SETUP_HRS, which this note used
    # to claim - see the SETUP_HRS_MASTER note below.
    df["CREW_OK"] = df.EMP_DISTINCT == df.USER_5.fillna(1)

    wo = (df[scope]
          .groupby(UNIT + ["PART_ID", "WORK_ORDER"], dropna=False)
          .agg(ANALYTIC_SECTION=("ANALYTIC_SECTION", "first"),
               MASTER_OPS_ROWID=("MASTER_OPS_ROWID", "first"),
               USER_5=("USER_5", "max"),
               CREW_OK=("CREW_OK", "all"),
               LAST_DATE=("LAST_TRANSACTION_DATE", "max"),
               HOURS_WORKED=("HOURS_WORKED", "sum"),
               HOURS_MASTER_TICKETS=("HOURS_MASTER", "sum"),
               SETUP_HRS_MASTER=("SETUP_HRS_MASTER", "max"))
          .reset_index())

    # THE STANDARD EACH SETUP IS JUDGED AGAINST IS THE WORK ORDER'S OWN
    # SETUP_HRS (2026-10-09), read straight from its operation by fts.sql - the
    # setup counterpart of RUN_MASTER. It used to be the extract's HOURS_MASTER,
    # which fts.sql REBUILDS from the tickets: SETUP_HRS / USER_5 credited once
    # per inner row. The inner query merges tickets that share a transaction
    # date and a department, so a crew-3 setup at 0.50 h booked by two people of
    # one department and one of another came out at 0.3334, and the job read
    # x1.65 where the operators' own screen said x1.10. Where nothing merged it
    # came out at 0.5001 - 0.5/3 at 4 decimals, three times - and every crew-3
    # setup was judged against that.
    #
    # HOURS_MASTER_TICKETS keeps the rebuilt figure, in 05_Evidence only, so the
    # two can be compared work order by work order. Nothing sums it any more:
    # the skip report's "standard charged" moved to SETUP_HRS_MASTER as well on
    # 2026-10-09 (a second commit) - the ERP reserves the work order's SETUP_HRS
    # whether or not a setup happens, and the ticket-built figure counted it
    # twice wherever somebody closed a skipped setup with two tickets.
    #
    # One value per work order. No setup in the 9 Oct 2026 extract is booked on
    # more than one resource, so the max is that work order's SETUP_HRS.
    wo["HOURS_MASTER"] = wo.SETUP_HRS_MASTER

    wo["SKIPPED"] = wo.HOURS_WORKED <= SKIP_CUT
    wo["LOG_RATIO"] = np.log(wo.HOURS_WORKED / wo.HOURS_MASTER)

    # SETUP_HRS_STORED: the setup value IN FORCE for a part+sequence - the
    # SETUP_HRS of its latest performed work order, or of its latest work order
    # where none was performed. The same rule as Std WO on the run analyses:
    # the value the last timed jobs ran with, token bookings ignored
    # (SKIP_CUT and TOKEN_HOURS are the same 0.02 h).
    #
    # It used to be the LARGEST value the part's crew-matched setups carried,
    # and the run and load analyses never worked that way. Where a part's setup
    # moved during the year the max kept the larger one, however briefly it
    # held: one prototype work order at 2.00 h among a year at 0.50 h made the
    # part's setup "2.00", and the family factor was then applied to it.
    #
    # ON_CURRENT: the work order ran under that value. test_family tests only
    # these, as fts_analysis.test_group does for RUN_MASTER - a setup run under
    # a value since replaced describes a standard that no longer exists.
    # Unlike the run path there is no "watch" verdict: a family left with too
    # few setups falls to the MIN_SETUPS floor like any other.
    key = ["PART_ID", "OPERATION_SEQUENCE", "OPERATION"]
    order = wo.sort_values("LAST_DATE", kind="stable")

    def last(w):
        return w.groupby(key).tail(1).set_index(key).SETUP_HRS_MASTER

    cur = last(order[~order.SKIPPED]).combine_first(last(order))
    wo = wo.join(cur.rename("SETUP_HRS_STORED"), on=key)
    wo["ON_CURRENT"] = ((wo.SETUP_HRS_MASTER - wo.SETUP_HRS_STORED).abs()
                        <= SAME_VALUE)
    return wo


def skip_rate(wo, run_type):
    """How often setup is skipped, and what the ERP charged for it anyway.

    Descriptive only - no test, no suggestion. That is what lets HRS/LOAD sit
    in the same sheet as PCS/HR: skipping is counted the same way whether or
    not there is enough evidence to judge how long a setup takes.

    STANDARD_CHARGED_SKIPPED is the number that matters. The ERP charges
    SETUP_HRS on every work order whether a setup happened or not, so a family
    skipping half its setups reserves roughly double the setup time it
    consumes. That is a routing and scheduling question, not a standards one.
    """
    g = (wo.groupby(UNIT, dropna=False)
         .agg(SECTION=("ANALYTIC_SECTION", "first"),
              PARTS=("PART_ID", "nunique"),
              WO_ALL=("WORK_ORDER", "size"),
              WO_SKIPPED=("SKIPPED", "sum"),
              SETUP_HRS_CURRENT=("SETUP_HRS_STORED", "max"),
              STANDARD_CHARGED_ALL_WO=("HOURS_MASTER", "sum"))
         .reset_index())
    sk = (wo[wo.SKIPPED].groupby(UNIT, dropna=False).HOURS_MASTER.sum()
          .rename("STANDARD_CHARGED_SKIPPED").reset_index())
    g = g.merge(sk, on=UNIT, how="left").fillna({"STANDARD_CHARGED_SKIPPED": 0})
    g["WO_REAL"] = g.WO_ALL - g.WO_SKIPPED
    g["SKIP_RATE"] = (g.WO_SKIPPED / g.WO_ALL).round(3)
    g["RUN_TYPE"] = run_type
    for c in ["STANDARD_CHARGED_ALL_WO", "STANDARD_CHARGED_SKIPPED"]:
        g[c] = g[c].round(2)
    return g[["RUN_TYPE"] + UNIT + [
        "SECTION", "PARTS", "WO_ALL", "WO_SKIPPED", "WO_REAL", "SKIP_RATE",
        "SETUP_HRS_CURRENT", "STANDARD_CHARGED_ALL_WO",
        "STANDARD_CHARGED_SKIPPED"]]


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


def test_family(g):
    """One family x sequence. Skipped setups are excluded from the test but
    still counted, because how often setup is skipped is its own finding.

    Setups whose crew did not match the routing are excluded the same way and
    for the same reason: they are still charged, still counted, and still
    reported - but the standard does not describe them, so they cannot be
    evidence about whether it is right. See load() for the two conditions.

    So are setups run under a SETUP_HRS the part has since replaced
    (ON_CURRENT, see load()): they describe a standard that no longer exists.
    """
    performed = g[~g.SKIPPED]
    crew = performed[performed.CREW_OK]
    real = crew[crew.ON_CURRENT]
    res = {
        "SECTION": g.ANALYTIC_SECTION.iat[0],
        "PARTS": g.PART_ID.nunique(),
        "WO_ALL": len(g),
        "WO_SKIPPED": int(g.SKIPPED.sum()),
        "WO_PERFORMED": len(performed),
        # WO_REAL is what the test actually ran on, and the floor is applied to
        # it. Named the same as before so MIN_SETUPS and every downstream
        # reader keep pointing at the tested count rather than the performed
        # one - those are now different numbers.
        "WO_REAL": len(real),
        "WO_CREW_MISMATCH": int((~performed.CREW_OK).sum()),
        # crew-matched and performed, but under a setup value since replaced
        "WO_SUPERSEDED": int((~crew.ON_CURRENT).sum()),
        "SKIP_RATE": round(g.SKIPPED.mean(), 3),
        "SETUP_HRS_CURRENT": np.nan, "SETUP_HRS_N_DISTINCT": np.nan,
        "HOURS_MASTER": np.nan, "HOURS_WORKED": np.nan, "HOURS_DELTA": np.nan,
        "RATIO": np.nan, "RATIO_OUTLIERS_REMOVED": np.nan,
        "CI95_LOW": np.nan, "CI95_HIGH": np.nan,
        "P_WILCOXON": np.nan, "P_RAW_TTEST": np.nan,
        "FIRST_DATE": g.LAST_DATE.min(), "LAST_DATE": g.LAST_DATE.max(),
        # the standard the ERP charges on every work order, performed or not
        "STANDARD_CHARGED_ALL_WO": round(g.HOURS_MASTER.sum(), 2),
        "STANDARD_CHARGED_SKIPPED": round(
            g.loc[g.SKIPPED, "HOURS_MASTER"].sum(), 2),
    }
    if len(real) == 0:
        return pd.Series(res)

    lr = real.LOG_RATIO.to_numpy()
    med = float(np.median(lr))
    mad = float(np.median(np.abs(lr - med))) * 1.4826
    keep = np.abs(lr - med) <= MAD_K * mad if mad > 0 else np.ones(len(lr), bool)
    rk = real[keep]

    hm, hw = real.HOURS_MASTER.sum(), real.HOURS_WORKED.sum()
    hmk, hwk = rk.HOURS_MASTER.sum(), rk.HOURS_WORKED.sum()

    res.update({
        # parts in one family can carry different SETUP_HRS, so the family
        # figure is the hours-weighted effective duration, not any one value
        #
        # SETUP_HRS_STORED is one value per part - the setup in force, see
        # load() - and every tested setup ran under it, so the median here is
        # the family's typical setup and SETUP_HRS_N_DISTINCT counts how many
        # different values its parts hold. It was the largest value each
        # part's setups had carried until 2026-10-09, which kept a superseded
        # standard alive wherever it was the larger one.
        "SETUP_HRS_CURRENT": round(real.SETUP_HRS_STORED.median(), 4),
        "SETUP_HRS_N_DISTINCT": int(real.SETUP_HRS_STORED.nunique()),
        "HOURS_MASTER": round(hm, 2),
        "HOURS_WORKED": round(hw, 2),
        "HOURS_DELTA": round(hw - hm, 2),
        "RATIO": hw / hm if hm > 0 else np.nan,
        "RATIO_OUTLIERS_REMOVED": hwk / hmk if hmk > 0 else np.nan,
    })

    if len(lr) >= MIN_SETUPS and np.any(lr != 0):
        try:
            res["P_WILCOXON"] = float(stats.wilcoxon(lr, zero_method="wilcox")[1])
        except ValueError:
            pass
        t = stats.ttest_1samp(lr, 0.0)
        res["P_RAW_TTEST"] = float(t.pvalue)
        lo, hi = t.confidence_interval(1 - ALPHA)
        res["CI95_LOW"], res["CI95_HIGH"] = float(np.exp(lo)), float(np.exp(hi))
    return pd.Series(res)


def verdict(r):
    if r.WO_REAL < MIN_SETUPS:
        return f"insufficient real setups (n<{MIN_SETUPS})"
    if not r.IS_SIGNIFICANT:
        return "no change - not statistically different"
    if abs(r.RATIO_OUTLIERS_REMOVED - 1) < MIN_EFFECT:
        return f"no change - deviation below {MIN_EFFECT:.0%}"
    if abs(r.HOURS_DELTA) < MIN_HOURS_IMPACT:
        return f"no change - impact below {MIN_HOURS_IMPACT}h"
    return "UPDATE SETUP_HRS"


def main():
    wo = load()
    load_ls = load("HRS/LOAD")
    print(f"setup work orders in scope : {len(wo):,}")
    print(f"  performed                : {(~wo.SKIPPED).sum():,}")
    print(f"  skipped (<= {SKIP_CUT} h)     : {wo.SKIPPED.sum():,} "
          f"({wo.SKIPPED.mean():.1%})")

    res = wo.groupby(UNIT, dropna=False).apply(
        test_family, include_groups=False).reset_index()

    res["Q_VALUE_FDR"] = bh_fdr(res.P_RAW_TTEST)
    res["IS_SIGNIFICANT"] = ((res.Q_VALUE_FDR < ALPHA)
                             & (res.P_WILCOXON < ALPHA)
                             & ((res.CI95_LOW > 1) | (res.CI95_HIGH < 1)))
    res["VERDICT"] = res.apply(verdict, axis=1)

    # SETUP_HRS is a DURATION: longer actual -> longer standard, so multiply.
    # (RUN_MASTER is a rate and divides. Getting this backwards produces
    # plausible numbers wrong in both directions at once.)
    res["SETUP_HRS_SUGGESTED"] = (
        res.SETUP_HRS_CURRENT * res.RATIO_OUTLIERS_REMOVED)
    res["SETUP_HRS_SUGGESTED"] = np.where(
        res.SETUP_HRS_SUGGESTED >= 10,
        res.SETUP_HRS_SUGGESTED.round(1), res.SETUP_HRS_SUGGESTED.round(2))
    res["SETUP_HRS_SUGGESTED"] = res.SETUP_HRS_SUGGESTED.where(
        res.VERDICT == "UPDATE SETUP_HRS")
    res["CHANGE_PCT"] = ((res.SETUP_HRS_SUGGESTED / res.SETUP_HRS_CURRENT - 1)
                         * 100).round(1)

    res = res.sort_values("HOURS_DELTA", ascending=False,
                          key=lambda s: s.abs() if s.name else s)

    cols = UNIT + [
        "SECTION", "VERDICT", "PARTS", "WO_ALL", "WO_PERFORMED", "WO_REAL",
        "WO_CREW_MISMATCH", "WO_SUPERSEDED", "WO_SKIPPED",
        "SKIP_RATE", "SETUP_HRS_CURRENT", "SETUP_HRS_SUGGESTED", "CHANGE_PCT",
        "HOURS_MASTER", "HOURS_WORKED", "HOURS_DELTA",
        "RATIO", "RATIO_OUTLIERS_REMOVED", "CI95_LOW", "CI95_HIGH",
        "P_WILCOXON", "Q_VALUE_FDR", "IS_SIGNIFICANT", "SETUP_HRS_N_DISTINCT",
        "STANDARD_CHARGED_ALL_WO", "STANDARD_CHARGED_SKIPPED",
        "FIRST_DATE", "LAST_DATE"]
    res = res[cols]

    actions = res[res.VERDICT == "UPDATE SETUP_HRS"]

    # one row per part inside an acted-on family, carrying the rowid to edit.
    # CREW_OK as well as performed: N_SETUPS has to count the setups the family
    # was actually judged on, and a part whose every setup was run by the wrong
    # crew contributed no evidence and should not appear with a proposed value.
    # ON_CURRENT for the same reason: only setups run under the part's setup
    # in force were tested.
    members = (wo[~wo.SKIPPED & wo.CREW_OK & wo.ON_CURRENT]
               .merge(actions[UNIT + ["SETUP_HRS_SUGGESTED",
                                      "RATIO_OUTLIERS_REMOVED", "VERDICT"]],
                      on=UNIT)
               .groupby(UNIT + ["PART_ID"], dropna=False)
               .agg(MASTER_OPS_ROWID=("MASTER_OPS_ROWID", "first"),
                    SETUP_HRS_CURRENT=("SETUP_HRS_STORED", "max"),
                    RATIO=("RATIO_OUTLIERS_REMOVED", "first"),
                    N_SETUPS=("WORK_ORDER", "nunique"),
                    LAST_DATE=("LAST_DATE", "max"))
               .reset_index())
    # the family factor applied to each part's own stored value, so families
    # running several different SETUP_HRS keep the distinctions they intended
    members["SETUP_HRS_RECOMMENDED"] = members.SETUP_HRS_CURRENT * members.RATIO
    members["SETUP_HRS_RECOMMENDED"] = np.where(
        members.SETUP_HRS_RECOMMENDED >= 10,
        members.SETUP_HRS_RECOMMENDED.round(1),
        members.SETUP_HRS_RECOMMENDED.round(2))
    members["CHANGE_PCT"] = (
        (members.SETUP_HRS_RECOMMENDED / members.SETUP_HRS_CURRENT - 1)
        * 100).round(1)
    members["TARGET_COLUMN"] = "SETUP_HRS"
    members["ROUTE"] = "FAMILY"

    # Skipping is a counting question, so both run types belong here even
    # though only PCS/HR is testable. HRS/LOAD carries a setup standard on one
    # operation only, with three performed setups against a floor of six - no
    # verdict is possible, but the hours it reserves are just as real.
    skip = pd.concat([skip_rate(wo, "PCS/HR"), skip_rate(load_ls, "HRS/LOAD")],
                     ignore_index=True)
    skip = (skip[skip.WO_ALL >= MIN_SETUPS]
            .sort_values("STANDARD_CHARGED_SKIPPED", ascending=False))

    performed = wo[~wo.SKIPPED]
    crew = performed[performed.CREW_OK]
    real = crew[crew.ON_CURRENT]
    summary = pd.DataFrame([
        ("Scope", "PCS/HR setup, current FY, family level", np.nan),
        ("Setup work orders", len(wo), round(wo.HOURS_WORKED.sum(), 1)),
        ("  ...performed", int((~wo.SKIPPED).sum()),
         round(performed.HOURS_WORKED.sum(), 1)),
        (f"  ...skipped (<= {SKIP_CUT} h)", int(wo.SKIPPED.sum()),
         round(wo.loc[wo.SKIPPED, "HOURS_WORKED"].sum(), 1)),
        # Charged and counted, but not evidence: the crew that booked was not
        # the crew the routing plans for, so the standard does not describe the
        # job. Reported here rather than silently missing from the totals.
        ("  ...performed but crew <> routing (not tested)",
         int((~performed.CREW_OK).sum()),
         round(performed.loc[~performed.CREW_OK, "HOURS_WORKED"].sum(), 1)),
        # Crew-matched, but run under a SETUP_HRS the part has since replaced.
        ("  ...performed under a replaced setup value (not tested)",
         int((~crew.ON_CURRENT).sum()),
         round(crew.loc[~crew.ON_CURRENT, "HOURS_WORKED"].sum(), 1)),
        ("Family x sequence groups", len(res), np.nan),
        (f"  ...testable (n >= {MIN_SETUPS} tested)",
         int((res.WO_REAL >= MIN_SETUPS).sum()), np.nan),
        ("  ...statistically different", int(res.IS_SIGNIFICANT.sum()), np.nan),
        (f"  ...material (>={MIN_EFFECT:.0%} and >={MIN_HOURS_IMPACT}h)",
         len(actions), round(actions.HOURS_DELTA.sum(), 1)),
        ("Pooled ratio, tested setups only",
         round(real.HOURS_WORKED.sum() / real.HOURS_MASTER.sum(), 4), np.nan),
        ("Pooled ratio, all setup rows",
         round(wo.HOURS_WORKED.sum() / wo.HOURS_MASTER.sum(), 4), np.nan),
        ("Parts to edit", members.PART_ID.nunique(), np.nan),
        ("Standard charged for setups never performed", np.nan,
         round(wo.loc[wo.SKIPPED, "HOURS_MASTER"].sum()
               + load_ls.loc[load_ls.SKIPPED, "HOURS_MASTER"].sum(), 1)),
        ("  ...PCS/HR", int(wo.SKIPPED.sum()),
         round(wo.loc[wo.SKIPPED, "HOURS_MASTER"].sum(), 1)),
        # reported, never tested - one operation with a standard and three
        # performed setups all year, far below the floor
        ("  ...HRS/LOAD (reported, not testable)", int(load_ls.SKIPPED.sum()),
         round(load_ls.loc[load_ls.SKIPPED, "HOURS_MASTER"].sum(), 1)),
    ], columns=["METRIC", "VALUE", "HOURS"])

    with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="00_Summary", index=False)
        res.to_excel(xl, sheet_name="01_FamilyRollup", index=False)
        actions.to_excel(xl, sheet_name="02_SetupActions", index=False)
        skip.to_excel(xl, sheet_name="03_SkipRate", index=False)
        members.to_excel(xl, sheet_name="04_SetupUpdateList", index=False)
        (wo.sort_values(UNIT + ["PART_ID", "LAST_DATE"])
           .to_excel(xl, sheet_name="05_Evidence", index=False))

    print(f"\nfamily x sequence groups   : {len(res):,}")
    print(f"  testable                 : {(res.WO_REAL >= MIN_SETUPS).sum():,}")
    print(f"  actions                  : {len(actions):,}")
    print(f"  parts to edit            : {members.PART_ID.nunique():,}")
    print(f"  crew <> routing, not tested : "
          f"{int((~performed.CREW_OK).sum()):,} of {len(performed):,} performed")
    print(f"  replaced setup value, not tested : "
          f"{int((~crew.ON_CURRENT).sum()):,}")
    print(f"\npooled ratio, tested setups only   : "
          f"{real.HOURS_WORKED.sum() / real.HOURS_MASTER.sum():.3f}")
    print(f"pooled ratio, all rows             : "
          f"{wo.HOURS_WORKED.sum() / wo.HOURS_MASTER.sum():.3f}")
    pcs = wo.loc[wo.SKIPPED, "HOURS_MASTER"].sum()
    lsd = load_ls.loc[load_ls.SKIPPED, "HOURS_MASTER"].sum()
    print(f"\nstandard charged for setups never performed: {pcs + lsd:,.0f} h")
    print(f"  PCS/HR                                  : {pcs:,.0f} h "
          f"({wo.SKIPPED.sum():,} work orders)")
    print(f"  HRS/LOAD (reported, not testable)       : {lsd:,.0f} h "
          f"({load_ls.SKIPPED.sum():,} work orders)")
    print("\nverdicts:")
    print(res.VERDICT.value_counts().to_string())
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
