#!/usr/bin/env python3
"""Fetch ALL issues for a Linear team with REAL per-state entry timestamps via
the GraphQL IssueHistory connection, and emit a Nave-ready CSV.

This pulls the actual `createdAt` of every state transition, so each stage
column holds the true date the issue first entered that stage.

Auth: reads LINEAR_API_KEY from the environment. Personal API keys go in the
Authorization header verbatim (NO "Bearer " prefix).

Team: reads LINEAR_TEAM_KEY from the environment (required; the issue-id prefix,
e.g. "WOS").

Usage:
    LINEAR_TEAM_KEY=WOS python3 backfill.py            # -> "WanderOS issues (history backfill).csv"
    LINEAR_TEAM_KEY=WOS python3 backfill.py out.csv    # custom output path
"""
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error

API_URL = "https://api.linear.app/graphql"
# Which Linear team to pull, from $LINEAR_TEAM_KEY (required). This is the team
# KEY (the issue-id prefix, e.g. "WOS"), not the display name.
TEAM_KEY = os.environ.get("LINEAR_TEAM_KEY")

# ---- Output shape (matches the existing formatted CSVs exactly) ----
PIPELINE = ["Triage", "Backlog", "Todo", "In Progress", "In Review", "Reviewed", "Staged", "Done"]
TERMINAL = ["Canceled", "Duplicate"]
STATUS_COLS = PIPELINE + TERMINAL
OUT_HEADER = ["Id", "Name"] + STATUS_COLS + [
    "Blocked Date Range", "Due Date", "Task URL", "Labels", "Cycle", "Issue Type", "Resolution",
]

# Map each real WOS workflow state -> the output column it feeds. Each pipeline
# state has its own column (including "Reviewed", between In Review and Staged).
STATE_TO_COLUMN = {
    "Triage": "Triage",
    "Backlog": "Backlog",
    "Todo": "Todo",
    "In Progress": "In Progress",
    "In Review": "In Review",
    "Reviewed": "Reviewed",
    "Staged": "Staged",
    "Done": "Done",
    "Canceled": "Canceled",
    "Duplicate": "Duplicate",
}

ISSUES_QUERY = """
query Issues($after: String, $teamKey: String!) {
  issues(
    first: 25
    after: $after
    includeArchived: true
    filter: { team: { key: { eq: $teamKey } } }
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      title
      createdAt
      dueDate
      cycle { number name }
      parent { id }
      state { name type }
      labels(first: 20) { nodes { name } }
      history(first: 50) {
        pageInfo { hasNextPage endCursor }
        nodes {
          createdAt
          fromState { name }
          toState { name }
        }
      }
    }
  }
}
"""

HISTORY_QUERY = """
query IssueHistory($id: String!, $after: String) {
  issue(id: $id) {
    history(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        createdAt
        fromState { name }
        toState { name }
      }
    }
  }
}
"""


def gql(query, variables):
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        sys.exit("ERROR: LINEAR_API_KEY is not set. Add it to ~/.zshrc and retry.")
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": key},
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
            if "errors" in data:
                raise RuntimeError(json.dumps(data["errors"]))
            return data["data"]
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited -> back off using reset header if present
                wait = int(e.headers.get("Retry-After", 2 ** attempt))
                print(f"  rate limited, sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("gave up after repeated rate limits")


def fmt_date(iso):
    """ISO 8601 -> dd/mm/yyyy (empty in -> empty out)."""
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso else ""


def fmt_labels(names):
    return "[" + "|".join(names) + "]" if names else ""


def fmt_cycle(cycle):
    """Linear cycle -> team-facing label. Prefer the name (e.g. "Cycle 19");
    fall back to "Cycle <number>" if a cycle is unnamed; empty if the issue has
    no cycle. (Linear's `number` is an internal index that diverges from the
    display name, so the name is what the team actually recognizes.)"""
    if not cycle:
        return ""
    name = (cycle.get("name") or "").strip()
    if name:
        return name
    num = cycle.get("number")
    return f"Cycle {num}" if num is not None else ""


def first_entry_per_state(issue):
    """Return {state_name: earliest ISO createdAt} of when the issue entered it.

    Initial state (at creation) is derived from the earliest history entry's
    fromState (or the current state if there's no history) and stamped with the
    issue's createdAt.
    """
    hist = sorted(issue["history"]["nodes"], key=lambda h: h["createdAt"])
    # Keep only entries that actually change state: the rest (assignee, label,
    # etc.) have toState == null, and Linear also records no-op same-state
    # "transitions" (fromState == toState, e.g. a Done -> Done touch) which must
    # be dropped or they'd inflate the most-recent-entry date below.
    changes = [
        h for h in hist
        if h.get("toState")
        and (h.get("fromState") or {}).get("name") != h["toState"]["name"]
    ]
    entered = {}

    # No recorded state changes -> the issue was created directly into its
    # current state and never moved.
    if not changes:
        entered[issue["state"]["name"]] = issue["createdAt"]
        return entered

    # If the first transition came FROM a real state, the issue was created in
    # that state (we're seeing it leave). If the first transition's fromState is
    # null, that transition IS the creation event (None -> X), so X is the
    # creation state and the loop below stamps it -- do NOT fall back to the
    # current state here (that stamps a terminal state at creation time).
    first_from = changes[0].get("fromState")
    if first_from:
        entered[first_from["name"]] = issue["createdAt"]

    for h in changes:
        name = h["toState"]["name"]
        if name not in entered:  # keep earliest (list is sorted ascending)
            entered[name] = h["createdAt"]

    # The card's CURRENT state must hold its MOST-RECENT entry, not the first.
    # Otherwise a reopened/moved-back card (e.g. Done -> In Progress, or
    # In Progress -> back to Todo) keeps an early first-touch date on its current
    # stage while a previously-visited stage has a later date -- which makes Nave
    # place the card in the wrong column. The last transition into the current
    # state is the most recent change overall, so this stays >= every other date.
    current = issue["state"]["name"]
    into_current = [h["createdAt"] for h in changes if h["toState"]["name"] == current]
    if into_current:
        entered[current] = max(into_current)

    return entered


def stage_dates(issue):
    """Map real state-entry timestamps onto the output columns with cumulative
    carry-forward so Nave sees monotonic, non-decreasing stage dates.

    Placement follows the issue's CURRENT Linear state: the pipeline is filled
    only up to the column the issue is in right now, and any further stage it
    once visited but was sent back from (e.g. a brief In Review touch before
    bouncing to Todo) is dropped. Terminal cards (Canceled/Duplicate) keep the
    full pipeline journey they actually completed before exiting."""
    entered = first_entry_per_state(issue)

    # earliest entry date per OUTPUT column (several states may map to one column)
    col_date = {c: "" for c in STATUS_COLS}
    for state, iso in entered.items():
        col = STATE_TO_COLUMN.get(state)
        if not col:
            continue
        if not col_date[col] or iso < col_date[col]:
            col_date[col] = iso

    # Fill the pipeline up to the issue's CURRENT state. If the current state is
    # a pipeline column, that column's index is the cutoff -- stages beyond it
    # that were only briefly visited (then reverted) are not carried. If the
    # current state is terminal (Canceled/Duplicate, not in PIPELINE), keep the
    # furthest pipeline stage actually reached so the card's journey survives.
    current_col = STATE_TO_COLUMN.get(issue["state"]["name"])
    if current_col in PIPELINE:
        fill_to = PIPELINE.index(current_col)
    else:
        reached = [i for i, c in enumerate(PIPELINE) if col_date[c]]
        fill_to = max(reached) if reached else -1

    out = {c: "" for c in STATUS_COLS}
    last = ""
    for i in range(fill_to + 1):
        c = PIPELINE[i]
        if col_date[c]:
            last = col_date[c]
        out[c] = last  # carry forward through genuinely-skipped intermediate stages

    for t in TERMINAL:
        if col_date[t]:
            out[t] = col_date[t]

    return {c: fmt_date(v) for c, v in out.items()}


def issue_type(issue):
    labs = [l.lower() for l in (n["name"] for n in issue["labels"]["nodes"])]
    if "bug" in labs:
        return "Bug"
    if issue.get("parent"):
        return "Sub-task"
    return "Task"


def fetch_all_issues():
    after = None
    while True:
        data = gql(ISSUES_QUERY, {"after": after, "teamKey": TEAM_KEY})
        conn = data["issues"]
        for issue in conn["nodes"]:
            # page through history if an issue has >50 transitions
            hpage = issue["history"]["pageInfo"]
            while hpage["hasNextPage"]:
                more = gql(HISTORY_QUERY, {"id": issue["identifier"], "after": hpage["endCursor"]})
                hc = more["issue"]["history"]
                issue["history"]["nodes"].extend(hc["nodes"])
                hpage = hc["pageInfo"]
            yield issue
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]


def main():
    if not TEAM_KEY:
        sys.exit("ERROR: LINEAR_TEAM_KEY is not set.")
    out_path = sys.argv[1] if len(sys.argv) > 1 else "WanderOS issues (history backfill).csv"
    n = 0
    with open(out_path, "w", newline="") as fout:
        writer = csv.writer(fout)
        writer.writerow(OUT_HEADER)
        for issue in fetch_all_issues():
            sd = stage_dates(issue)
            writer.writerow(
                [issue["identifier"], issue["title"]]
                + [sd[c] for c in STATUS_COLS]
                + [
                    "",  # Blocked Date Range (no source data in history)
                    fmt_date(issue.get("dueDate")),
                    f"https://linear.app/wander/issue/{issue['identifier']}",
                    fmt_labels([x["name"] for x in issue["labels"]["nodes"]]),
                    fmt_cycle(issue.get("cycle")),
                    issue_type(issue),
                    issue["state"]["name"],  # Resolution = current Wander status
                ]
            )
            n += 1
            if n % 100 == 0:
                print(f"  {n} issues...", file=sys.stderr)
    print(f"{out_path}: {n} rows")


if __name__ == "__main__":
    main()
