# Design notes

The decisions behind the analysis described in [README.md](README.md), and why
each was taken. The step-by-step walkthrough, with worked examples, is
[`fts_approach.ipynb`](fts_approach.ipynb).

The findings themselves, with the plant's own figures, are not published.

## Contents

- [Misstated hours, not net hours](#misstated-hours-not-net-hours)
- [Setup](#setup)
- [HRS/LOAD](#hrsload)
- [Semi-finished and finished parts are never pooled](#semi-finished-and-finished-parts-are-never-pooled)
- [Two shapes of operation](#two-shapes-of-operation)
- [Applying the results](#applying-the-results)
- [Scope](#scope)
- [Token bookings](#token-bookings)
- [Known issues in the extract](#known-issues-in-the-extract)
- [Running it, in detail](#running-it-in-detail)
- [Keeping the prose honest](#keeping-the-prose-honest)

## Misstated hours, not net hours

The headline is the hours of **misstated** standard time: the gap between hours
worked and hours allowed, summed per action **without its sign**. An operation
holding one part that runs long and a family that runs short counts both —
they are two wrong standards, not one small one. Netting would let them cancel.

The case the two-level design exists to find is exactly that: at a single
routing step, one part judged on its own runs over its standard while the rest
of its family, pooled, runs under theirs. Net, the step looks nearly right and
would barely be looked at. In fact both standards are wrong, in opposite
directions, and no single value of `RUN_MASTER` fixes both.

A standard is used per part: to quote it, to cost it, to schedule the resource
that makes it, and to judge whether the job ran well. Every one of those
decisions is made on one part's number, never on the sum. A part allowed 40%
too much time and another allowed 40% too little are two wrong standards, and
the factory pays for both — the first in work quoted too expensively and
capacity reserved that is never used, the second in jobs that quietly run late
and look like poor performance. They do not offset; they compound.

Net would matter only if the goal were total plant capacity for the year. It is
not. The goal is that each family and part carries a standard that describes it.

`00_Summary` carries both: `HOURS_DELTA_NET` sums the deltas signed, and
**`HOURS_DELTA_ABSOLUTE`** discards the signs first — the one to read.
Operations that produced no action are listed too, as *tested, holds* or
*nothing testable*. Without them, "we tested this and the standard holds" would
be indistinguishable from "we never got to it".

## Setup

Analysed separately, family level only, `PCS/HR` operations only, in
`03_SetupUpdateList`. Setup is not the run analysis with a different filter.
Four things differ:

- **`SETUP_HRS` is a duration, so the correction multiplies.** `RUN_MASTER` is
  a rate and divides. Reversing this yields plausible numbers wrong in both
  directions at once.
- **A setup booked at ~0 hours was not performed.** When several work orders of
  one family run back to back on a resource, only the first needs a full setup.
  That is normal. Pooling those tickets measures how often setup is skipped
  rather than how long one takes, and the two answers point opposite ways. They
  are excluded from the test at `<= 0.02 h`, a cut that sits in a sparse band of
  the booking distribution, so it is not sensitive to where exactly it falls.
- **Part level is hopeless.** Setup units carry only a few worked hours a year,
  so nothing clears a materiality gate alone. Setup is judged at family level,
  with an impact gate of 5 hours against 20 for run.
- **The crew has to match the routing.** `SETUP_HRS` is one elapsed value for
  the crew the routing plans for, so a setup booked by a different number of
  people is a different job and cannot say whether that value is right. Those
  setups are excluded from the test — see
  [Known issues](#known-issues-in-the-extract).

**Skip rate is reported separately** (`03_SkipRate` in `FTS_SETUP.xlsx`). The
ERP charges the setup standard on every work order whether or not a setup
happened, so the schedule reserves time for setups never performed. That is a
routing and scheduling question, not a standards correction, and it goes to
different people. Skipping is a counting question — it needs no test and no
minimum sample — so `03_SkipRate` is the only sheet covering both run types.

## HRS/LOAD

Operations where the master is not a rate but a **step function of quantity**:

```
HOURS_MASTER = CEILING(TOTAL_QTY / LOAD_SIZE_QTY) * RUN
```

`RUN` is hours per load. 1048 pieces against a load size of 500 needs three
machine cycles and is charged 3 × `RUN`, however full the last basket is.
Three things differ from `PCS/HR`:

- **The correction multiplies.** `RUN` is a duration per load, like `SETUP_HRS`.
- **`LOAD_SIZE_QTY` is taken as correct.** It is a physical fact about the
  resource and the part, and labour tickets contain no evidence about it — they
  measure elapsed time. Correcting it from timing data would be inventing a
  number rather than measuring one, so `RUN` is the only parameter moved.
- **Only single-load work orders are tested**, on `HOURS_MASTER == RUN_MASTER`.

That last one is the substantive choice. The model assumes the second basket
costs what the first did, and it does not: single-load jobs and multi-load jobs
sit on opposite sides of their standard, and fitting
`HOURS_WORKED = a + b × loads` puts the marginal load at a different share of
`RUN` on every operation. There is no single multiplier, so one `RUN` cannot fit
both regimes — fit it to everything pooled and single-load jobs that are
already correct get tightened while multi-load jobs stay loose.

Normalising both sides by the load count does **not** help: the ratio is already
dimensionless, so dividing numerator and denominator by the same number cancels
and every test statistic comes out identical. It is a shape problem, not a scale
problem. The multi-load gap is therefore reported rather than corrected
(`04_MultiLoadGap`): capacity reserved and not used, a routing question.

The filter is `HOURS_MASTER == RUN_MASTER` rather than
`TOTAL_QTY <= LOAD_SIZE_QTY`. The two cannot disagree: charging exactly one
load is the same statement as the quantity fitting one, and every extract so
far confirms it. The form used is the one that stays correct if the master
hours ever stop being derived from the quantity — `fts_checks.py` re-tests the
identity on every run.

## Semi-finished and finished parts are never pooled

A family holds both the semi-finished article and the finished one. At a
routing step they share, those are two different physical jobs — one part is
half made, the other is complete — so the part type is part of the family
pooling key in both the run and the setup analysis. It is read from the ERP
field, not inferred from the part number, because the numbering convention has
exceptions.

The cost is a few groups that were testable only when pooled: they go untested
rather than judged on a blend of two different jobs. The gain is that a
finished part never receives a correction measured entirely on semi-finished
ones.

The failure mode this prevents is not a wrong number but a hidden one. When the
two halves are wrong in opposite directions, the pooled ratio can land near 1.0
and report the group as clean — and a blend near 1.0 produces no row anywhere
for anyone to question.

## Two shapes of operation

Once every operation was in, a pattern showed: operations come in two shapes,
and the shape tells you what to expect. Either the standards were set properly
per part — pooled ratio near 1, and the work is finding the few that drifted —
or a single round number was stamped across everything, wrong for almost
everyone and usually in the same direction. The second kind carries larger
errors and is cheaper to fix. Checking whether `RUN_MASTER` is uniform within a
family tells you which you have before reading a single result.

The direction tends to track the kind of work, too. Whether a standard that
runs generous means the standard is wrong or the work is being rushed is not
something this analysis can answer.

## Applying the results

**Three execution sheets, one review sheet.** They are split by the field they
write to and the arithmetic behind them — merging would need a nullable column
per target and would invite keying a duration into a rate. `01_Actions` is where
they come back together, with `RUN_TYPE` telling `PCS/HR` (a rate, correction
divides) from `HRS/LOAD` (hours per load, correction multiplies).

**`02_RunUpdateList`** is the sheet to work from: one row per part, carrying the
exact `RUN_MASTER` to apply and the `MASTER_OPS_ROWID` to apply it to.

| Column | |
|---|---|
| **`RM_RECOMMENDED`** | **the value to key in** |
| `RECOMMENDED_BASIS` | why that value won |
| `MASTER_OPS_ROWID` | the master routing row to update — one per part+sequence |
| `REVIEW_FLAG` | `NEEDS REVIEW` where `RATIO` falls outside 0.25–4.0 |
| `RM_SUGGESTED` / `RM_SUGGESTED_IF_WHOLE_FAMILY` | the two candidates it chose between |
| `RATIO`, `CI95_*_PER_WO`, `N_WO_EVIDENCE` | the evidence, for overriding by eye |

**`RATIO` and the interval are two different estimators**, so they can
disagree. `RATIO` is pooled and hours-weighted (`ΣHOURS_WORKED / ΣHOURS_MASTER`)
and drives the correction; the interval is the t-interval on `mean(ln ratio)`,
the geometric mean of the *per work order* ratios, unweighted. When large and
small jobs behave differently the two part company. The interval is only ever
used as a test — does it exclude 1? — never as error bars on `RATIO`.

**The review flag is on the ratio, not on the percentage change**, because the
change is asymmetric. `RUN_MASTER` is a rate and the correction divides, so a
job running 4× faster reads **+300%** while a job running 4× slower reads
**−75%** — same size of error, and a decrease can never pass −100% however
extreme it gets. A ±300% threshold would catch every fast row and, by
construction, no slow one. Testing the ratio makes the two sides symmetric,
because 4 and ¼ sit the same distance from 1 in log space. Flagged rows are
kept rather than suppressed: an extreme ratio is a real signal that something
is wrong, and deciding whether it is the standard or the booking is a judgement
the analysis cannot make.

**A part judged on its own evidence keeps its own value.** If a part clears all
six rules alone, `RM_RECOMMENDED` is its own measured figure and the family
never overrides it. The family value covers only the parts that could not be
judged alone — and those parts are removed from the family pool *before* the
family ratio is computed, so the two routes never both act on the same part.

Family-route parts are corrected by applying the family *factor* to each part's
own `RUN_MASTER`, so families running several different standards keep the
distinctions the routing intended and every row carries a concrete number.

Suggested rates are rounded to one decimal at 10 and above, two below. A flat
one decimal distorts the small values badly — 0.74 becomes 0.70, a 5.4% shift,
which eats half the 10% materiality threshold the row had to clear.

`03_SetupUpdateList` is the equivalent for setup: same `MASTER_OPS_ROWID`, but
**`SETUP_HRS_RECOMMENDED` goes in a different field**, and `TARGET_COLUMN` says
which. A routing line can appear in both lists, with one edit each.

## Scope

In: run operations, current fiscal year, routings rebuilt in the master-data
overhaul (4-digit operation sequences).

**Why these totals differ from the operational performance dashboard.** They
will never match, and the difference is a scope decision rather than an error
in either. The two answer different questions.

| | Operational dashboard | This analysis |
|---|---|---|
| Operation status | `R` and `C` — includes work in progress | `C` only; a half-finished operation's hours are not comparable to a whole standard |
| Operation sequence | all | **4 digits only** — post-overhaul routings |
| Setup with no ticket | imputed, `SETUP_HRS` added with 0 h worked | not imputed; skip rate reported separately in `03_SkipRate` |
| Upper date bound | yesterday | yesterday — `TRANSACTION_DATE < CAST(GETDATE() AS DATE)` |

**The sequence filter is usually the whole difference**, and it is the one that
looks most like data being thrown away. It is not. The overhaul renumbered
routing steps from 2 and 3 digits to 4 as it rebuilt them, so a shorter
sequence is a step on a routing that no longer exists. There is no
`RUN_MASTER` at sequence 20 to correct, because sequence 20 is gone. Those
hours are real work and belong on a performance dashboard; they are inert on a
list of values to edit. Checked on one operation within one family, the
cutover is clean: the same parts carried across, with a short overlap and
nothing booked on the old routing afterwards. This query re-checks it against
the database:

```sql
SELECT LEN(CAST(LT.OPERATION_SEQ_NO AS VARCHAR(10))) AS seq_len,
       COUNT(DISTINCT P.ID) AS parts,
       COUNT(DISTINCT LT.WORKORDER_BASE_ID) AS work_orders,
       MIN(LT.TRANSACTION_DATE) AS first_seen,
       MAX(LT.TRANSACTION_DATE) AS last_seen,
       SUM(ISNULL(LT.HOURS_WORKED, 0)) AS hours_worked
FROM dbo.LABOR_TICKET LT
JOIN dbo.WORK_ORDER WO ON WO.BASE_ID = LT.WORKORDER_BASE_ID
                      AND WO.LOT_ID = LT.WORKORDER_LOT_ID
                      AND WO.SUB_ID = LT.WORKORDER_SUB_ID
                      AND WO.SPLIT_ID = LT.WORKORDER_SPLIT_ID
JOIN dbo.OPERATION O ON O.WORKORDER_BASE_ID = LT.WORKORDER_BASE_ID
                    AND O.WORKORDER_LOT_ID = LT.WORKORDER_LOT_ID
                    AND O.WORKORDER_SPLIT_ID = LT.WORKORDER_SPLIT_ID
                    AND O.WORKORDER_SUB_ID = LT.WORKORDER_SUB_ID
                    AND LT.OPERATION_SEQ_NO = O.SEQUENCE_NO
JOIN dbo.PART P ON WO.PART_ID = P.ID
WHERE O.OPERATION_TYPE = '<operation>'
  AND LT.TYPE = 'R' AND O.STATUS = 'C'
  AND LT.TRANSACTION_DATE >= '<fiscal year start>'
  AND WO.BASE_ID LIKE '%OP%'
  AND (SELECT STRING_VAL FROM dbo.USER_DEF_FIELDS
       WHERE DOCUMENT_ID = P.ID AND ID = 'UDF-0000026') = '<family>'
GROUP BY LEN(CAST(LT.OPERATION_SEQ_NO AS VARCHAR(10)))
```

**What would change the conclusion:** short-sequence work orders still being
opened. If `last_seen` on a 2- or 3-digit row ever moves forward, the old
routings are back in use and the filter needs revisiting.

**Both date bounds are fixed.** The upper bound excludes today, whose bookings
are still arriving, so a re-run within the same day is reproducible. The lower
bound is hard-coded rather than derived from `GETDATE()`: a derived bound moved
on 1 October while the analysis scripts' fiscal-year start did not, which would
have reported on a few weeks of data believing it had a year. Both ends change
together, and the review app warns if the extract starts long after the fiscal
year does.

Deliberately excluded, each for a reason:

- **Setup tickets** — excluded from the *run* pipeline, since `SETUP_HRS` is a
  duration and the correction inverts. Analysed separately — see
  [Setup](#setup).
- **`HRS/LOAD`** — excluded from the *run* pipeline because the standard is a
  step function of quantity. Analysed separately — see [HRS/LOAD](#hrsload).
- **Rework operations** — hours worked against essentially zero standard.
  Nothing to test against.
- **Steps with no standard (`RUN_MASTER = 0`)** — **the zero is a decision, not
  a gap.** Whole operations sit at zero across every part and family:
  warehousing, document control, picking and dispatch, work nobody set out to
  time. Suggesting a standard where somebody decided there should be none would
  be inventing policy, so these are reported nowhere and corrected nowhere.
  What is left after that reasoning is genuinely patchy and far below the
  materiality gate.
- **Steps no longer in the routing** — `MASTER_OPS_ROWID` comes back empty when
  a later revision deleted the step, *or* when the slot now holds a different
  operation. A routing edit overwrites the line in place and keeps its ROWID,
  so the master join matches on operation as well as sequence. Either way the
  work order measures a process no longer in force, and with no master routing
  line there is nothing an update could target. The exclusion fails safe,
  doing nothing rather than something wrong.
- **Prior fiscal years** — standards changed in the overhaul.

## Token bookings

Some labour tickets carry a flat **0.01 h whatever the quantity** — one piece or
five hundred, the same hundredth of an hour. That is an entry made to close the
step, not a measurement, and there is no duration inside it to correct.

They are **dropped before testing**, at `<= 0.02 h`, in all three run analyses
(`TOKEN_HOURS`). The setup analysis does the same — there the identical booking
means a setup that was never performed — so this is one rule applied
consistently.

**Why exclude rather than trim.** Every other filter here touches the estimate
and leaves the test alone, deliberately: trimming first and testing after
manufactures significance. This one is the exception, and it needs a different
argument to earn that. A booking that recorded no duration is not an extreme
measurement of job time — it is the absence of one, so it is not an observation
of the quantity under test at all. The rule also never looks at the outcome:
the threshold is on `HOURS_WORKED` alone, blind to the standard, to the ratio
and to which direction a correction would go, so it cannot be tuned toward an
answer.

**Where the cut sits.** There is no clean gap to aim at — under-recording is a
continuum, and the median ratio climbs smoothly toward 1 as the booked hours
rise. `0.02` is the conservative end of the defensible range: below it the
bookings claim a few percent of the allowance and the same flat value appears
against hundreds of pieces; a little above it a low ratio could be a genuinely
fast job, and the cut deliberately does not reach there. It barely matters
which value in that range is chosen: almost no family pool changes testability
across it.

**They defeated the outlier filter rather than being caught by it.** MAD
trimming removes points *far from the centre*, and where the tokens are the
majority they **are** the centre — at which point the filter runs in reverse.
On one inspection step almost every work order was a token booking; the median
settled near zero, the filter discarded the only two work orders that measured
anything, and the test read what was left as a perfectly consistent signal,
suggesting a standard hundreds of times the current one.

Nothing is capped or imputed. A booking that recorded no time says nothing
about how long the job takes, so any substitute value would be invented.
Dropping them hands the decision to the `MIN_WO` floor: a step either still has
six real work orders or it leaves as *insufficient data*.

**What this does not fix.** The guard removes bad *suggestions*; it does not
make the underlying step visible. A step booked at a hundredth of an hour is
not a step running fast — it is a step nobody is recording, and its standard
may be badly wrong with no way to tell from this data. `07_TokenBookings` is
the record of exactly that, a shop-floor booking question that goes to
different people, the same way `03_SkipRate` does for setup.

## Known issues in the extract

**Dividing `SETUP_HRS` by `USER_5` is de-duplication, not a unit conversion.**
`SETUP_HRS` is a single elapsed value on the master routing line — 0.5 means
half an hour of setup, whoever does it. The division exists because the inner
query emits **one row per labour ticket**, so a crew of three repeats the master
value three times before the outer `SUM` collects it. Dividing first cancels
that:

```
HOURS_MASTER = SETUP_HRS × (tickets / USER_5)
```

which lands on the true `SETUP_HRS` exactly when the ticket count matches the
planned crew. The rounding proves it: crew-3 rows mostly show `HOURS_MASTER` =
**0.5001**, not 0.5000, because `0.5 / 3 = 0.16667` at `NUMERIC(19,4)`, summed
three times. A genuine 1.5 divided by 3 would give exactly 0.5000. Do not remove
the division: without it every crewed setup is multiplied by its crew size.

**Not quite one row per ticket, so the setup test does not read it.** The inner
query also groups by transaction date and department, so tickets booked on the
same date by people of one department merge into one row and the master is
credited once for all of them. The extract therefore carries
`SETUP_HRS_MASTER`, the work order's own `SETUP_HRS` read straight from its
operation like `RUN_MASTER`, and every setup is judged against that. The
ticket-built `HOURS_MASTER` is kept beside it for comparison only.

**The crew that books is not always the crew the routing plans for.** It fails
in both directions — a crew of three where two booked, and a one-person
standard booked by two people. The extract carries `EMP_DISTINCT` and `LT_ROWS`
to measure it; they are aggregates, so the extract keeps the same grain. They
are counted from the labour tickets directly: carried up through the grouped
inner query, two people booking in one group collapsed into one, and the counts
came out low while the hours stayed right.

**Setups by a crew other than the one planned are excluded from the setup
test, not corrected.** The arithmetic *could* be inverted, but a one-person,
one-hour setup performed by two people is not a fast or slow execution of that
plan, it is a different job. Correcting it would make the number look
trustworthy without making it meaningful. They are still charged, still
counted, and still reported in the skip figures; they simply are not evidence
about the standard. `00_Summary` reports the excluded count on its own line.

**The setup in force.** The value the family factor multiplies is the
`SETUP_HRS_MASTER` of the part's latest performed work order — the same rule as
on the run analyses. Setups run under a value the part has since replaced are
left out of the test and counted on their own line.

**`SETUP_HRS` is barely a standard.** A handful of round values cover almost
every setup row, and most sections put nearly all their rows on a single value.
Testing against that measures distance from a placeholder, not whether a
standard is right.

## Running it, in detail

**`new_extract.py`** — the order is validate, archive, promote, run, and only
the promotion destroys anything. It refuses outright if the incoming workbook
will not open, has no rows, or is missing a column the current one has; if the
archive comes back incomplete it rolls the half-generation back and stops
before promoting. A new `.sql` is optional, a new `.xlsx` is not: a new query
with no new extract is refused, because `fts.sql` would then describe a query
that did not produce `fts.parquet`. If the incoming columns differ and no
`.sql` was supplied, it says so loudly.

**`run_all.py`** — rebuilds the cache, runs every operation, then setup, then
`HRS/LOAD`, then the update list, re-executes the notebook, looks for orphaned
workbooks and runs `fts_checks.py`. It refuses to start when a new extract is
waiting in front of a cache that was never archived, because the rebuild would
discard it. After a rebuild, `archive/scope_changes.md` says what moved between
the two extracts; rows that kept their place but **lost** `MASTER_OPS_ROWID`
are a master routing rebuilt in the ERP, which changes results without
changing a row count.

**One operation by hand:**

```bash
python fts_analysis.py    "<operation>"
python fts_family_view.py "<operation>"
python fts_setup_view.py            # all operations at once
python fts_load_view.py             # HRS/LOAD, all operations at once
python fts_update_list.py           # consolidates all three - run it last, always
```

**Only `fts_analysis.py` rebuilds the cache.** The other three read the parquet
with no freshness check, so running any of them first against a new extract
silently analyses the *previous* one — no error, no warning, plausible numbers.
Half of why `run_all.py` exists is to make that ordering impossible to get
wrong.

Re-execute the notebook through `nbclient` rather than the `jupyter execute`
CLI — the CLI reads the file with the Windows locale encoding and silently
mojibakes every non-ASCII character it writes back:

```bash
python -c "import nbformat; from nbclient import NotebookClient; import pathlib; p=pathlib.Path('fts_approach.ipynb'); d=nbformat.reads(p.read_text(encoding='utf-8'), as_version=4); NotebookClient(d, timeout=900, kernel_name='python3').execute(); p.write_text(nbformat.writes(d)+'\n', encoding='utf-8')"
```

## Keeping the prose honest

Some figures in the findings notes are counterfactuals — "what the numbers
would have been under a different rule" — which the workbooks never record. They
were computed once by hand, and prose does not recompute. `fts_checks.py`
recomputes every one of them on each run and **claims** it: the exact string as
the prose spells it must still appear in the notes, or the run ends with exit 2
naming it. With `--fix` it rewrites a drifted number in place and still reports
it, so a person reads the before and after: replacing a number is safe while it
moves proportionally and unsafe when the number *is* the claim.

The notes with the figures live in the private folder beside this repository,
not here. In a clone without it, the claims are skipped rather than failed.
