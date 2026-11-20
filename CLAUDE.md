# factory_time_statistics

FTS: statistical validation of the routings' time standards against the labour actually booked,
with suggested corrections.

## Shared rules - read first

The rules every factory project follows (databases, SQL, data and people, ERP
facts, how to answer, git) are in
`~\GitHub\factory_shared_private\RULES.md`, imported here:

@~/GitHub/factory_shared_private/RULES.md

If that import did not load, read the file before anything else. What follows
is specific to this project.

## This project

- Read first: `README.md`, `DESIGN.md`, `fts.sql`, `fts_approach.ipynb`; open points in the private `OPEN_QUESTIONS.md`.
- The figures (findings with the plant's numbers) live only in the private `FINDINGS.md`, which
  `fts_checks.py` checks and fixes; README, DESIGN, docstrings and the notebook text carry none.
  The repository is public: keep it that way (since 2026-11-20).
- Private twin: `~\GitHub\factory_time_statistics_private\` (data, outputs, notes that
  name real parts; never a git repository).
- Python: conda env `fts` (`%LOCALAPPDATA%\miniconda3\envs\fts\python.exe`), built from
  `requirements.txt`; `requirements-lock.txt` pins what it was proven on.
- Saved memory: `~\.claude\projects\<encoded path of the repo>-factory-time-statistics\memory\`.
