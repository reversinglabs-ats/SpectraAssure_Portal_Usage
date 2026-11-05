"""
audit_export.py — gets the quota audit log out of the Portal.

What it does (think of ordering at a bakery)
    1. Place the order   POST /audit-log/{org}/export       (last 90 days, quota actions, NDJSON)
    2. Check if ready    GET  /audit-log/{org}/export/{id}/status  every 10 s (202 = still baking)
    3. Pick it up        download the file and save it UNCHANGED in output/raw/

    The Portal's file is a parcel in a box in an envelope:
        .zip  ->  audit_log_ndjson.gz  ->  NDJSON (one JSON event per line)
    read_events() opens all the layers for you.

How to run it on its own (step c test)
    python audit_export.py                  # last 90 days
    python audit_export.py --days 30        # shorter range
    python audit_export.py --dry-run        # show the request, send nothing
    python audit_export.py --read output/raw/<file>.zip   # just open a saved file

Who can run it
    Only Organization administrators can export audit logs.
"""

from __future__ import annotations

import gzip
import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config_loader import Config
from portal_client import AUDIT_EXPORT_PATH, AUDIT_STATUS_PATH, PortalClient, PortalError

QUOTA_ACTIONS = [
    "version.upload_quota.spent",          # package version uploaded
    "filestream_file.upload_quota.spent",  # file uploaded to a filestream
]
MAX_DAYS = 365                # Portal API limit per export
DEFAULT_DAYS = 90             # what a normal run exports (--days changes it)
POLL_EVERY_SECONDS = 10
DEFAULT_TIMEOUT_SECONDS = 600  # give up after 10 minutes
RAW_DIR = Path("output/raw")


# ---------------------------------------------------------------------- #
# 1. Build the request
# ---------------------------------------------------------------------- #
def build_export_request(days: int = DEFAULT_DAYS, now: datetime | None = None,
                         all_actions: bool = False,
                         start: datetime | None = None, end: datetime | None = None,
                         exact_range: bool = False) -> dict:
    """
    The JSON body for POST /audit-log/{organization}/export.

    Normally: the last `days` days, quota actions only.
    all_actions=True drops the action filter (used to find out which actions exist).
    start/end (UTC) replace `days` when given.
    exact_range=True adds "_range" to the export name, so a later rebuild
    (--from-file) knows the period was picked with --from/--to.
    """
    if start is None or end is None:
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(
                f"--days must be between 1 and {MAX_DAYS}. The Portal API exports at most "
                f"{MAX_DAYS} days per request."
            )
        end = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        start = end - timedelta(days=days)
    if end <= start:
        raise ValueError("The end date must be after the start date.")
    if (end - start).days > MAX_DAYS:
        raise ValueError(f"The Portal API exports at most {MAX_DAYS} days per request.")
    filters = [] if all_actions else [{"filter_by": "action", "operation": "is_any", "values": QUOTA_ACTIONS}]
    return {
        "name": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ("_all_actions" if all_actions else "")
                + ("_range" if exact_range else ""),
        "datetime_start": _iso(start),
        "datetime_end": _iso(end),
        "filters": filters,
        "should_create_cef": False,
        "should_create_ndjson": True,
    }


def parse_us_date(text: str) -> datetime:
    """'09/01/2026' -> 2026-09-01 00:00 UTC."""
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"'{text}' is not a date in MM/DD/YYYY format.") from None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------- #
# 2 + 3. Start, wait, download
# ---------------------------------------------------------------------- #
def run_export(client: PortalClient, body: dict,
               timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, job_id: str | None = None,
               name_suffix: str = "", raw_dir: Path = RAW_DIR) -> Path:
    """
    Start the export, wait until it's ready, download it into raw_dir. Returns the saved file path.
    With job_id, skip starting a new export and pick up an existing one instead.
    """
    if job_id:
        export_id = job_id
        print(f"  Using existing export (id {export_id}) — not starting a new one.")
    else:
        try:
            started = client.start_audit_export(body)
        except PortalError as e:
            if e.status == 403:
                raise PortalError(403, "Your account needs the Organization administrator role "
                                       "to export audit logs.") from None
            raise
        export_id = started.get("export_id") or started.get("id") or started.get("job_id")
        if not export_id:
            raise PortalError(None, f"the Portal did not return an export ID (got: {list(started)})")
        print(f"  Export started (id {export_id}). Waiting for the Portal to prepare it...")

    deadline = time.monotonic() + timeout_seconds
    status_file = raw_dir / "audit_status_last.json"
    raw_dir.mkdir(parents=True, exist_ok=True)
    first = True
    while True:
        http_status, info = client.get_audit_export_status(export_id)
        # Keep the latest answer on disk — handy if the Portal's field names surprise us.
        status_file.write_text(json.dumps(getattr(client, "last_status_body", info), indent=2),
                               encoding="utf-8")

        status = str(find_value(info, STATUS_KEYS) or "").upper()
        link = find_value(info, LINK_KEYS) or find_url({k: v for k, v in info.items() if "cef" not in k}
                                                        if isinstance(info, dict) else info)
        if first:
            print(f"    status answer fields: {', '.join(map(str, info)) if isinstance(info, dict) else type(info).__name__}")
            first = False

        if status in FAILED_STATES:
            raise PortalError(http_status, f"the Portal reported the export as {status}. "
                                           "Try again, or try a shorter --days range.")
        if http_status == 200 and link and status not in WORKING_STATES:
            break

        if time.monotonic() > deadline:
            raise PortalError(None, f"export still not ready after {timeout_seconds // 60} min. "
                                    f"Last status answer saved to {status_file}. (export id {export_id})")
        print(f"    status: {status or '(none given)'} · HTTP {http_status} · "
              f"{'link ready' if link else 'no download link yet'} — checking again in {POLL_EVERY_SECONDS}s")
        time.sleep(POLL_EVERY_SECONDS)

    print("  Export ready. Downloading from the presigned link...")
    data = client.download(link)

    start = body["datetime_start"][:10].replace("-", "")
    end = body["datetime_end"][:10].replace("-", "")
    out = raw_dir / f"audit_{client.org}_{start}_{end}{name_suffix}{_guess_extension(data)}"
    out.write_bytes(data)  # saved exactly as received

    # Also unpack it: zip -> audit_log_ndjson.gz -> plain NDJSON next to the zip.
    try:
        ndjson_path = out.with_name(f"audit_{client.org}_{start}_{end}{name_suffix}.ndjson")
        if ndjson_path != out:
            ndjson_path.write_text(_unwrap(data), encoding="utf-8")
            print(f"  Extracted NDJSON to {ndjson_path}")
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        print(f"  Note: saved the download, but could not extract it ({e}).")
    return out


# Field names the status answer might use. The first one found wins.
# Confirmed Portal answer: {id, name, ndjson_status: "DONE", ndjson_presigned_url, ndjson_expires_at,
#                          cef_status, cef_presigned_url, cef_expires_at}
STATUS_KEYS = ("ndjson_status", "status", "state", "job_status", "export_status")
LINK_KEYS = ("ndjson_presigned_url", "download_link", "download_url", "presigned_url", "url", "link",
             "file_url")
WORKING_STATES = {"PENDING", "IN_PROGRESS", "RUNNING", "QUEUED", "PROCESSING", "STARTED", "CREATED"}
FAILED_STATES = {"FAILED", "ERROR", "CANCELLED", "CANCELED", "EXPIRED"}


def find_value(obj, keys):
    """Look for the first of `keys` anywhere in a (possibly nested) JSON answer."""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v not in (None, "", [], {}) and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            found = find_value(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_value(v, keys)
            if found:
                return found
    return None


def find_url(obj):
    """Fallback: the first http(s) link anywhere in the answer."""
    if isinstance(obj, str):
        return obj if obj.startswith(("https://", "http://")) else None
    values = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    for v in values:
        found = find_url(v)
        if found:
            return found
    return None


def _guess_extension(data: bytes) -> str:
    """Name the file by what it really is, using its first bytes ("magic numbers")."""
    if data[:2] == b"PK":
        return ".ndjson.zip"
    if data[:2] == b"\x1f\x8b":
        return ".ndjson.gz"
    return ".ndjson"


# ---------------------------------------------------------------------- #
# Reading a saved export (used by the report too)
# ---------------------------------------------------------------------- #
def read_events(path: str | Path) -> list[dict]:
    """
    Open .zip / .gz / plain .ndjson and return a list of event dicts.
    Blank or broken lines are skipped (and counted) instead of crashing.
    """
    raw = Path(path).read_bytes()
    text = _unwrap(raw)

    events, bad = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(obj, dict):
            events.append(obj)
    if bad:
        print(f"  Note: skipped {bad} line(s) that were not valid JSON.")
    return events


def _unwrap(raw: bytes) -> str:
    """Peel off zip / gzip layers until we reach the NDJSON text."""
    if raw[:2] == b"PK":  # zip
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = [n for n in zf.namelist() if "ndjson" in n.lower()]
            if not members:
                raise ValueError(f"no NDJSON file inside the zip (found: {zf.namelist()})")
            return _unwrap(zf.read(members[0]))
    if raw[:2] == b"\x1f\x8b":  # gzip
        return _unwrap(gzip.decompress(raw))
    return raw.decode("utf-8-sig")


def read_export_range(path: str | Path) -> tuple[str, str] | None:
    """
    The export's start/end, read from the description.txt the Portal puts in
    the zip (lines like "start = 2025-10-06 15:46:13+00:00"). None if unknown.
    """
    try:
        raw = Path(path).read_bytes()
        if raw[:2] != b"PK":
            return None
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith("description.txt")), None)
            if not name:
                return None
            text = zf.read(name).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile):
        return None
    found = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() in ("start", "end"):
            found[key.strip().lower()] = value.strip().replace(" ", "T", 1)
    return (found["start"], found["end"]) if "start" in found and "end" in found else None


def is_exact_range_export(path: str | Path) -> bool:
    """True if the export was made with --from/--to (its "Job name" in description.txt ends with "_range")."""
    try:
        raw = Path(path).read_bytes()
        if raw[:2] != b"PK":
            return False
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith("description.txt")), None)
            text = zf.read(name).decode("utf-8", "replace") if name else ""
    except (OSError, zipfile.BadZipFile):
        return False
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "job name":
            return value.strip().endswith("_range")
    return False


def file_size_bytes(event: dict) -> int:
    """The Portal sends the size as text ("1661239"); turn it into a number."""
    try:
        return int((event.get("context") or {}).get("file_size_bytes") or 0)
    except (TypeError, ValueError):
        return 0


def print_action_summary(events: list[dict]) -> None:
    """One line per action: how many events, and the total file size where events carry one."""
    rows = {}
    for e in events:
        r = rows.setdefault(e.get("action") or "(none)", {"n": 0, "sized": 0, "bytes": 0})
        r["n"] += 1
        size = (e.get("context") or {}).get("file_size_bytes")
        if size not in (None, ""):
            r["sized"] += 1
            r["bytes"] += file_size_bytes(e)
    gb = 1024 ** 3
    print(f"\n  {'action':55} {'events':>7} {'with size':>9} {'total size':>12}")
    for action, r in sorted(rows.items(), key=lambda kv: (-kv[1]["bytes"], -kv[1]["n"])):
        mark = "  <- quota action in the report" if action in QUOTA_ACTIONS else ""
        print(f"  {action:55} {r['n']:7} {r['sized']:9} {r['bytes'] / gb:10.2f} GB{mark}")


def print_summary(events: list[dict]) -> None:
    total = sum(file_size_bytes(e) for e in events)
    users = {e.get("user_email") or e.get("user_id") for e in events}
    stamps = sorted(e.get("timestamp", "") for e in events if e.get("timestamp"))
    print(f"  Events:      {len(events)}")
    gb = 1024 ** 3  # the Portal counts 1 GB as 1,073,741,824 bytes
    size = f"{total / gb:,.2f} GB" if total >= gb else f"{total / 1024 ** 2:,.2f} MB"
    print(f"  Total size:  {size}")
    print(f"  Users:       {len(users)}")
    if stamps:
        print(f"  First/last:  {stamps[0][:19]}  →  {stamps[-1][:19]}  (UTC)")


# ---------------------------------------------------------------------- #
# Run on its own: python audit_export.py
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import sys

    from config_loader import ConfigError, load_config

    parser = argparse.ArgumentParser(description="Export quota audit events (step c test).")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="how many days back (default 90, max 365)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help="seconds to wait for the export (default 600)")
    parser.add_argument("--dry-run", action="store_true", help="show the request, send nothing")
    parser.add_argument("--read", metavar="FILE", help="only read an already-downloaded export")
    parser.add_argument("--all-actions", action="store_true",
                        help="export EVERY action (no action filter) — to see which actions exist")
    parser.add_argument("--from", dest="date_from", metavar="MM/DD/YYYY", help="start date (instead of --days)")
    parser.add_argument("--to", dest="date_to", metavar="MM/DD/YYYY", help="end date, included")
    parser.add_argument("--by-action", action="store_true", help="with --read: totals per action")
    args = parser.parse_args()

    if args.read:
        try:
            evs = read_events(args.read)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            print(f"Could not read {args.read}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Read {args.read}")
        print_summary(evs)
        if args.by_action:
            print_action_summary(evs)
        sys.exit(0)

    try:
        start = end = None
        if args.date_from or args.date_to:
            if not (args.date_from and args.date_to):
                raise ValueError("Use --from and --to together.")
            start = parse_us_date(args.date_from)
            end = parse_us_date(args.date_to) + timedelta(days=1)   # include the whole --to day
        request_body = build_export_request(args.days, all_actions=args.all_actions, start=start, end=end)
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    try:
        cfg: Config = load_config(args.config)
    except ConfigError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("Dry run — this call WOULD be made:")
        print(f"  POST {cfg.base_url}{AUDIT_EXPORT_PATH.format(organization=cfg.organization)}")
        print("  " + json.dumps(request_body, indent=2).replace("\n", "\n  "))
        print(f"  then GET {cfg.base_url}{AUDIT_STATUS_PATH.format(organization=cfg.organization, export_id='<export_id>')} every {POLL_EVERY_SECONDS}s,"
              " then download the file")
        sys.exit(0)

    client = PortalClient(cfg)
    try:
        what = "ALL audit events" if args.all_actions else "quota events"
        print(f"Exporting {what} for '{cfg.organization}' "
              f"({request_body['datetime_start'][:16]} → {request_body['datetime_end'][:16]} UTC)")
        saved = run_export(client, request_body, args.timeout,
                           name_suffix="_all_actions" if args.all_actions else "")
        print(f"  Saved to {saved}")
        evs = read_events(saved)
        print_summary(evs)
        if args.all_actions:
            print_action_summary(evs)
    except PortalError as e:
        print(f"Portal error: {e}", file=sys.stderr)
        sys.exit(1)
