"""
run_report.py — the one command that does everything.

What it does
    1. Reads config.toml
    2. Fetches organization usage + the group list       (GET /usage, GET /list)
    3. Exports the quota audit log for the last 90 days   (POST /audit-log/{org}/export -> poll -> download)
    4. Builds the HTML report and opens it in your browser

    Every run gets its own folder, so older reports and exports are kept:
        output/<org>_<YYYY-MM-DD_HH-MM-SS>/
            report_<org>_<from>_<to>.html   the report
            run_log.txt                     what this run printed
            raw/                            every API answer, saved untouched
    The raw files let you rebuild that report later without calling the API
    again (see --from-file). Nothing is ever deleted automatically.

How to run it
    python run_report.py                         # full run, last 90 days
    python run_report.py --days 30               # different range (1-365)
    python run_report.py --from 09/01/2026 --to 09/30/2026   # exact dates (--to included)
    python run_report.py --dry-run               # show what would be called, call nothing
    python run_report.py --job-id <id>           # reuse an export that already exists
    python run_report.py --from-file output/<run folder>/raw/audit_<...>.ndjson.zip
                                                 # rebuild that run's report, no API calls
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import textwrap
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from audit_export import (DEFAULT_DAYS, DEFAULT_TIMEOUT_SECONDS, MAX_DAYS, build_export_request,
                          is_exact_range_export, read_events, read_export_range, run_export)
from config_loader import ConfigError, load_config
from portal_client import (AUDIT_EXPORT_PATH, AUDIT_STATUS_PATH, GROUP_LIST_PATH, USAGE_PATH,
                           PortalClient, PortalError, build_usage_summary, group_name_map,
                           parse_group_list)
from report_builder import (build_report_data, default_report_name, load_group_names,
                            render_report)

OUTPUT_DIR = Path("output")
LOG_FILE = OUTPUT_DIR / "run_log.txt"   # always the latest run; a copy goes into the run's own folder
RUN_DIR: Path | None = None             # this run's folder, once known (see new_run_dir)
COPY_LOG = False                        # True for API runs: keep their log in the run folder


class Tee:
    """
    Print to the screen AND to output/run_log.txt, so a failed run leaves a
    note behind that can be shared for troubleshooting. (The token is never
    printed, so it never lands in the log.)
    """

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, text):
        self.stream.write(text)
        self.log.write(text)
        self.log.flush()

    def flush(self):
        self.stream.flush()


def newest(folder: Path, pattern: str) -> Path | None:
    """Most recently saved file in the folder matching the pattern."""
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def new_run_dir(org: str) -> Path:
    """
    A fresh folder for this run, e.g. output/Trial_2026-10-26_14-30-05.
    Like a new page in a logbook: earlier pages (runs) are never touched.
    The name starts with the organization and sorts by date and time.
    """
    safe_org = "".join(c if c.isalnum() or c in "-_" else "_" for c in org)
    base = OUTPUT_DIR / f"{safe_org}_{datetime.now():%Y-%m-%d_%H-%M-%S}"
    folder, n = base, 2
    while folder.exists():                  # two runs in the same second: add _2, _3, ...
        folder, n = base.with_name(f"{base.name}_{n}"), n + 1
    (folder / "raw").mkdir(parents=True)
    return folder


def org_from_filename(path: str) -> str | None:
    """'audit_Trial_20251021_20261021.ndjson.zip' -> 'Trial' (None if the name doesn't fit)."""
    m = re.match(r"audit_(.+?)_\d{8}_\d{8}", Path(path).name)
    return m.group(1) if m else None


def parse_local_date(text: str, option: str) -> datetime:
    """'09/01/2026' -> midnight at the start of that day in this computer's time zone."""
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").astimezone()
    except ValueError:
        raise ValueError(f"{option} '{text}' is not a date in MM/DD/YYYY format, e.g. {option} 09/01/2026") from None


def exact_range(date_from: str | None, date_to: str | None) -> tuple[datetime, datetime]:
    """
    --from / --to as (start, end). Both are local calendar days and --to is
    included: --from 09/01/2026 --to 09/30/2026 covers all of September.
    --to defaults to today; an end in the future is cut back to "now".
    """
    if not date_from:
        raise ValueError("--to needs --from as well, e.g. --from 09/01/2026 --to 09/30/2026")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = parse_local_date(date_from, "--from")
    end = parse_local_date(date_to, "--to") + timedelta(days=1) if date_to else now
    if start >= now:
        raise ValueError("--from can't be in the future.")
    end = min(end, now)
    if end <= start:
        raise ValueError("--to must be the same day as --from or later.")
    if end - start > timedelta(days=MAX_DAYS):
        today = datetime.now().date()
        oldest = today - timedelta(days=MAX_DAYS - 1)
        raise ValueError(f"The Portal API exports at most {MAX_DAYS} days at a time. Pick a shorter --from/--to "
                         f"range, e.g. --from {oldest:%m/%d/%Y} --to {today:%m/%d/%Y}.")
    return start, end


class HelpFormatter(argparse.HelpFormatter):
    """--help layout: never split words or file paths at a '-' (keeps the example path copyable)."""

    def _split_lines(self, text, width):
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False, break_long_words=False)


def main() -> int:
    global RUN_DIR, COPY_LOG  # set below, so the log can be copied into the run folder at the end
    # Help texts match the options table in README.md.
    p = argparse.ArgumentParser(
        description="Full run (python run_report.py): Creates the report for the last 90 days.",
        add_help=False,
        formatter_class=HelpFormatter,
    )
    p.add_argument("-h", "--help", action="help", help="List all available options")
    p.add_argument("--config", metavar="<CONFIG_PATH>", default="config.toml",
                   help="Specifies config file where to read required Portal parameters")
    p.add_argument("--days", metavar="<DAYS>", type=int, default=None,
                   help="Different period, e.g. --days 30 or --days 365 (min 1, max 365; default 90)")
    p.add_argument("--from", dest="date_from", metavar="<MM/DD/YYYY>",
                   help="Exact start date instead of --days, e.g. --from 09/01/2026")
    p.add_argument("--to", dest="date_to", metavar="<MM/DD/YYYY>",
                   help="Exact end date, included (default: today). Use with --from, e.g. "
                        "--from 09/01/2026 --to 09/30/2026 (max 365 days)")
    p.add_argument("--timeout", metavar="<TIMEOUT>", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                   help="Wait longer for the export (seconds; default 600)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show which public APIs would be called; calls nothing")
    p.add_argument("--job-id", metavar="<JOB_ID>",
                   help="Reuse an export that's already prepared and exists in the Portal "
                        "(skips starting a new one)")
    p.add_argument("--from-file", metavar="<FILE_PATH>",
                   help="Rebuild a run's report from its saved files with no API calls, e.g. --from-file "
                        r"output\Trial_2026-10-26_14-30-05\raw\audit_Trial_20260728_20261026.ndjson.zip. "
                        "The rebuilt report replaces the one in that run's folder")
    p.add_argument("--no-open", action="store_true",
                   help="Build the report without opening the browser automatically")
    args = p.parse_args()

    # ---------- Offline: rebuild from saved files ----------
    if args.from_file:
        # Everything comes from the same saved run: the organization is read from the
        # audit file name (audit_<org>_<from>_<to>...), the usage + group list are taken
        # from the same raw/ folder, and the rebuilt report goes back into that run's folder.
        raw_dir = Path(args.from_file).parent
        RUN_DIR = raw_dir.parent if raw_dir.name == "raw" else raw_dir
        org = org_from_filename(args.from_file)
        if not org:
            try:
                org = load_config(args.config).organization
            except ConfigError:
                org = "Unknown organization"
        usage_path = newest(raw_dir, f"usage_{org}_*.json")
        groups_path = newest(raw_dir, f"groups_{org}_*.json")
        if usage_path is None:
            print(f"No saved usage file for '{org}' in {raw_dir}. Run python run_report.py to fetch fresh data.",
                  file=sys.stderr)
            return 1
        print(f"Building from saved files (no API calls):\n  events: {args.from_file}\n"
              f"  usage:  {usage_path}\n  groups: {groups_path or '(none saved; groups show as IDs)'}")
        try:
            events = read_events(args.from_file)
            usage_raw = json.loads(usage_path.read_text(encoding="utf-8"))
            names = load_group_names(groups_path, build_usage_summary(usage_raw))
        except (OSError, ValueError) as e:
            print(f"Could not read input files: {e}", file=sys.stderr)
            return 1
        return finish(events, usage_raw, names, org, args.no_open, read_export_range(args.from_file), RUN_DIR,
                      is_exact_range_export(args.from_file))

    # ---------- Online: call the API ----------
    try:
        cfg = load_config(args.config)
        if args.date_from or args.date_to:
            if args.days is not None:
                raise ValueError("Use either --days or --from/--to, not both.")
            start, end = exact_range(args.date_from, args.date_to)
            export_body = build_export_request(start=start, end=end, exact_range=True)
        else:
            export_body = build_export_request(args.days if args.days is not None else DEFAULT_DAYS)
    except (ConfigError, ValueError) as e:
        print(f"Problem: {e}", file=sys.stderr)
        return 1

    print("Using config:")
    print(cfg.describe())

    if args.dry_run:
        print("\nDry run — these calls WOULD be made (nothing is sent):")
        print(f"  GET  {cfg.base_url}{USAGE_PATH.format(organization=cfg.organization)}")
        print(f"  GET  {cfg.base_url}{GROUP_LIST_PATH.format(organization=cfg.organization)}")
        print(f"  POST {cfg.base_url}{AUDIT_EXPORT_PATH.format(organization=cfg.organization)}")
        print("       " + json.dumps(export_body))
        print(f"  GET  {cfg.base_url}{AUDIT_STATUS_PATH.format(organization=cfg.organization, export_id='<export_id>')}   (every 10 s until ready)")
        print("  GET  <download_link>   (save to output/<org>_<date_time>/raw/, then build the report there)")
        return 0

    client = PortalClient(cfg)
    RUN_DIR = new_run_dir(cfg.organization)
    COPY_LOG = True   # (a --from-file rebuild never replaces the original run's log)
    raw_dir = RUN_DIR / "raw"
    print(f"\nThis run's folder: {RUN_DIR}")
    today = date.today().isoformat()
    try:
        print("\n[1/3] Fetching usage and groups...")
        usage_raw = client.get_org_usage()
        (raw_dir / f"usage_{cfg.organization}_{today}.json").write_text(
            json.dumps(usage_raw, indent=2), encoding="utf-8")
        print(f"  Usage OK — {len(usage_raw.get('groups_usage') or [])} groups in usage")

        # The group list turns group IDs (in audit events) into names.
        # If it fails we carry on — the report then shows short IDs instead.
        names = {}
        try:
            groups_raw = client.list_groups_raw()
            (raw_dir / f"groups_{cfg.organization}_{today}.json").write_text(
                json.dumps(groups_raw, indent=2), encoding="utf-8")
            names = group_name_map(parse_group_list(groups_raw))
            print(f"  Group list OK — {len(names)} groups with an ID and name")
        except PortalError as e:
            print(f"  Warning: group list failed ({e}). Continuing; groups will show as IDs.")

        print(f"\n[2/3] Exporting audit log ({export_body['datetime_start'][:10]} → "
              f"{export_body['datetime_end'][:10]})...")
        saved = run_export(client, export_body, args.timeout, job_id=args.job_id, raw_dir=raw_dir)
        print(f"  Saved to {saved}")
    except PortalError as e:
        print(f"\nPortal error: {e}", file=sys.stderr)
        return 1

    # The zip's description.txt has the real range (matters with --job-id); else use our request.
    export_range = read_export_range(saved) or (export_body["datetime_start"], export_body["datetime_end"])
    exact = is_exact_range_export(saved) or bool(args.date_from)
    return finish(read_events(saved), usage_raw, names, cfg.organization, args.no_open, export_range, RUN_DIR, exact)


def finish(events, usage_raw, names, org, no_open, export_range, run_dir: Path, exact: bool = False) -> int:
    print("\n[3/3] Building report...")
    data = build_report_data(events, usage_raw, names, org, export_range, exact_range=exact)
    out = render_report(data, run_dir / default_report_name(data).name)
    print(f"  {len(data['rows'])} uploads → {out}")
    if not no_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w", encoding="utf-8") as log:
        screen_out, screen_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = Tee(screen_out, log), Tee(screen_err, log)
        try:
            code = main()
        except Exception:  # unexpected bug: keep the details in the log, show a short note
            import traceback
            log.write("\n--- unexpected error ---\n" + traceback.format_exc())
            screen_err.write(f"\nUnexpected error — details saved to {LOG_FILE}\n")
            code = 2
        finally:
            sys.stdout, sys.stderr = screen_out, screen_err   # back to plain screen output before the log closes
    # Keep a copy of this run's log next to its report (also when the run failed).
    if COPY_LOG and RUN_DIR is not None and RUN_DIR.is_dir():
        try:
            shutil.copyfile(LOG_FILE, RUN_DIR / "run_log.txt")
        except OSError:
            pass
    sys.exit(code)
