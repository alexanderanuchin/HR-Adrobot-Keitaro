"""Keitaro Admin API v1 clients with no automatic retries or redirects."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Literal, Self
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
    """Request timed out. A write may have reached Keitaro."""


class KeitaroHTTPError(KeitaroError):
    """Unexpected HTTP status with the potentially sensitive body omitted."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Keitaro Admin API returned HTTP {status_code}")


class KeitaroAuthenticationError(KeitaroHTTPError):
    """Unauthorized (401) or forbidden (403)."""


class KeitaroEditionError(KeitaroHTTPError):
    """The tracker edition does not permit this Admin API call (402)."""


class KeitaroNotFoundError(KeitaroHTTPError):
    """The requested resource does not exist (404)."""


class KeitaroValidationError(KeitaroHTTPError):
    """Keitaro rejected the request parameters or body (400, 406, 422)."""


class KeitaroRateLimitError(KeitaroHTTPError):
    """The tracker refused the request due to its rate limit (429)."""


class KeitaroUpstreamError(KeitaroHTTPError):
    """Keitaro reported a server-side error (5xx)."""


class KeitaroResponseError(KeitaroError):
    """A successful response did not match the expected resource."""


@dataclass(frozen=True, slots=True)
class KeitaroRecord:
    """One remote entity with its complete untouched JSON representation.

    The raw response is preserved for future stream normalization/snapshots;
    never log or expose it to unauthenticated API clients.
    """

    id: int
    name: str = field(repr=False)
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class StreamOfferAssignment:
    """One offer association in the desired stream configuration."""

    offer_id: int
    share: int
    state: Literal["active", "disabled"] = "active"


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


def _record(payload: object, *, expected_id: int | None = None) -> KeitaroRecord:
    if not isinstance(payload, dict):
        raise KeitaroResponseError("Expected a JSON entity from Keitaro")
    entity_id = payload.get("id")
    name = payload.get("name")
    if type(entity_id) is not int or not 0 < entity_id <= 9_223_372_036_854_775_807 or not isinstance(name, str):
        raise KeitaroResponseError("Keitaro entity needs a positive integer ID and string name")
    if expected_id is not None and entity_id != expected_id:
        raise KeitaroResponseError("Keitaro returned the wrong entity")
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
        if (
            not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or any(not 32 <= ord(char) <= 126 for char in api_key)
        ):
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
    ) -> Self:
        return cls(
            base_url=settings.keitaro_base_url,
            api_key=settings.keitaro_api_key,
            timeout=settings.keitaro_http_timeout,
            client=client,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def _get_json(self, resource: str, *, params: dict[str, int | str] | None = None) -> object:
        return self._request_json("GET", resource, params=params)

    def _request_json(
        self,
        method: str,
        resource: str,
        *,
        params: dict[str, int | str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> object:
        response = self._send(method, resource, params=params, body=body)
        try:
            return response.json()
        except ValueError:
            raise KeitaroResponseError("Keitaro Admin API returned invalid JSON") from None

    def _send(
        self,
        method: str,
        resource: str,
        *,
        params: dict[str, int | str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        # Resource is a fixed internal literal or a validated positive integer.
        url = f"{self._origin}/admin_api/v1/{resource}"
        try:
            response = self._client.request(
                method,
                url,
                params=params,
                json=body,
                headers={"Api-Key": self._api_key, "Accept": "application/json"},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise KeitaroTimeoutError(f"Keitaro Admin API {method} timed out") from None
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
            elif response.status_code in (400, 406, 422):
                exception_type = KeitaroValidationError
            elif response.status_code == 429:
                exception_type = KeitaroRateLimitError
            elif response.status_code >= 500:
                exception_type = KeitaroUpstreamError
            else:
                exception_type = KeitaroHTTPError
            raise exception_type(response.status_code)
        return response

    def list_campaigns(self, *, offset: int = 0, limit: int | None = None) -> tuple[KeitaroRecord, ...]:
        if type(offset) is not int or offset < 0:
            raise KeitaroRequestError("offset must be a non-negative integer")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise KeitaroRequestError("limit must be a positive integer")
        params: dict[str, int] = {}
        if offset:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        return _records(self._get_json("campaigns", params=params or None))

    def get_campaign(self, campaign_id: int) -> KeitaroRecord:
        cid = _positive_id(campaign_id, "campaign_id")
        return _record(self._get_json(f"campaigns/{cid}"), expected_id=cid)

    def list_streams(self, campaign_id: int) -> tuple[KeitaroRecord, ...]:
        cid = _positive_id(campaign_id, "campaign_id")
        result = _records(self._get_json(f"campaigns/{cid}/streams"))
        for item in result:
            if type(item.raw.get("campaign_id")) is not int or item.raw["campaign_id"] != cid:
                raise KeitaroResponseError("Keitaro returned a stream for a different campaign")
        return result

    def get_stream(self, stream_id: int) -> KeitaroRecord:
        sid = _positive_id(stream_id, "stream_id")
        return _record(self._get_json(f"streams/{sid}"), expected_id=sid)

    def list_offers(self) -> tuple[KeitaroRecord, ...]:
        return _records(self._get_json("offers"))

    def get_offer(self, offer_id: int) -> KeitaroRecord:
        oid = _positive_id(offer_id, "offer_id")
        return _record(self._get_json(f"offers/{oid}"), expected_id=oid)

    def list_domains(self) -> tuple[KeitaroRecord, ...]:
        return _records(self._get_json("domains"))

    def list_groups(self, group_type: GroupType = "campaigns") -> tuple[KeitaroRecord, ...]:
        if group_type not in ("campaigns", "offers", "landings", "domains"):
            raise KeitaroRequestError("group_type must be a Keitaro group category")
        return _records(self._get_json("groups", params={"type": group_type}))

    def list_traffic_sources(self) -> tuple[KeitaroRecord, ...]:
        return _records(self._get_json("traffic_sources"))


def stream_offer_assignments(record: KeitaroRecord) -> tuple[StreamOfferAssignment, ...]:
    """Read the fields shared by the OfferStream and OfferStreamRequest schemas."""
    offers = record.raw.get("offers")
    if not isinstance(offers, list):
        raise KeitaroResponseError("Expected a JSON array of stream offers from Keitaro")
    result = []
    for offer in offers:
        if not isinstance(offer, dict):
            raise KeitaroResponseError("Invalid Keitaro stream offer")
        assignment = StreamOfferAssignment(offer.get("offer_id"), offer.get("share"), offer.get("state"))
        if not _valid_assignment(assignment):
            raise KeitaroResponseError("Invalid Keitaro stream offer ID, share or state")
        result.append(assignment)
    return tuple(result)


def _valid_assignment(assignment: StreamOfferAssignment) -> bool:
    return (
        type(assignment.offer_id) is int
        and 0 < assignment.offer_id <= 9_223_372_036_854_775_807
        and type(assignment.share) is int
        and 0 <= assignment.share <= 100
        and assignment.state in ("active", "disabled")
    )


class KeitaroAdminClient(KeitaroAdminReadClient):
    """Write methods for campaign creation and editing stream associations."""

    def create_campaign(self, fields: dict[str, Any]) -> KeitaroRecord:
        return _record(self._request_json("POST", "campaigns", body=fields))

    def create_stream(self, fields: dict[str, Any]) -> KeitaroRecord:
        cid = _positive_id(fields.get("campaign_id"), "campaign_id")
        record = _record(self._request_json("POST", "streams", body=fields))
        if type(record.raw.get("campaign_id")) is not int or record.raw["campaign_id"] != cid:
            raise KeitaroResponseError("Keitaro returned a stream for a different campaign")
        return record

    def enable_campaign(self, campaign_id: int) -> KeitaroRecord:
        cid = _positive_id(campaign_id, "campaign_id")
        # The tracker can acknowledge activation without returning a campaign.
        self._send("POST", f"campaigns/{cid}/enable")
        return self.get_campaign(cid)

    def replace_stream_offers(
        self, stream_id: int, offers: Sequence[StreamOfferAssignment],
    ) -> KeitaroRecord:
        """Send the full desired association list using a partial flow update."""
        sid = _positive_id(stream_id, "stream_id")
        assignments = tuple(offers)
        if any(not isinstance(offer, StreamOfferAssignment) or not _valid_assignment(offer) for offer in assignments):
            raise KeitaroRequestError("Each stream offer needs a positive ID, integer share from 0 to 100, and valid state")
        body = {"offers": [
            {"offer_id": offer.offer_id, "share": offer.share, "state": offer.state}
            for offer in assignments
        ]}
        record = _record(self._request_json("PUT", f"streams/{sid}", body=body), expected_id=sid)
        if Counter(stream_offer_assignments(record)) != Counter(assignments):
            raise KeitaroResponseError("Keitaro returned different stream offer assignments")
        return record
