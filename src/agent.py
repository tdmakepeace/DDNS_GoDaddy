#!/usr/bin/env python3
"""123DNS Dynamic DNS agent.

Keeps one or more A records aligned with this host's public IP using the
GoDaddy Domains API.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml

LOG = logging.getLogger("123dns")

IPV4_URLS = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
)

DEFAULT_CONFIG_PATHS = (
    Path(os.environ.get("CONFIG_PATH", "/config/config.yaml")),
    Path("config/config.yaml"),
    Path("config.yaml"),
)

DEFAULT_API_BASE = "https://api.godaddy.com"
HTTP_TIMEOUT = (10, 20)


class ConfigError(Exception):
    pass


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class DnsRecord:
    name: str
    a_record: str


@dataclass(frozen=True)
class Settings:
    records: tuple[DnsRecord, ...]
    domain: str
    api_key: str
    api_secret: str
    ttl: int
    interval_seconds: int
    api_base: str


_STOP = False


def _request_stop(signum: int, _frame: Any) -> None:
    global _STOP
    LOG.info("Received signal %s, stopping after this cycle", signum)
    _STOP = True


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _require_str(data: dict[str, Any], key: str, env_name: str | None = None) -> str:
    env_val = os.environ.get(env_name, "").strip() if env_name else ""
    raw = env_val or data.get(key, "")
    if raw is None:
        raw = ""
    value = str(raw).strip()
    if not value or value.startswith("CHANGE_ME"):
        raise ConfigError(f"Missing required setting: {env_name or key}")
    return value


def split_record_name(a_record: str, domain: str) -> str:
    """Return the GoDaddy host label (@, gateway, or nested.relative)."""
    fqdn = a_record.strip().rstrip(".").lower()
    zone = domain.strip().rstrip(".").lower()
    if not fqdn:
        raise ConfigError("a_record must be set")
    if not zone:
        raise ConfigError("domain must be set")
    if fqdn in {"@", zone}:
        return "@"
    suffix = "." + zone
    if fqdn.endswith(suffix):
        relative = fqdn[: -len(suffix)]
        if not relative:
            raise ConfigError(f"a_record {a_record!r} is not inside domain {domain!r}")
        return relative
    if "." not in fqdn:
        return fqdn
    raise ConfigError(
        f"a_record {a_record!r} does not match domain {domain!r}. "
        "Use the host label (gateway) or a full name under that domain."
    )


def _dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _parse_record_names(data: dict[str, Any]) -> list[str]:
    env_val = os.environ.get("DDNS_A_RECORD", "").strip()
    if env_val:
        names = [part.strip().rstrip(".") for part in env_val.split(",") if part.strip()]
        if not names:
            raise ConfigError("Missing required setting: DDNS_A_RECORD")
        return _dedupe_preserve(names)

    names: list[str] = []
    raw_list = data.get("a_records")
    if raw_list is not None:
        if isinstance(raw_list, str):
            raw_list = [part.strip() for part in raw_list.split(",") if part.strip()]
        if not isinstance(raw_list, list):
            raise ConfigError("a_records must be a list of host names")
        for item in raw_list:
            if item is None:
                continue
            value = str(item).strip().rstrip(".")
            if not value or value.startswith("CHANGE_ME"):
                raise ConfigError("Missing required setting: a_records")
            names.append(value)

    raw_single = data.get("a_record")
    if raw_single is not None:
        value = str(raw_single).strip().rstrip(".")
        if value:
            if value.startswith("CHANGE_ME"):
                raise ConfigError("Missing required setting: a_record")
            names.append(value)

    if not names:
        raise ConfigError("Missing required setting: a_records (or a_record)")
    return _dedupe_preserve(names)


def _resolve_records(names: list[str], domain: str) -> tuple[DnsRecord, ...]:
    records: list[DnsRecord] = []
    seen: set[str] = set()
    for entry in names:
        host = split_record_name(entry, domain)
        if host in seen:
            continue
        seen.add(host)
        fqdn = domain if host == "@" else f"{host}.{domain}"
        records.append(DnsRecord(name=host, a_record=fqdn))
    return tuple(records)


def load_config(path: Path) -> Settings:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    data = _expand_env(raw)
    keys_data = data.get("keys") or {}
    if keys_data is None:
        keys_data = {}
    if not isinstance(keys_data, dict):
        raise ConfigError("keys must be a mapping of api_key and api_secret")

    domain = _require_str(data, "domain", "DDNS_DOMAIN").rstrip(".")
    records = _resolve_records(_parse_record_names(data), domain)
    api_key = (
        os.environ.get("DDNS_API_KEY", "").strip()
        or str(keys_data.get("api_key") or data.get("api_key") or "").strip()
    )
    api_secret = (
        os.environ.get("DDNS_API_SECRET", "").strip()
        or str(keys_data.get("api_secret") or data.get("api_secret") or "").strip()
    )
    if not api_key or api_key.startswith("CHANGE_ME"):
        raise ConfigError("Missing API key (keys.api_key or DDNS_API_KEY)")
    if not api_secret or api_secret.startswith("CHANGE_ME"):
        raise ConfigError("Missing API secret (keys.api_secret or DDNS_API_SECRET)")

    ttl = int(data.get("ttl", 600))
    if ttl < 600:
        raise ConfigError("ttl must be at least 600 seconds (GoDaddy minimum)")

    interval = int(data.get("interval_seconds", 300))
    if interval < 30:
        raise ConfigError("interval_seconds must be at least 30")

    api_base = str(data.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    return Settings(
        records=records,
        domain=domain,
        api_key=api_key,
        api_secret=api_secret,
        ttl=ttl,
        interval_seconds=interval,
        api_base=api_base,
    )


def _http_get_text(url: str, timeout: float = 10.0) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text.strip()


def discover_public_ipv4() -> str:
    errors: list[str] = []
    for url in IPV4_URLS:
        try:
            text = _http_get_text(url)
            parsed = ipaddress.ip_address(text)
            if parsed.version != 4:
                raise ValueError(f"expected IPv4, got {parsed}")
            if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
                raise ValueError(f"non-public address {parsed}")
            LOG.debug("Public IPv4 from %s: %s", url, parsed)
            return str(parsed)
        except Exception as exc:  # noqa: BLE001 — probe next provider
            errors.append(f"{url}: {exc}")
            LOG.debug("IP probe failed via %s: %s", url, exc)
    raise UpdateError(f"Could not discover public IPv4: {'; '.join(errors)}")


def _auth_headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"sso-key {settings.api_key}:{settings.api_secret}",
        "Accept": "application/json",
    }


def _record_url(settings: Settings, record: DnsRecord) -> str:
    name = quote(record.name, safe="@._-")
    domain = quote(settings.domain)
    return f"{settings.api_base}/v1/domains/{domain}/records/A/{name}"


def _api_error(action: str, response: requests.Response) -> UpdateError:
    body = response.text.strip()
    try:
        payload = response.json()
        message = payload.get("message") or payload.get("code") or body
    except ValueError:
        message = body or response.reason
    hint = ""
    if response.status_code in {401, 403}:
        hint = (
            " Use a Production key/secret from https://developer.godaddy.com/keys "
            "(not OTE), for the account that holds the domain."
        )
    return UpdateError(f"{action} failed (HTTP {response.status_code}): {message}.{hint}")


def current_a_ips(settings: Settings, record: DnsRecord) -> list[str]:
    try:
        response = requests.get(
            _record_url(settings, record),
            headers=_auth_headers(settings),
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise UpdateError(f"GoDaddy GET A {record.a_record} failed: {exc}") from exc
    if response.status_code == 404:
        return []
    if not response.ok:
        raise _api_error(f"GET A {record.a_record}", response)
    payload = response.json()
    if not isinstance(payload, list):
        raise UpdateError(f"Unexpected GoDaddy GET payload: {payload!r}")
    return [str(item.get("data", "")).strip() for item in payload if item.get("data")]


def replace_a_record(settings: Settings, record: DnsRecord, ipaddr: str) -> None:
    try:
        response = requests.put(
            _record_url(settings, record),
            headers={**_auth_headers(settings), "Content-Type": "application/json"},
            json=[{"data": ipaddr, "ttl": settings.ttl}],
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise UpdateError(f"GoDaddy PUT A {record.a_record} failed: {exc}") from exc
    if response.status_code not in {200, 201, 204}:
        raise _api_error(f"PUT A {record.a_record} -> {ipaddr}", response)


def update_one_record(settings: Settings, record: DnsRecord, ipaddr: str) -> str:
    current = current_a_ips(settings, record)
    if current == [ipaddr]:
        LOG.info("A %s already %s", record.a_record, ipaddr)
        return "unchanged"
    LOG.info(
        "Updating A %s from %s to %s",
        record.a_record,
        ",".join(current) or "(none)",
        ipaddr,
    )
    replace_a_record(settings, record, ipaddr)
    LOG.info("Updated A %s -> %s", record.a_record, ipaddr)
    return "updated"


def update_dns_records(settings: Settings, ipaddr: str) -> str:
    ipaddress.IPv4Address(ipaddr)
    results: list[str] = []
    errors: list[str] = []
    for record in settings.records:
        try:
            results.append(update_one_record(settings, record, ipaddr))
        except UpdateError as exc:
            errors.append(str(exc))
    if errors:
        raise UpdateError(
            f"{len(errors)} of {len(settings.records)} record(s) failed: "
            f"{'; '.join(errors)}"
        )
    if "updated" in results:
        return "updated"
    return "unchanged"


def run_once(settings: Settings) -> str:
    ipaddr = discover_public_ipv4()
    return update_dns_records(settings, ipaddr)


def run_loop(settings: Settings) -> int:
    hosts = ", ".join(record.a_record for record in settings.records)
    labels = ", ".join(record.name for record in settings.records)
    LOG.info(
        "Watching A %s (GoDaddy hosts %s, zone %s) every %ss",
        hosts,
        labels,
        settings.domain,
        settings.interval_seconds,
    )
    last_ip = ""
    while not _STOP:
        try:
            ipaddr = discover_public_ipv4()
            if ipaddr == last_ip:
                LOG.info("Public IP unchanged (%s)", ipaddr)
            else:
                update_dns_records(settings, ipaddr)
                last_ip = ipaddr
        except (ConfigError, UpdateError, requests.RequestException) as exc:
            LOG.error("%s", exc)
        except Exception:
            LOG.exception("Unexpected error during update cycle")
        deadline = time.monotonic() + settings.interval_seconds
        while not _STOP and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    LOG.info("Stopped")
    return 0


def resolve_config_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path)
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    return DEFAULT_CONFIG_PATHS[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="123DNS Dynamic DNS agent")
    parser.add_argument(
        "-c",
        "--config",
        help="Path to config.yaml (default: /config/config.yaml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single update and exit",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate config and print the resolved records, then exit",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config_path = resolve_config_path(args.config)
    try:
        settings = load_config(config_path)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    if args.check_config:
        print(
            json.dumps(
                {
                    "a_records": [record.a_record for record in settings.records],
                    "names": [record.name for record in settings.records],
                    "domain": settings.domain,
                    "ttl": settings.ttl,
                    "interval_seconds": settings.interval_seconds,
                    "api_base": settings.api_base,
                },
                indent=2,
            )
        )
        return 0

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    if args.once:
        try:
            run_once(settings)
        except (ConfigError, UpdateError, requests.RequestException) as exc:
            LOG.error("%s", exc)
            return 1
        return 0

    return run_loop(settings)


if __name__ == "__main__":
    sys.exit(main())
