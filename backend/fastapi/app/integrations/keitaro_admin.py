"""Read-only Keitaro Admin API v1 transport (A-01..A-04).

Official routes: https://admin-api.docs.keitaro.io/.
All calls are GET, with no automatic retries or redirect following.
The transport deliberately does NOT normalize stream actions, offer weights,
filters, or removed associations: FETCH will do that in a separate adapter.

The caller can inject an ``httpx.Client`` with ``MockTransport`` for tests.
Never log the configured key, request headers, response bodies or raw objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings


GroupType = Literal["campaigns", "offers", "landings", "domains"]


class KeitaroError(Exception):
    """Base exception; messages must not contain credentials or API bodies."""


class KeitaroConfigurationError(KeitaroError, ValueError):
    """Invalid tracker origin, API token, or timeout."""


class KeitaroRequestError(KeitaroError, ValueError):
    """Invalid caller-supplied ID, group type, or pagination arguments."""


class KeitaroTransportError(KeitaroError):
    """Connection or TLS error before a valid HTTP response was obtained."""


class KeitaroTimeoutError(KeitaroTransportError):
    """GET exceeded the configured HTTP timeout; nothing was retried."""


class KeitaroHTTPError(KeitaroError):
    """Non-200 response; do not expose the potentially sensitive body."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Keitaro Admin API returned HTTP {status_code}")


class KeitaroAuthenticationError(KeitaroHTTPError):
    """Unauthorized (401) or forbidden (403)."""


class KeitaroEditionError(KeitaroHTTPError):
    """The tracker edition does not permit this Admin API call (402)."""


class KeitaroNotFoundError(KeitaroHTTPError):
    """The requested resource does not exist (404)."""


class KeitaroRateLimitError(KeitaroHTTPError):
    """The tracker refused the request due to its rate limit (429)."""


class KeitaroUpstreamError(KeitaroHTTPError):
    """Keitaro reported a server-side error (5xx)."""


class KeitaroResponseError(KeitaroError):
    """A 200 response did not match the documented JSON resource shape."""


@dataclass(frozen=True, slots=True)
class KeitaroRecord:
    """One remote entity with its complete untouched JSON representation.

    The raw response is preserved for future stream normalization/snapshots;
    never log or expose it to unauthenticated API clients.
    """

    id: int
    name: str = field(repr=False)
    raw: dict[str, Any] = field(repr=False)


def _tracker_origin(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise KeitaroConfigurationError("Keitaro base URL must be an HTTPS tracker origin")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port  # Forces validation of malformed ports.
    except ValueError:
        raise KeitaroConfigurationError("Invalid Keitaro tracker origin") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise KeitaroConfigurationError("Keitaro base URL must be the HTTPS tracker origin, without a path")
    # Only an explicitly configured origin is ever used; no caller-provided URLs.
    return base_url.rstrip("/")


def _positive_id(value: int, label: str) -> int:
    if type(value) is not int or not 0 < value <= 9_223_372_036_854_775_807:
        raise KeitaroRequestError(f"{label} must be a positive integer")
    return value


def _records(payload: object) -> tuple[KeitaroRecord, ...]:
    if not isinstance(payload, list):
        raise KeitaroResponseError("Expected a JSON array from Keitaro")
    return tuple(_record(item) for item in payload)


def _record(payload: object) -> KeitaroRecord:
    if not isinstance(payload, dict):
        raise KeitaroResponseError("Expected a JSON entity from Keitaro")
    entity_id = payload.get("id")
    name = payload.get("name")
    if type(entity_id) is not int or entity_id <= 0 or not isinstance(name, str):
        raise KeitaroResponseError("Keitaro entity needs a positive integer ID and string name")
    return KeitaroRecord(id=entity_id, name=name, raw=payload)


class KeitaroAdminReadClient:
    """Typed, injected, server-side GET-only client for Keitaro Admin API.

    External clients are never closed by this adapter. Owned clients must be
    closed explicitly, or use ``with KeitaroAdminReadClient(...) as client``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._origin = _tracker_origin(base_url)
        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise KeitaroConfigurationError("A valid Keitaro Admin API key is required")
        if type(timeout) not in (float, int) or not isfinite(timeout) or not 0 < timeout <= 120:
            raise KeitaroConfigurationError("Keitaro HTTP timeout must be between 0 and 120 seconds")
        if client is not None and not isinstance(client, httpx.Client):
            raise KeitaroConfigurationError("Injected transport must be an httpx.Client")
        self._api_key = api_key
        self._timeout = float(timeout)
        self._owned = client is None
        # No ambient proxy variables or system trust_env rewriting requests.
        self._client = client or httpx.Client(trust_env=False, follow_redirects=False)

    @classmethod
    def from_settings(
        cls, settings: Settings, *, client: httpx.Client | None = None,
    ) -> KeitaroAdminReadClient:
        return cls(
            base_url=settings.keitaro_base_url,
            api_key=settings.keitaro_api_key,
            timeout=settings.keitaro_http_timeout,
            client=client,
        )

    def __enter__(self) -> KeitaroAdminReadClient:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def _get_json(self, resource: str, *, params: dict[str, int | str] | None = None) -> object:
        # Resource is a fixed internal literal or a validated positive integer.
        url = f"{self._origin}/admin_api/v1/{resource}"
        try:
            response = self._client.get(
                url,
                params=params,
                headers={"Api-Key": self._api_key, "Accept": "application/json"},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise KeitaroTimeoutError("Keitaro Admin API GET timed out") from None
        except httpx.RequestError:
            raise KeitaroTransportError("Unable to reach Keitaro Admin API") from None

        if response.status_code != 200:
            exception_type: type[KeitaroHTTPError]
            if response.status_code in (401, 403):
                exception_type = KeitaroAuthenticationError
            elif response.status_code == 402:
                exception_type = KeitaroEditionError
            elif response.status_code == 404:
                exception_type = KeitaroNotFoundError
            elif response.status_code == 429:
                exception_type = KeitaroRateLimitError
            elif response.status_code >= 500:
                exception_type = KeitaroUpstreamError
            else:
                exception_type = KeitaroHTTPError
            raise exception_type(response.status_code)
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            raise KeitaroResponseError("Keitaro Admin API returned invalid JSON") from None

    def list_campaigns(self, *, offset: int = 0, limit: int | None = None) -> tuple[KeitaroRecord, ...]:
        if type(offset) is not int or offset < 0:
            raise KeitaroRequestError("offset must be a non-negative integer")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise KeitaroRequestError("limit must be an integer from 1 to 1000")
        params: dict[str, int] = {}
        if offset:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        return _records(self._get_json("campaigns", params=params or None))

    def get_campaign(self, campaign_id: int) -> KeitaroRecord:
        cid = _positive_id(campaign_id, "campaign_id")
        record = _record(self._get_json(f"campaigns/{cid}"))
        if record.id != cid:
            raise KeitaroResponseError("Keitaro returned the wrong campaign")
        return record

    def list_streams(self, campaign_id: int) -> tuple[KeitaroRecord, ...]:
        cid = _positive_id(campaign_id, "campaign_id")
        result = _records(self._get_json(f"campaigns/{cid}/streams"))
        for item in result:
            if type(item.raw.get("campaign_id")) is not int or item.raw["campaign_id"] != cid:
                raise KeitaroResponseError("Keitaro returned a stream for a different campaign")
        return result

    def get_stream(self, stream_id: int) -> KeitaroRecord:
        sid = _positive_id(stream_id, "stream_id")
        record = _record(self._get_json(f"streams/{sid}"))
        if record.id != sid:
            raise KeitaroResponseError("Keitaro returned the wrong stream")
        return record

    def list_offers(self) -> tuple[KeitaroRecord, ...]:
        return _records(self._get_json("offers"))

    def get_offer(self, offer_id: int) -> KeitaroRecord:
        oid = _positive_id(offer_id, "offer_id")
        record = _record(self._get_json(f"offers/{oid}"))
        if record.id != oid:
            raise KeitaroResponseError("Keitaro returned the wrong offer")
        return record

    def list_domains(self) -> tuple[KeitaroRecord, ...]:
        return _records(self._get_json("domains"))

    def list_groups(self, group_type: GroupType = "campaigns") -> tuple[KeitaroRecord, ...]:
        if group_type not in ("campaigns", "offers", "landings", "domains"):
            raise KeitaroRequestError("group_type must be a Keitaro group category")
        return _records(self._get_json("groups", params={"type": group_type}))

    def list_traffic_sources(self) -> tuple[KeitaroRecord, ...]:
        return _records(self._get_json("traffic_sources"))
