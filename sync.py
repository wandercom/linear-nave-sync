#!/usr/bin/env python3
"""One-command Linear -> Nave sync (on demand).

Pulls a full snapshot of WanderOS (team WOS) issues from Linear, then uploads
the resulting CSV to a Nave dashboard and watches the job finish.

This is a thin orchestrator over two single-purpose scripts that each stay
independently runnable:
    backfill.py FILE.csv     pull from Linear  -> Nave-ready CSV
    upload.py   FILE.csv     push that CSV to Nave + poll the job

Env required:
    LINEAR_API_KEY        (used by backfill.py)
    LINEAR_TEAM_KEY       (used by backfill.py; team to pull, e.g. "WOS")
    NAVE_TOKEN            (used by upload.py)
    NAVE_DASHBOARD_ID     (used by upload.py; or pass --dashboard-id)

By default this is a FULL REPLACE: each run mirrors Linear onto the dashboard
(upload.py uses cumulative=false; Nave never deletes completed items).

Usage:
    python3 sync.py                       # pull -> push (full replace)
    python3 sync.py --wipe-out            # wipe ALL dashboard data first
    python3 sync.py --keep-csv out.csv    # write the snapshot to a named file
    python3 sync.py --dashboard-id ID     # override $NAVE_DASHBOARD_ID
"""
import argparse
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BACKFILL = os.path.join(HERE, "backfill.py")
UPLOAD = os.path.join(HERE, "upload.py")


def run(label, cmd):
    print(f"\n=== {label} ===\n$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"\nABORT: {label} failed (exit {result.returncode}); dashboard not touched.")


def main():
    ap = argparse.ArgumentParser(description="Pull WOS issues from Linear and push to Nave.")
    ap.add_argument("--dashboard-id", default=None, help="Nave dashboard id (default: $NAVE_DASHBOARD_ID)")
    ap.add_argument("--keep-csv", metavar="PATH", default=None,
                    help="write the snapshot CSV here and keep it (default: a temp file, deleted after)")
    ap.add_argument("--wipe-out", action="store_true",
                    help="wipe ALL dashboard data (to-do/WIP/done) before loading")
    ap.add_argument("--cumulative", action="store_true",
                    help="merge into existing data instead of full replace")
    ap.add_argument("--no-poll", action="store_true",
                    help="don't poll the Nave job; print the status URL and exit")
    args = ap.parse_args()

    # Fail fast on missing config before spending a full Linear pull.
    for var in ("LINEAR_API_KEY", "LINEAR_TEAM_KEY", "NAVE_TOKEN"):
        if not os.environ.get(var):
            sys.exit(f"ERROR: {var} is not set.")
    if not args.dashboard_id and not os.environ.get("NAVE_DASHBOARD_ID"):
        sys.exit("ERROR: NAVE_DASHBOARD_ID is not set (or pass --dashboard-id).")

    if args.keep_csv:
        csv_path, cleanup = args.keep_csv, False
    else:
        fd, csv_path = tempfile.mkstemp(prefix="nave-sync-", suffix=".csv", dir=HERE)
        os.close(fd)
        cleanup = True

    try:
        run("PULL from Linear", [sys.executable, BACKFILL, csv_path])

        upload_cmd = [sys.executable, UPLOAD, csv_path]
        if args.dashboard_id:
            upload_cmd += ["--dashboard-id", args.dashboard_id]
        if args.wipe_out:
            upload_cmd.append("--wipe-out")
        if args.cumulative:
            upload_cmd.append("--cumulative")
        if args.no_poll:
            upload_cmd.append("--no-poll")
        run("PUSH to Nave", upload_cmd)

        print("\nSync complete.")
    finally:
        if cleanup and os.path.exists(csv_path):
            os.remove(csv_path)


if __name__ == "__main__":
    main()
