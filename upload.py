#!/usr/bin/env python3
"""Upload a CSV to a Nave (getnave.com) dashboard and watch the job finish.

Pushes one file to the Nave file-upload endpoint, then polls the pulling-status
URL it returns until the job reports done (or fails / times out).

Endpoint + payload follow Nave's "How to Use the API to Update Your Dashboard":
    POST https://file.getnave.com/api/dashboards/update
    Authorization: <token>          (verbatim, NO "Bearer " prefix)
    multipart form: dashboardId, cumulative, wipeOut, data=@file.csv

Auth/config: reads NAVE_TOKEN and NAVE_DASHBOARD_ID from the environment
(same pattern as backfill.py's LINEAR_API_KEY).

Defaults give a FULL REPLACE: cumulative=false (clears current WIP and loads the
file fresh; Nave never deletes completed items), wipeOut=false.

Usage:
    python3 upload.py FILE.csv                # full replace into NAVE_DASHBOARD_ID
    python3 upload.py FILE.csv --wipe-out     # wipe ALL data first (to-do/WIP/done)
    python3 upload.py FILE.csv --cumulative   # merge into existing data instead
    python3 upload.py FILE.csv --no-poll      # fire the upload, print status URL, exit
    python3 upload.py FILE.csv --dashboard-id ID   # override env dashboard id
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

UPDATE_URL = "https://file.getnave.com/api/dashboards/update"

# Polling. The file we push is small (<1 MB), so a short interval is fine; the
# docs recommend 5-10 min for LARGE files and a 10 min back-off on HTTP 429.
POLL_INTERVAL_S = 30
POLL_MAX_WAIT_S = 15 * 60
RATE_LIMIT_BACKOFF_S = 10 * 60

# The pulling-status response is JSON shaped like:
#   {"running":true,"completed":false,"failed":false,"state":"active",...}
# so we read those booleans directly. Substring markers are only a fallback for
# a non-JSON body. We always print the raw body for visibility.
DONE_MARKERS = ("completed", "complete", "finished", "success", "processed")
FAIL_MARKERS = ("failed", "errored", "error")


def _require_env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: {name} is not set. Export it and retry "
                 f"(get the token from Nave via 'Get Access Token').")
    return val


def _multipart_body(fields, file_field, filename, file_bytes):
    """Build a multipart/form-data body. Returns (content_type, body_bytes)."""
    boundary = f"----navepipeline{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts = []
    for name, value in fields.items():
        parts += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            str(value).encode(),
        ]
    parts += [
        f"--{boundary}".encode(),
        (f'Content-Disposition: form-data; name="{file_field}"; '
         f'filename="{filename}"').encode(),
        b"Content-Type: text/csv",
        b"",
        file_bytes,
        f"--{boundary}--".encode(),
        b"",
    ]
    body = crlf.join(parts)
    return f"multipart/form-data; boundary={boundary}", body


def _read_response(resp):
    raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return None, raw


def upload(csv_path, dashboard_id, token, cumulative, wipe_out):
    """POST the CSV to Nave. Returns the parsed JSON (or raw-text fallback)."""
    with open(csv_path, "rb") as f:
        file_bytes = f.read()
    filename = os.path.basename(csv_path)
    print(f"Uploading {filename} ({len(file_bytes):,} bytes) -> dashboard {dashboard_id}")
    print(f"  cumulative={str(cumulative).lower()} wipeOut={str(wipe_out).lower()}")

    content_type, body = _multipart_body(
        {
            "dashboardId": dashboard_id,
            "cumulative": str(cumulative).lower(),
            "wipeOut": str(wipe_out).lower(),
        },
        "data", filename, file_bytes,
    )
    req = urllib.request.Request(
        UPDATE_URL, data=body, method="POST",
        headers={"Authorization": token, "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            parsed, raw = _read_response(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"ERROR: upload failed (HTTP {e.code}): {detail or e.reason}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: could not reach Nave: {e.reason}")

    print(f"  upload accepted: {raw.strip()[:500]}")
    return parsed, raw


def status_url_from(parsed, raw, dashboard_id):
    """Extract the pulling-status URL from the upload response, falling back to
    the documented URL shape if the body doesn't hand us one."""
    if isinstance(parsed, dict):
        for key in ("status", "pullingStatusUrl", "pullingstatus", "statusUrl", "url"):
            if isinstance(parsed.get(key), str) and parsed[key].startswith("http"):
                return parsed[key]
    if raw:
        stripped = raw.strip().strip('"')
        if stripped.startswith("http"):
            return stripped
    return f"https://file.getnave.com/api/dashboards/{dashboard_id}/pullingstatus"


def poll(status_url, token):
    """Poll the pulling-status URL until the job looks done/failed or we time out.
    Returns True on a detected success, False otherwise."""
    print(f"Polling job status: {status_url}")
    waited = 0
    while waited <= POLL_MAX_WAIT_S:
        req = urllib.request.Request(status_url, headers={"Authorization": token})
        try:
            with urllib.request.urlopen(req) as resp:
                parsed, raw = _read_response(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"  rate limited (429); backing off {RATE_LIMIT_BACKOFF_S}s")
                time.sleep(RATE_LIMIT_BACKOFF_S)
                waited += RATE_LIMIT_BACKOFF_S
                continue
            print(f"  status check HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
            return False
        except urllib.error.URLError as e:
            print(f"  status check unreachable: {e.reason}")
            return False

        body = raw.strip()
        print(f"  [{waited}s] {body[:300]}")

        if isinstance(parsed, dict):
            # Authoritative path: the documented response carries explicit
            # booleans. Check failure first, then completion; otherwise keep
            # polling while the job is running.
            if parsed.get("failed") is True:
                print("Job reported a failure.")
                return False
            if parsed.get("completed") is True:
                print("Job completed.")
                return True
        else:
            # Fallback only if the body isn't JSON (shape changed / error page).
            low = body.lower()
            if any(m in low for m in FAIL_MARKERS):
                print("Job reported a failure.")
                return False
            if any(m in low for m in DONE_MARKERS):
                print("Job completed.")
                return True

        time.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S

    print(f"Gave up waiting after {POLL_MAX_WAIT_S}s. Job may still be processing; "
          f"check the dashboard or the status URL above.")
    return False


def main():
    ap = argparse.ArgumentParser(description="Upload a CSV to a Nave dashboard.")
    ap.add_argument("csv", help="path to the CSV file to upload")
    ap.add_argument("--dashboard-id", default=None,
                    help="Nave dashboard id (default: $NAVE_DASHBOARD_ID)")
    ap.add_argument("--cumulative", action="store_true",
                    help="merge into existing data (cumulative=true) instead of full replace")
    ap.add_argument("--wipe-out", action="store_true",
                    help="wipe ALL dashboard data (to-do/WIP/done) before loading")
    ap.add_argument("--no-poll", action="store_true",
                    help="don't poll the job; just print the status URL and exit")
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        sys.exit(f"ERROR: no such file: {args.csv}")

    token = _require_env("NAVE_TOKEN")
    dashboard_id = args.dashboard_id or _require_env("NAVE_DASHBOARD_ID")

    parsed, raw = upload(
        args.csv, dashboard_id, token,
        cumulative=args.cumulative, wipe_out=args.wipe_out,
    )

    status_url = status_url_from(parsed, raw, dashboard_id)
    if args.no_poll:
        print(f"Status URL: {status_url}")
        return

    ok = poll(status_url, token)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
