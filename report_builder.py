"""
report_builder.py — turns saved Portal data into one interactive HTML report.

What it does
    1. Reads the audit export (.zip / .gz / .ndjson) and the usage JSON.
    2. Tidies each event into a simple "row" (time, group, user, file, size...).
    3. Pours rows + usage figures into templates/report.html.j2 and writes
       ONE self-contained HTML file (no internet needed to open it).

    Analogy: the API gave us raw ingredients; this file is the kitchen that
    plates them. The browser does the rest (filters, sorting, charts) with the
    JavaScript inside the HTML file.

How to run it on its own (step d test — no token, no API calls)
    python report_builder.py --events sample_data/audit_sample.ndjson ^
                             --usage  sample_data/usage_sample.json ^
                             --groups sample_data/groups_sample.json
    (on macOS/Linux use \\ instead of ^ at the line ends)

    Output: output/report_<org>_<from>_<to>.html  (run_report.py puts it in the run's own folder)
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from audit_export import file_size_bytes, read_events, read_export_range
from portal_client import build_usage_summary, group_name_map, parse_group_list

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "report.html.j2"
# Drop your logo files in assets/ (SVG best, PNG/JPG fine) — they're embedded in the report.
# A file with "dark" in its name is used in dark mode; any other one in light mode.
ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


# ---------------------------------------------------------------------- #
# Events -> rows
# ---------------------------------------------------------------------- #
def event_to_row(event: dict, group_names: dict) -> dict:
    """Flatten one audit event into the fields the report table uses."""
    ts = _parse_ts(event.get("timestamp"))
    group_id = str(event.get("group_id") or "")
    first = (event.get("user_first_name") or "").strip()
    last = (event.get("user_last_name") or "").strip()
    email = event.get("user_email") or ""

    return {
        # Milliseconds since 1970 — every browser understands this,
        # and it lets the viewer see times in THEIR local time zone.
        "ts": int(ts.timestamp() * 1000) if ts else None,
        "group_id": group_id,
        "group": group_names.get(group_id) or (f"Group {group_id[:8]}" if group_id else "—"),
        "user": f"{first} {last}".strip() or email or "Unknown user",
        "email": email,
        "action": event.get("action") or "",
        "type": event.get("resource_type") or "",
        "file": _display_name(event),
        "project": event.get("project_name") or "",
        "project_id": str(event.get("project_id") or ""),
        "package": event.get("package_name") or "",
        "package_id": str(event.get("package_id") or ""),
        "size": file_size_bytes(event),
        "outcome": (event.get("outcome") or "").upper(),
        "raw": event,  # full event, shown when a row is expanded
    }


def _display_name(event: dict) -> str:
    """
    Version uploads are named like 'pkg:rl/project/package@1.2.3'.
    Show the short 'package@1.2.3'; filestream files keep their file name.
    """
    name = event.get("resource_name") or ""
    if name.startswith("pkg:") and "/" in name:
        return name.rsplit("/", 1)[-1]
    return name or "—"


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def load_group_names(path: str | Path | None, usage_summary: dict) -> dict:
    """
    {group_id: name}. Uses the /list (groups) answer if we have it, plus any names
    already present in the usage response.
    """
    names = {}
    for g in usage_summary.get("groups_usage", []):
        if g.get("group_id") is not None and g.get("name") and not g["name"].startswith("Group "):
            names[str(g["group_id"])] = g["name"]
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        names.update(group_name_map(parse_group_list(data)))
    return names


# ---------------------------------------------------------------------- #
# Build + render
# ---------------------------------------------------------------------- #
def load_logos() -> dict:
    """
    {"light": data-URI or None, "dark": data-URI or None}.
    A data: URI puts the image inside the HTML, so the report stays one file.
    """
    found = {"light": None, "dark": None}
    if not ASSETS_DIR.is_dir():
        return found
    files = sorted(p for p in ASSETS_DIR.iterdir()
                   if p.suffix.lower() in LOGO_TYPES and p.stat().st_size < 1_000_000)
    for path in files:
        slot = "dark" if "dark" in path.stem.lower() else "light"
        if found[slot] is None:
            mime = LOGO_TYPES[path.suffix.lower()]
            found[slot] = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    # Only one logo given? Use it in both modes.
    found["light"] = found["light"] or found["dark"]
    found["dark"] = found["dark"] or found["light"]
    return found


def build_report_data(events: list[dict], usage_raw: dict, group_names: dict,
                      organization: str, export_range: tuple[str, str] | None = None,
                      exact_range: bool = False) -> dict:
    """
    export_range = (start, end) of the audit export as ISO text. It defines the
    report's date range (normally the last 90 days before the export).
    """
    usage = build_usage_summary(usage_raw)  # no client -> no API calls
    # Usage groups come with a name but no ID; audit events come with an ID
    # but no name. Link the two through the group list (ID -> name).
    name_to_id = {name: gid for gid, name in group_names.items()}
    for g in usage["groups_usage"]:
        if g.get("group_id") is None and g["name"] in name_to_id:
            g["group_id"] = name_to_id[g["name"]]
        elif g["name"].startswith("Group ") and str(g.get("group_id")) in group_names:
            g["name"] = group_names[str(g["group_id"])]

    rows = [event_to_row(e, group_names) for e in events]
    rows.sort(key=lambda r: r["ts"] or 0, reverse=True)
    stamps = [r["ts"] for r in rows if r["ts"]]

    # Report range = the export's range; fall back to the first/last event.
    start = _parse_ts(export_range[0]) if export_range else None
    end = _parse_ts(export_range[1]) if export_range else None
    range_start = int(start.timestamp() * 1000) if start else (min(stamps) if stamps else None)
    range_end = int(end.timestamp() * 1000) if end else (max(stamps) if stamps else None)
    if exact_range and end and range_end:
        # An exact range ends at midnight AFTER the --to day; step back 1 ms so the
        # report shows (and filters up to) the --to day itself, e.g. 09/30 not 10/01.
        range_end -= 1

    return {
        "organization": organization,
        "generated_at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "range_start": range_start,
        "range_end": range_end,
        "range_from_export": bool(start and end),
        "range_exact": bool(exact_range and start and end),   # picked with --from/--to
        "logo": load_logos(),
        "usage": usage,
        "rows": rows,
    }


def render_report(data: dict, out_path: str | Path) -> Path:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        # Autoescape = every value is treated as text, never as HTML/code.
        # Audit logs contain user-entered text, so this matters.
        autoescape=select_autoescape(["html", "j2"]),
    )
    html = env.get_template(TEMPLATE_NAME).render(data=data)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def default_report_name(data: dict) -> Path:
    def day(ms):
        # Local calendar dates, the same ones the report header shows.
        return datetime.fromtimestamp(ms / 1000).strftime("%Y%m%d") if ms else "none"
    safe_org = "".join(c if c.isalnum() or c in "-_" else "_" for c in data["organization"])
    return Path("output") / f"report_{safe_org}_{day(data['range_start'])}_{day(data['range_end'])}.html"


# ---------------------------------------------------------------------- #
# Run on its own
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Build the HTML report from saved files.")
    parser.add_argument("--events", required=True, help="audit export (.zip, .gz or .ndjson)")
    parser.add_argument("--usage", required=True, help="usage JSON saved by portal_client.py")
    parser.add_argument("--groups", help="optional groups JSON (group id -> name)")
    parser.add_argument("--org", default="Sample organization", help="name shown in the header")
    parser.add_argument("--out", help="output HTML path (default: output/report_<org>_<from>_<to>.html)")
    args = parser.parse_args()

    try:
        events = read_events(args.events)
        usage_raw = json.loads(Path(args.usage).read_text(encoding="utf-8"))
        names = load_group_names(args.groups, build_usage_summary(usage_raw))
    except (OSError, ValueError) as e:
        print(f"Could not read input files: {e}", file=sys.stderr)
        sys.exit(1)

    report = build_report_data(events, usage_raw, names, args.org, read_export_range(args.events))
    out = render_report(report, args.out or default_report_name(report))
    print(f"Report with {len(report['rows'])} uploads written to {out}")
