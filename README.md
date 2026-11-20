# Factory time statistics: validating manufacturing time standards

#### -- Project Status: [Completed]

## Objective

Statistical validation of manufacturing time standards against actual recorded
labour, with suggested corrections.

Every routing step in the ERP carries a standard — how long the job *should*
take: a setup time, and a run rate or a run time per load. Reality is recorded
separately, in labour tickets. This project asks, for each part at each routing
step, whether the gap between the two is large enough and consistent enough that
the standard should be changed — and if so, what to change it to.

It is a hypothesis-testing problem rather than a reporting one. A single work
order proves nothing; jobs vary. What matters is whether the gap is
*systematic*.

The deliverable is one review workbook: every proposed change with the evidence
behind it, ready for manufacturing engineering to check and key into the ERP. A
companion app,
[factory_time_statistics_streamlit_app](https://github.com/mikjf/factory_time_statistics_streamlit_app),
lets project management, finance and supply chain read the proposals and the
work orders behind them.

### The idea in one paragraph

For a `PCS/HR` operation the standard is a rate, so the theoretical time is
`quantity / RUN_MASTER`. Divide the hours actually worked by that theoretical
time and you get a dimensionless **ratio** — above 1 the job took longer than
allowed, below 1 it was quicker. Because the ratio is dimensionless, a 60-piece
order and a 600-piece order are directly comparable, and so are two parts whose
standards are 6 and 90 pieces per hour. Take the logarithm so that "twice as
long" and "half as long" are equal distances from centre, and the null
hypothesis becomes clean: **the log-ratios should be centred on zero**. Test
that, correct for the fact that thousands of tests are running at once, apply a
materiality threshold so trivial-but-significant results are filtered out, and
convert whatever survives back into a suggested `RUN_MASTER`.

Full walkthrough, with worked examples and every design decision justified:
**[`fts_approach.ipynb`](fts_approach.ipynb)**.

### What this project demonstrates

- **Hypothesis testing at scale:** Wilcoxon signed-rank and t-tests on
  log-ratios, Benjamini–Hochberg false discovery rate over thousands of tests,
  and materiality gates so that significant is not mistaken for worth changing
- **A two-level design:** parts judged alone where they have the evidence, and
  thin parts pooled by family, with the two lists never overlapping
- **Three kinds of standard, three kinds of arithmetic:** a rate (the correction
  divides), a duration (it multiplies) and a step function of quantity, kept on
  separate sheets so a duration can never be keyed into a rate
- **Data quality handled explicitly:** token bookings, skipped setups, crews
  other than the planned one and standards that changed mid-year are excluded
  and reported, never silently corrected
- **A pipeline that guards itself:** a new extract is validated and the old one
  archived before anything is overwritten, stale workbooks are detected, and the
  figures quoted in the findings notes are recomputed on every run
- **A SQL extract** on Infor VISUAL (SQL Server): labour tickets joined to
  work orders, operations and the master routing

## Method summary

| Step | Choice | Why |
|---|---|---|
| Measurement | log of (worked / master) per work order | dimensionless, symmetric, comparable across order sizes and standards |
| Unit | part × operation sequence | the same operation name recurs at different sequences and is a different job each time |
| Not keyed on | work order revision | a revision is bumped for any edit anywhere in the routing, so it says nothing about *this* step — work orders spanning several revisions pool freely as long as the standard held still |
| Changed standards | test only work orders under the value currently in force; if too few, report as *watch* and suggest nothing | older ones describe a standard that no longer exists, and pooling them would correct the new value using evidence about the old one |
| Outliers | 3×MAD on the log-ratio, estimate only — never the test | trimming before testing would manufacture significance |
| Primary test | Wilcoxon signed-rank | rank-based, so one mis-clocked ticket cannot swing it |
| Secondary test | one-sample t-test on log-ratios | supplies the confidence interval; also drives the FDR, since Wilcoxon's discreteness floors out at small n |
| Many tests | Benjamini–Hochberg q-values | ~5% of flagged results expected false, rather than 5% of all tests |
| Materiality | ≥10% deviation and ≥20 hours impact | significance is not the same as worth changing |
| Two levels | part, and family with flagged parts removed | most parts are too thin to judge alone; the two lists never overlap |
| Which value wins | family, unless the part's CI excludes the family ratio | a part that cannot be distinguished from its siblings should not carry a different standard |

Setup times and `HRS/LOAD` operations follow the same idea with their own
arithmetic; [DESIGN.md](DESIGN.md) explains how they differ, and every other
decision in detail.

## Tech stack

- Python 3.12
- pandas, NumPy, SciPy
- pyarrow, openpyxl
- Jupyter Notebook and matplotlib (the walkthrough)
- SQL Server (T-SQL) on Infor VISUAL ERP

## Repository layout

```
fts.sql                  extract query (labour tickets joined to routings)
fts_analysis.py          run, part level     -> FTS_<OP>_PARTS.xlsx
fts_family_view.py       run, family level   -> FTS_<OP>_FAMILY.xlsx
fts_setup_view.py        setup, family level -> FTS_SETUP.xlsx
fts_load_view.py         HRS/LOAD run        -> FTS_RUN_HRS_LOAD.xlsx
fts_update_list.py       consolidates all    -> FTS_UPDATE_LIST.xlsx
fts_approach.ipynb       the method, in plain language, with live figures
new_extract.py           takes delivery of a new extract: validate, archive, promote, run
run_all.py               runs the whole pipeline over the extract in place
archive_current.py       banks the current generation before it is replaced
fts_checks.py            recomputes the figures the findings notes quote
```

The extract is **not committed** — see [Data](#data).

## Running it

A new extract goes in this folder as **`fts_new.xlsx`** (and the query as
**`fts_new.sql`** if it changed). Then:

```bash
python new_extract.py --dry-run
```

```bash
python new_extract.py
```

That validates the incoming workbook, archives the outgoing generation,
renames the `_new` files into place and runs the whole pipeline. Nothing is
moved by hand, and a bad export is caught while it is still just a file with a
funny name.

To re-run without a new extract, after changing an analysis script:

```bash
python run_all.py
```

A stale workbook looks completely normal and gives no indication of being out
of date, so after any change to the analysis scripts every operation is re-run,
never one by hand. [DESIGN.md](DESIGN.md#running-it-in-detail) has the
details: the guards, running one operation alone, and re-executing the notebook.

## The deliverable

`FTS_UPDATE_LIST.xlsx`:

| Sheet | |
|---|---|
| `00_Summary` | every operation analysed, run and setup, hours signed and sign-free |
| `01_Actions` | one row per decision, for review — all three targets |
| **`02_RunUpdateList`** | **the `PCS/HR` run list to execute**, one row per part |
| `03_SetupUpdateList` | the setup list to execute |
| `04_LoadUpdateList` | the `HRS/LOAD` run list to execute |
| `05_FamilyTotals` | the same actions by family rather than by operation |
| `06_FamilyOperation` | one level down — family × operation |
| `07_TokenBookings` | steps closed without a duration |

Every execution row carries the master routing row it applies to and the exact
value to key. Rows extreme enough to deserve a human look first are flagged
`NEEDS REVIEW`, and every row shows both the standard the jobs ran under and the
master as it stands today, so a change already made is marked rather than
keyed twice.

## What this cannot tell you

- **A difference is not a diagnosis.** A significant result says the standard
  does not match reality — not that the standard is wrong. Tooling, operator
  mix and process drift produce an identical signature. Output is a review list
  for manufacturing engineering, not an ERP feed.
- **Coverage is the binding limit.** Only a minority of units have enough work
  orders to test. This improves with time, not with tuning.
- **Labour-ticket quality is assumed, not audited.**
- **No trend modelling** — one ratio per unit for the whole period.
- **A standard set to observed performance is a description, not a target.**

## Data

The extract is real production data and is **not committed**. It contains part
numbers, customer-identifiable family names, work order numbers and how long
each job actually takes — commercially sensitive on both the customer and cost
sides. The findings, with the plant's own figures, are kept privately too: this
repository holds the method and the code.

To reproduce: run `fts.sql` against the ERP and save the result as
`fts.xlsx` in the project root. The scripts cache it to `fts.parquet` on
first run.
