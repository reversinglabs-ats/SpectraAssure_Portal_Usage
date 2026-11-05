"""
config_loader.py — reads and checks config.toml.

What it does
    Opens the config file, makes sure the three required fields are there and
    look sensible, and returns them as a small Config object the rest of the
    tool can use. If something is wrong, it raises ConfigError with a friendly
    message that names the field to fix.

How to run it on its own (quick self-check)
    python config_loader.py
    python config_loader.py --config path/to/other.toml
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    raise SystemExit("This tool needs Python 3.11 or newer (you have "
                     f"{sys.version_info.major}.{sys.version_info.minor}). Install a newer Python from python.org.")

import tomllib  # TOML reader built into Python 3.11+, nothing to install
from dataclasses import dataclass
from pathlib import Path

REQUIRED_FIELDS = ("base_url", "organization", "token")

# Values copied from config.example.toml. If we still see them, the user
# forgot to fill the file in — better to say so than to get a confusing 401.
PLACEHOLDERS = {
    "base_url": "https://my.secure.software/your-instance/api/public/v1",
    "organization": "your-org",
    "token": "your-personal-access-token",
}


class ConfigError(Exception):
    """Something is missing or wrong in the config file."""


@dataclass(frozen=True)
class Config:
    base_url: str
    organization: str
    token: str

    def masked_token(self) -> str:
        """Show only the first and last 4 characters, e.g. 'abcd…wxyz'."""
        if len(self.token) <= 8:
            return "…"  # too short to reveal anything safely
        return f"{self.token[:4]}…{self.token[-4:]}"

    def describe(self) -> str:
        """Human-readable summary that is safe to print (token masked)."""
        return (
            f"  base_url     = {self.base_url}\n"
            f"  organization = {self.organization}\n"
            f"  token        = {self.masked_token()}"
        )

    # Keep the token out of accidental prints / logs / tracebacks.
    def __repr__(self) -> str:
        return (
            f"Config(base_url={self.base_url!r}, "
            f"organization={self.organization!r}, token='{self.masked_token()}')"
        )


def load_config(path: str | Path = "config.toml") -> Config:
    """Read the TOML file at `path`, validate it, and return a Config."""
    path = Path(path)

    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            "  Copy config.example.toml to config.toml and fill in your values."
        )

    try:
        with path.open("rb") as f:  # tomllib requires binary mode
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"{path} is not valid TOML: {e}\n"
            '  Tip: every value needs quotes, e.g. organization = "my-org"'
        ) from None

    # 1) All required fields present and non-empty
    for field in REQUIRED_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f'Missing or empty field "{field}" in {path}.\n'
                f'  Add a line like:  {field} = "..."'
            )
        if value.strip() == PLACEHOLDERS[field]:
            raise ConfigError(
                f'Field "{field}" in {path} still has the example value.\n'
                "  Replace it with your real value."
            )

    base_url = data["base_url"].strip()
    organization = data["organization"].strip()
    token = data["token"].strip()

    # 2) base_url must be HTTPS — the token travels in every request header,
    #    and plain http would send it unencrypted.
    if not base_url.lower().startswith("https://"):
        raise ConfigError(
            f'"base_url" must start with https://  (got: {base_url})'
        )

    # 3) No trailing slash, so joining paths gives ".../v1/usage/org"
    #    and not ".../v1//usage/org".
    base_url = base_url.rstrip("/")

    return Config(base_url=base_url, organization=organization, token=token)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Check a config file.")
    parser.add_argument("--config", default="config.toml", help="path to config file")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Config OK ({args.config}):")
    print(cfg.describe())
