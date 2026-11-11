"""RDAP lookups for domain expiry.

Some registries (.dev and .app for example) don't run a normal port 43 whois,
so python-whois can't read them. RDAP is plain JSON over https and works for those.
"""

import logging
import time
from datetime import datetime, timezone

import requests

from moon_watcher.config import REQUEST_TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_MAX_AGE = 24 * 3600

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rdap+json, application/json"})

# tld -> rdap base url, reloaded once a day
_servers: dict[str, str] = {}
_servers_loaded_at = 0.0


class RdapUnavailable(Exception):
    """The TLD has no RDAP server, so the caller should try whois instead."""


def _load_servers() -> dict[str, str]:
    global _servers, _servers_loaded_at
    if _servers and time.monotonic() - _servers_loaded_at < BOOTSTRAP_MAX_AGE:
        return _servers

    r = _session.get(BOOTSTRAP_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    servers = {}
    for tlds, urls in r.json().get("services", []):
        https = [u for u in urls if u.startswith("https://")]
        if not https:
            continue
        base = https[0] if https[0].endswith("/") else https[0] + "/"
        for tld in tlds:
            servers[tld.lower()] = base
    _servers, _servers_loaded_at = servers, time.monotonic()
    return servers


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # naive utc, same as what domain.py does with the whois dates
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _registrar_name(data: dict) -> str | None:
    for entity in data.get("entities", []):
        if "registrar" not in entity.get("roles", []):
            continue
        try:
            for field in entity["vcardArray"][1]:
                if field[0] == "fn" and field[3]:
                    return field[3]
        except (KeyError, IndexError, TypeError):
            pass
    return None


def parse_response(data: dict) -> dict:
    events = {e.get("eventAction"): e.get("eventDate") for e in data.get("events", [])}
    return {
        "registrar": _registrar_name(data),
        "creation_date": _parse_date(events.get("registration")),
        "expiration_date": _parse_date(events.get("expiration")),
        "updated_date": _parse_date(events.get("last changed")),
        "name_servers": [n["ldhName"].lower() for n in data.get("nameservers", []) if n.get("ldhName")],
        "status": data.get("status", []),
    }


def lookup(domain: str) -> dict:
    """Same keys as parse_response. Raises RdapUnavailable if the TLD has no RDAP server."""
    tld = domain.rsplit(".", 1)[-1].lower()
    base = _load_servers().get(tld)
    if not base:
        raise RdapUnavailable(tld)

    r = _session.get(f"{base}domain/{domain}", timeout=REQUEST_TIMEOUT, allow_redirects=True)
    if r.status_code == 404:
        raise ValueError("domain not found in RDAP")
    r.raise_for_status()
    return parse_response(r.json())
