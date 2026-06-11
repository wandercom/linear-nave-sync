# linear-nave-sync

Mirror a [Linear](https://linear.app) team's issues onto a [Nave](https://getnave.com)
dashboard. Pulls every issue for a team from the Linear GraphQL API — with the
**real per-state entry timestamps** from each issue's history — converts them to
Nave's CSV import shape, and uploads the result to a Nave dashboard.

The board tracks each card's **current** Linear state: a card is placed at the
stage it is in right now, and a stage it once touched but was sent back from
(e.g. a brief In Review before bouncing to Todo) is dropped. Terminal cards
(Canceled / Duplicate) keep the full pipeline journey they completed before
exiting.

## Requirements

- **Python 3.8+** — standard library only, no `pip install` needed.
- A Linear API key and a Nave API token + dashboard id (see below).

## Setup

```bash
cp .env.example .env
# edit .env with your keys, then:
set -a && source .env && set +a
```

| Variable            | Required | Where to get it |
|---------------------|----------|-----------------|
| `LINEAR_API_KEY`    | yes      | Linear → Settings → Security & access → API. Used verbatim (no `Bearer` prefix). |
| `LINEAR_TEAM_KEY`   | yes      | The team **key** — the issue-id prefix, e.g. `WOS` for `WOS-1234`. |
| `NAVE_TOKEN`        | yes      | In Nave: dashboard → API → "Get Access Token". Used verbatim (no `Bearer` prefix). |
| `NAVE_DASHBOARD_ID` | yes\*    | The target Nave dashboard id. \*Optional if you pass `--dashboard-id`. |

## Usage

One command — pull from Linear, push to Nave, watch the job finish:

```bash
python3 sync.py
```

By default this is a **full replace**: each run mirrors Linear onto the dashboard
(`cumulative=false`). Nave never deletes completed items.

```bash
python3 sync.py --wipe-out            # wipe ALL dashboard data first
python3 sync.py --cumulative          # merge into existing data instead of replacing
python3 sync.py --keep-csv out.csv    # keep the generated snapshot at a path
python3 sync.py --dashboard-id ID     # override $NAVE_DASHBOARD_ID
python3 sync.py --no-poll             # fire the upload and exit without polling
```

### The two stages, runnable on their own

`sync.py` is a thin orchestrator over two single-purpose scripts:

```bash
python3 backfill.py snapshot.csv      # Linear  -> Nave-ready CSV
python3 upload.py   snapshot.csv      # push that CSV to Nave + poll the job
```

`upload.py` supports the same `--wipe-out` / `--cumulative` / `--no-poll` /
`--dashboard-id` flags.

## How the stage mapping works

`backfill.py` reads each issue's full state-transition history and records the date
it entered every workflow stage. The output CSV has one column per stage, plus
terminal columns for any `canceled`/`duplicate`-type states. A stage the card
genuinely skipped (no recorded entry, but it sits between two stages the card did
enter) inherits the **next** real stage's date — the card passed that point on its
way there — so Nave sees a monotonic, non-decreasing progression and places the
card in the right column. Leading stages the card never reached stay empty, and
stages beyond the current state — visited once but reverted from — are not filled,
so the card lands where Linear says it is now.

**The columns are derived per team automatically.** On each run `backfill.py`
fetches the team's workflow states from Linear and builds the pipeline from them,
mirroring the board's own layout: states are ordered by type
(`triage → backlog → unstarted → started → completed`) and then by `position`
within a type, with `canceled`/`duplicate` states becoming terminal columns. So
any team works with no code edits — point `LINEAR_TEAM_KEY` at it and run. For
example, `WOS` yields `… In Progress, In Review, Reviewed, Staged, Done` while
`SNC` yields `… In Progress, For Review, In Review, Reviewed, Done`.

## Notes

- Generated CSVs and any Linear exports are git-ignored; this repo holds only code.
- Large pulls page through the Linear API and back off on rate limits automatically.
