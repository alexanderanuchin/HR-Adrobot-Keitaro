"""Opt-in GET-only Keitaro Admin API contract diagnostic (A-01..A-04).

Run inside the FastAPI Docker container with a locally configured API key:

    python -m app.integrations.keitaro_contract_probe
    python -m app.integrations.keitaro_contract_probe --campaign-id 123

The output is *only* a sanitized structural summary. Names, IDs, raw JSON,
URLs, free-form field values, API keys, and upstream error bodies are never
included. No data is written to Keitaro, PostgreSQL, or files.

This probe is intentionally not an API endpoint or automatic startup task.
It performs a finite set of GET calls, without redirects or retries, through
the already-tested KeitaroAdminReadClient.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import get_settings
from app.integrations.keitaro_admin import (
    KeitaroAdminReadClient,
    KeitaroError,
    KeitaroHTTPError,
    KeitaroRecord,
)

_MAX_ID = 9_223_372_036_854_775_807
_SAFE_FIELDS = frozenset({
    "id", "name", "alias", "state", "status", "type", "schema", "schema_type",
    "stream_type", "campaign_id", "stream_id", "offer_id", "domain_id", "group_id",
    "traffic_source_id", "position", "priority", "share", "weight", "is_active",
    "enabled", "offers", "landings", "filters", "action_type", "action_payload",
    "action_options", "action", "url", "domain", "domains", "is_default",
})
_SAFE_ENUMS = {
    "state": frozenset({"active", "disabled", "deleted"}),
    "status": frozenset({"active", "disabled", "deleted"}),
    "type": frozenset({"position", "weight", "campaigns", "offers", "domains", "landings", "regular", "forced", "default"}),
    "schema": frozenset({"offers", "redirect", "landings", "landing"}),
    "schema_type": frozenset({"offers", "redirect", "landings", "landing"}),
    "stream_type": frozenset({"regular", "forced", "default"}),
}


def _positive_id(value: object, field_name: str) -> int:
    if type(value) is not int or not 0 < value <= _MAX_ID:
        raise ValueError(f"{field_name} must be a positive 64-bit integer")
    return value


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "other"


def _field_types(payload: dict[str, Any]) -> dict[str, str]:
    return {
        key: _kind(value)
        for key, value in sorted(payload.items())
        if key in _SAFE_FIELDS
    }


def _sample(record: KeitaroRecord) -> dict[str, object]:
    """Only explicit allowlisted field names, types and safe enums leave here."""
    raw = record.raw
    info: dict[str, object] = {"fields": _field_types(raw)}
    enum_values: dict[str, str] = {}
    for field, allowed in _SAFE_ENUMS.items():
        if field in raw:
            value = raw[field]
            enum_values[field] = value if type(value) is str and value in allowed else "other"
    if enum_values:
        info["known_enums"] = enum_values

    for field in ("offers", "filters", "landings"):
        entries = raw.get(field)
        if isinstance(entries, list) and entries:
            first = entries[0]
            if isinstance(first, dict):
                info[f"{field}_item_fields"] = _field_types(first)
            else:
                info[f"{field}_item_type"] = _kind(first)
    return info


def _inspect_collection(fetch: Callable[[], tuple[KeitaroRecord, ...]]) -> dict[str, object]:
    return _safe_call(lambda: _collection_sample(fetch()))


def _collection_sample(records: tuple[KeitaroRecord, ...]) -> dict[str, object]:
    if not records:
        return {"status": "empty"}
    return {"status": "ok", "sample": _sample(records[0])}


def _safe_call(fetch: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        return fetch()
    except KeitaroHTTPError as exc:
        return {"status": "error", "error": "http", "http_status": exc.status_code}
    except KeitaroError as exc:
        # The class name is fixed in our own code; never include str(exc),
        # upstream response bodies, request URLs or exception chains.
        return {"status": "error", "error": type(exc).__name__}
    except Exception:
        # Even unexpected exceptions must not expose raw response or secrets.
        return {"status": "error", "error": "unexpected_local_error"}


def probe_keitaro_contract(
    client: KeitaroAdminReadClient,
    *,
    campaign_id: int | None = None,
    offer_id: int | None = None,
) -> dict[str, object]:
    """Probe read-only response shapes; never print or persist remote values.

    Optional IDs are supplied explicitly by the operator. A campaign is NEVER
    chosen automatically for stream inspection. All IDs are withheld from the
    report even when provided by the operator.
    """
    if campaign_id is not None:
        _positive_id(campaign_id, "campaign_id")
    if offer_id is not None:
        _positive_id(offer_id, "offer_id")

    checks: dict[str, dict[str, object]] = {
        "campaigns": _inspect_collection(lambda: client.list_campaigns(limit=1)),
        "offers": _inspect_collection(client.list_offers),
        "domains": _inspect_collection(client.list_domains),
        "campaign_groups": _inspect_collection(lambda: client.list_groups("campaigns")),
        "traffic_sources": _inspect_collection(client.list_traffic_sources),
    }
    if offer_id is not None:
        checks["offer_detail"] = _safe_call(lambda: {"status": "ok", "sample": _sample(client.get_offer(offer_id))})

    if campaign_id is not None:
        checks["campaign_detail"] = _safe_call(
            lambda: {"status": "ok", "sample": _sample(client.get_campaign(campaign_id))}
        )
        # Never weaken the read client's strict campaign ownership check.
        # On success, inspect one stream belonging to the explicitly selected
        # campaign, preferring an offer stream to capture its offer schema.
        selected_stream: list[int] = []

        def get_campaign_streams() -> dict[str, object]:
            rows = client.list_streams(campaign_id)
            if not rows:
                return {"status": "empty"}
            preferred = next(
                (row for row in rows if row.raw.get("schema") == "offers"),
                rows[0],
            )
            selected_stream.append(preferred.id)
            return {"status": "ok", "sample": _sample(preferred)}

        checks["campaign_streams"] = _safe_call(get_campaign_streams)
        if selected_stream:
            checks["stream_detail"] = _safe_call(
                lambda: {"status": "ok", "sample": _sample(client.get_stream(selected_stream[0]))}
            )

    return {
        "probe": "keitaro_admin_v1_get_only",
        "ok": all(item["status"] != "error" for item in checks.values()),
        "checks": checks,
        "writes_performed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe structural GET-only Keitaro API probe")
    parser.add_argument("--campaign-id", type=int, default=None, help="Optional existing campaign ID for stream read")
    parser.add_argument("--offer-id", type=int, default=None, help="Optional existing offer ID for detail read")
    args = parser.parse_args(argv)
    try:
        if args.campaign_id is not None:
            _positive_id(args.campaign_id, "campaign_id")
        if args.offer_id is not None:
            _positive_id(args.offer_id, "offer_id")
        with KeitaroAdminReadClient.from_settings(get_settings()) as client:
            report = probe_keitaro_contract(client, campaign_id=args.campaign_id, offer_id=args.offer_id)
    except Exception:
        # Configuration/parsing exceptions can contain credentials or hostnames.
        # Deliberately emit only a fixed, secret-free setup failure message.
        print(json.dumps({"probe": "keitaro_admin_v1_get_only", "ok": False,
                          "error": "invalid_configuration_or_arguments", "writes_performed": False}))
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
