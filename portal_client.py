"""
portal_client.py — talks to the Spectra Assure Portal API.

What it does
    Uses ReversingLabs' official Python SDK (spectra-assure-sdk) for the Portal
    calls, and adds what this tool needs on top: friendly error messages and
    plain Python dicts/lists back. The rest of the tool simply calls:
        client.get_org_usage()            -> GET  /usage/{organization}
        client.list_groups()              -> GET  /list/{organization}
        client.start_audit_export(body)   -> POST /audit-log/{organization}/export
        client.get_audit_export_status(id)-> GET  /audit-log/{organization}/export/{job_id}/status
        client.download(link)             -> fetch the finished export file

    Analogy: the SDK is the company's own courier service to the Portal —
    it knows the addresses, carries your badge (token) and waits politely
    when the Portal is busy. This file just translates its answers into
    plain language.

    build_usage_summary() then picks only the fields the report needs and
    fills in group names if the usage response is missing them.

How to run it on its own (step b test)
    python portal_client.py                 # fetch usage, print it, save JSON
    python portal_client.py --dry-run       # only show which calls would be made
    python portal_client.py --config other.toml

    The raw API answer is saved unchanged to output/raw/usage_<org>_<date>.json
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import requests
from spectra_assure_api_client import SpectraAssureApiOperations, SpectraAssureExceptions

from config_loader import Config

REQUEST_TIMEOUT_SECONDS = 30   # how long to wait for the Portal's answer
CONNECT_TIMEOUT_SECONDS = 10   # how long to wait for the connection itself
API_SUFFIX = "/api/public/v1"  # every Portal API address ends with this

# The SDK insists on a group name when it starts, even though every call this
# tool makes is organization-level (usage, group list, audit log). Tested with
# SDK 1.1.0: the group never appears in these four addresses, so any non-empty
# name works and users don't need a "group" line in config.toml.
SDK_GROUP_PLACEHOLDER = "Default"

# The SDK logs its own warnings (with the Portal's raw answer). Keep them quiet:
# this tool shows its own plain-language messages instead.
_sdk_log = logging.getLogger("spectra_assure_api_client")
_sdk_log.addHandler(logging.NullHandler())
_sdk_log.propagate = False

# Plain-language messages for the errors people actually hit.
FRIENDLY_ERRORS = {
    400: "the Portal rejected the request (bad parameters)",
    401: "token invalid or expired — create a new Personal Access Token",
    403: "no access / missing role for this action",
    404: "not found — check base_url / organization in config.toml, or the API path",
    429: "too many requests — rate limit reached, try again later",
}

# Fields from GET /usage/{organization} used in the report.
# (They sit inside the "organization_usage" section of the answer.)
ORG_USAGE_FIELDS = [
    "max_packages",
    "package_count",
    "total_capacity_bytes",          # includes promo (Flex) capacity
    "total_capacity_bytes_portal",
    "total_capacity_bytes_cli",
    "this_month_used_bytes_portal",
    "this_month_used_bytes_cli",
    "download_capacity_bytes",
    "downloaded_this_month_bytes",
    "promo_capacity_bytes",          # Flex
    "time_to_capacity_reset_seconds",
]
GROUP_USAGE_FIELDS = [
    "name",
    "projects_usage_bytes",
    "projects_usage_limit_bytes",    # None = no limit
    "filestream_usage_bytes",
    "filestream_usage_limit_bytes",  # None = no limit
]

GB = 1024 ** 3  # the Portal counts 1 GB as 1,073,741,824 bytes

# API paths (relative to base_url), used in messages and in --dry-run output.
# The SDK builds the real addresses itself; these match what it sends.
USAGE_PATH = "/usage/{organization}"
GROUP_LIST_PATH = "/list/{organization}"
AUDIT_EXPORT_PATH = "/audit-log/{organization}/export"
AUDIT_STATUS_PATH = "/audit-log/{organization}/export/{export_id}/status"


class PortalError(Exception):
    """A request to the Portal failed. The message is safe to show to users."""

    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(f"[{status}] {message}" if status else message)


def sdk_location(base_url: str) -> dict:
    """
    Turn config.toml's base_url into the SDK's host/server settings:
        https://trial.secure.software/api/public/v1       -> host="trial.secure.software"
        https://my.secure.software/demo/api/public/v1     -> host="my.secure.software", server="demo"
    The SDK then rebuilds exactly the same address.
    """
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    if parts.scheme != "https" or not parts.netloc or not path.endswith(API_SUFFIX):
        raise PortalError(None, f"base_url in config.toml must look like https://<portal-host>{API_SUFFIX} "
                                f"(got {base_url})")
    location = {"host": parts.netloc}
    instance = path[: -len(API_SUFFIX)].strip("/")
    if instance:
        location["server"] = instance
    return location


# ---------------------------------------------------------------------- #
# Download of the finished export (not part of the SDK's audit-log calls)
# ---------------------------------------------------------------------- #
class Response:
    """The few things we need from a download: status, headers and body."""

    def __init__(self, status_code: int, headers, content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return json.loads(self.content.decode("utf-8"))


# ssl.create_default_context() checks the HTTPS certificate against the
# operating system's trusted certificates; HTTPS_PROXY is honoured automatically.
_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()))


class CertificateError(ConnectionError):
    """The server's HTTPS certificate isn't trusted by this computer."""


def _send(method: str, url: str, headers: dict, secret_headers: dict | None = None,
          body: bytes | None = None, timeout: float | None = None) -> Response:
    """
    Send one HTTP request and return a Response for ANY status (2xx, 4xx, 5xx).
    secret_headers (the token) are never forwarded on a redirect to another server.
    """
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    for name, value in (secret_headers or {}).items():
        req.add_unredirected_header(name, value)
    try:
        with _OPENER.open(req, timeout=timeout or REQUEST_TIMEOUT_SECONDS) as resp:
            return Response(resp.status, resp.headers, resp.read())
    except urllib.error.HTTPError as e:      # the server answered, just not with 2xx
        with e:
            return Response(e.code, e.headers, e.read())
    except (socket.timeout, TimeoutError):
        raise TimeoutError from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise TimeoutError from None
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise CertificateError(str(e.reason)) from None
        raise ConnectionError(str(e.reason)) from None
    except OSError as e:                     # e.g. connection reset mid-download
        raise ConnectionError(str(e)) from None


class PortalClient:
    """
    One client = one logged-in "session" with the Portal, through the SDK.

    Analogy: the token is like a visitor badge — handed to the SDK once, then
    shown automatically at every door (request) of the Portal.
    """

    def __init__(self, config: Config, _sdk_options: dict | None = None):
        # _sdk_options is only for automated tests (e.g. a local fake Portal).
        self.base_url = config.base_url
        self.org = config.organization
        self._auth = {"Authorization": f"Bearer {config.token}"}   # only for a Portal-hosted download link
        try:
            self._api = SpectraAssureApiOperations(
                **sdk_location(config.base_url),
                organization=config.organization,
                group=SDK_GROUP_PLACEHOLDER,
                token=config.token,
                read_timeout=REQUEST_TIMEOUT_SECONDS,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                auto_adapt_to_throttle=True,   # on "too many requests", wait (Retry-After) and retry, up to 5 tries
                **(_sdk_options or {}),
            )
        except SpectraAssureExceptions as e:
            raise PortalError(None, f"could not set up the Spectra Assure SDK: {e}") from None

    def path(self, template: str, **values) -> str:
        """Fill in {organization} / {export_id} in one of the *_PATH templates above."""
        return template.format(organization=self.org, **values)

    # ------------------------------------------------------------------ #
    # One SDK call, with plain-language errors
    # ------------------------------------------------------------------ #
    def _call(self, path: str, operation, **kwargs) -> requests.Response:
        """
        Run one SDK operation. Returns the answer for any 2xx status (200, 201,
        202...). Raises PortalError with a friendly message for everything else.
        """
        try:
            resp = operation(**kwargs)
        except requests.exceptions.SSLError:
            raise PortalError(
                None, f"the HTTPS certificate of {self.base_url} is not trusted by this computer "
                      "(a company network that inspects HTTPS traffic can cause this — ask IT)"
            ) from None
        except requests.exceptions.Timeout:
            raise PortalError(
                None, f"no answer from the Portal within {REQUEST_TIMEOUT_SECONDS}s ({path})"
            ) from None
        except requests.exceptions.ConnectionError:
            raise PortalError(
                None, f"could not connect to {self.base_url} — check base_url and your network"
            ) from None
        except requests.exceptions.RequestException as e:
            raise PortalError(None, f"request to the Portal failed ({path}): {type(e).__name__}") from None
        except SpectraAssureExceptions as e:
            raise PortalError(None, f"the Spectra Assure SDK refused the request ({path}): {e}") from None

        if resp.ok:
            return resp
        raise PortalError(resp.status_code, _friendly_message(resp, path))

    def _get_json(self, path: str, operation, **kwargs):
        resp = self._call(path, operation, **kwargs)
        try:
            return resp.json()
        except ValueError:
            raise PortalError(resp.status_code, f"response from {path} was not JSON") from None

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #
    def get_org_usage(self) -> dict:
        """GET /usage/{organization} — capacity and usage for the whole org."""
        return self._get_json(self.path(USAGE_PATH), self._api.usage)

    def list_groups_raw(self):
        """GET /list/{organization} — the group list exactly as the Portal sends it."""
        return self._get_json(self.path(GROUP_LIST_PATH), self._api.list_groups)

    def list_groups(self) -> list[dict]:
        """The group list as a plain list of dicts (see parse_group_list)."""
        return parse_group_list(self.list_groups_raw())

    def group_names(self) -> dict:
        """{group_id: name} for every group. Audit events only carry group_id."""
        return group_name_map(self.list_groups())

    # --- Audit export (the "bakery order": place → check → pick up) ---
    def start_audit_export(self, body: dict) -> dict:
        """
        POST /audit-log/{organization}/export — asks the Portal to PREPARE a file.
        This is the only write-style call the tool makes; it changes no data.
        body = name, datetime_start, datetime_end, filters, should_create_cef/ndjson.
        """
        resp = self._call(self.path(AUDIT_EXPORT_PATH), self._api.audit_log_export, **body)
        try:
            return resp.json() if resp.content else {}
        except ValueError:
            return {}

    def get_audit_export_status(self, export_id: str) -> tuple[int, dict]:
        """
        GET /audit-log/{organization}/export/{job_id}/status — returns
        (HTTP status, info about our export). 202 = still working.
        The export ID works like a bakery ticket number: it says which order is ours.
        """
        resp = self._call(self.path(AUDIT_STATUS_PATH, export_id=export_id),
                          self._api.audit_log_status, job_id=export_id)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        self.last_status_body = body  # kept so a confusing answer can be saved for troubleshooting
        # This address is for ONE export already, so a plain object is ours as-is.
        if isinstance(body, dict) and not any(isinstance(v, list) for v in body.values()):
            return resp.status_code, body
        return resp.status_code, find_export(body, export_id) or (body if isinstance(body, dict) else {})

    def download(self, url: str) -> bytes:
        """
        Download a finished export.

        The Portal answers with a cloud storage link that already contains its
        own access key, so we fetch it WITHOUT our token — never hand your badge
        to a building that isn't ours. Only https:// links are accepted.
        """
        portal_hosted = url.startswith(self.base_url) or url.startswith("/")
        if url.startswith("/"):
            url = self.base_url + url
        if urlsplit(url).scheme != "https":   # only encrypted links, never file:// or plain http
            raise PortalError(None, "the export download link is not an https:// address")
        try:
            resp = _send("GET", url, {}, self._auth if portal_hosted else None,
                         timeout=REQUEST_TIMEOUT_SECONDS * 4)
        except CertificateError:
            raise PortalError(None, "could not download the export file (its HTTPS certificate is not trusted)") from None
        except (TimeoutError, ConnectionError):
            raise PortalError(None, "could not download the export file (network problem)") from None
        if not resp.ok:
            raise PortalError(resp.status_code, "could not download the export file "
                              "(the link may have expired — exports expire after 7 days)")
        return resp.content


# ---------------------------------------------------------------------- #
# Turning the raw API answer into what the report needs
# ---------------------------------------------------------------------- #
def build_usage_summary(raw_usage: dict, client: PortalClient | None = None) -> dict:
    """
    Keep only the fields the report uses.

    The org figures live under "organization_usage"; groups under
    "groups_usage". If a group has no name, call GET /list once and fill
    names in by group ID. If that isn't possible, show the ID instead.
    """
    org = raw_usage.get("organization_usage")
    if not isinstance(org, dict):
        org = raw_usage  # older/flat shape — tolerate it
    summary = {field: org.get(field) for field in ORG_USAGE_FIELDS}

    groups_raw = raw_usage.get("groups_usage") or []
    missing_names = any(not g.get("name") for g in groups_raw)

    id_to_name: dict = {}
    if missing_names and client is not None:
        try:
            id_to_name = client.group_names()
        except PortalError as e:
            print(f"  Note: could not look up group names ({e}); showing IDs instead.")

    groups = []
    for g in groups_raw:
        item = {field: g.get(field) for field in GROUP_USAGE_FIELDS}
        gid = _group_id(g)
        item["group_id"] = gid
        if not item["name"]:
            item["name"] = id_to_name.get(str(gid)) or f"Group {gid}"
        groups.append(item)

    summary["groups_usage"] = groups
    return summary


# ---------------------------------------------------------------------- #
# Small helpers
# ---------------------------------------------------------------------- #
EXPORT_ID_KEYS = ("export_id", "id", "job_id")


def find_export(body, export_id: str) -> dict:
    """
    Pick our export out of a status answer. Handles:
      - one export:            {"export_id": ..., "status": ...}
      - a list:                [{...}, {...}]
      - a list in a wrapper:   {"exports": [{...}, ...]}
    """
    def is_ours(item):
        return isinstance(item, dict) and any(str(item.get(k)) == str(export_id) for k in EXPORT_ID_KEYS)

    if isinstance(body, dict) and is_ours(body):
        return body
    items = body if isinstance(body, list) else next(
        (v for v in body.values() if isinstance(v, list)), []) if isinstance(body, dict) else []
    for item in items:
        if is_ours(item):
            return item
    # A single export without an ID field: assume it's the one we just started.
    if isinstance(body, dict) and "status" in body:
        return body
    return {}


ID_KEYS = ("name_id", "group_id", "id", "uuid", "group_uuid")  # /list uses "name_id"
NAME_KEYS = ("name", "group_name", "display_name")


def _group_id(g: dict):
    """Groups may carry their ID under different keys — accept the common ones."""
    for key in ID_KEYS:
        if g.get(key) is not None:
            return g[key]
    return None


def _group_name(g: dict):
    for key in NAME_KEYS:
        if g.get(key):
            return g[key]
    return None


def parse_group_list(data) -> list[dict]:
    """
    Accept a bare list, or a wrapper like {"groups": [...]} / {"data": [...]}.
    Anything that isn't a dict inside the list is ignored.
    """
    if isinstance(data, dict):
        data = next((v for k, v in data.items()
                     if isinstance(v, list) and k in ("groups", "items", "data", "results", "list")),
                    next((v for v in data.values() if isinstance(v, list)), []))
    return [g for g in data if isinstance(g, dict)] if isinstance(data, list) else []


def group_name_map(groups: list[dict]) -> dict:
    """{group_id (as text): name} from a parsed group list."""
    names = {}
    for g in groups:
        gid, name = _group_id(g), _group_name(g)
        if gid is not None and name:
            names[str(gid)] = name
    return names


def _friendly_message(resp, path: str) -> str:
    base = FRIENDLY_ERRORS.get(resp.status_code, "request failed")
    # Add the Portal's own short message if it sent one (but keep it short).
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("detail") or body.get("error") or "")
    except ValueError:
        pass
    detail = detail.strip()[:200]
    return f"{base} ({path})" + (f" — Portal says: {detail}" if detail else "")


def format_bytes(n) -> str:
    """536_870_912_000 -> '500.00 GB'. Uses 1 GB = 1,073,741,824 bytes, like the Portal."""
    if n is None:
        return "—"
    return f"{n / GB:,.2f} GB"


# ---------------------------------------------------------------------- #
# Run on its own: python portal_client.py
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import sys

    from config_loader import ConfigError, load_config

    parser = argparse.ArgumentParser(description="Fetch organization usage (step b test).")
    parser.add_argument("--config", default="config.toml", help="path to config file")
    parser.add_argument("--dry-run", action="store_true", help="show calls, don't make them")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        sys.exit(1)

    print("Using config:")
    print(cfg.describe())

    if args.dry_run:
        print("\nDry run — these calls WOULD be made:")
        print(f"  GET {cfg.base_url}{USAGE_PATH.format(organization=cfg.organization)}")
        print(f"  GET {cfg.base_url}{GROUP_LIST_PATH.format(organization=cfg.organization)}   (group ID -> name list)")
        sys.exit(0)

    client = PortalClient(cfg)
    try:
        print(f"\nFetching usage for '{cfg.organization}'...")
        raw = client.get_org_usage()
        summary = build_usage_summary(raw, client)
    except PortalError as e:
        print(f"Portal error: {e}", file=sys.stderr)
        sys.exit(1)

    # Save the untouched API answer — useful for checking field names.
    out_dir = Path("output/raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"usage_{cfg.organization}_{date.today().isoformat()}.json"
    out_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    s = summary
    print("\nOrganization usage")
    print(f"  Packages:            {s['package_count']} / {s['max_packages']}")
    print(f"  Analysis capacity:   {format_bytes(s['total_capacity_bytes'])} incl. Flex"
          f"  (Portal {format_bytes(s['total_capacity_bytes_portal'])}"
          f" / CLI {format_bytes(s['total_capacity_bytes_cli'])}"
          f" / Flex {format_bytes(s['promo_capacity_bytes'])})")
    print(f"  Used this cycle:     Portal {format_bytes(s['this_month_used_bytes_portal'])}"
          f" / CLI {format_bytes(s['this_month_used_bytes_cli'])}")
    if s.get("time_to_capacity_reset_seconds") is not None:
        print(f"  Capacity resets in:  {s['time_to_capacity_reset_seconds'] / 86400:.1f} days")
    print(f"  Downloaded / limit:  {format_bytes(s['downloaded_this_month_bytes'])}"
          f" / {format_bytes(s['download_capacity_bytes'])}")

    busiest = sorted(s["groups_usage"], key=lambda g: g.get("projects_usage_bytes") or 0, reverse=True)
    print(f"\nGroups: {len(busiest)} (top 10 by storage used)")
    for g in busiest[:10]:
        limit = g["projects_usage_limit_bytes"]
        print(f"  - {g['name']}: {format_bytes(g['projects_usage_bytes'])}"
              f" / {format_bytes(limit) if limit is not None else 'no limit'}")

    print(f"\nRaw response saved to {out_file}")

    # Audit events only carry group IDs, so keep the ID -> name list for the report.
    try:
        groups_raw = client.list_groups_raw()
        groups_file = out_dir / f"groups_{cfg.organization}_{date.today().isoformat()}.json"
        groups_file.write_text(json.dumps(groups_raw, indent=2), encoding="utf-8")
        print(f"Group list ({len(parse_group_list(groups_raw))} groups) saved to {groups_file}")
    except PortalError as e:
        print(f"Note: could not fetch the group list ({e}); the report will show group IDs.")
