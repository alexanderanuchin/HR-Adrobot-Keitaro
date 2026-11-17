"""Deterministic integer-percentage allocation for unpublished draft offers.

A-04 rule: active draft positions share exactly 100 percentage points, while
REMOVED positions receive zero. PIN values are kept exactly; any unpinned
remainder is distributed in position_index order with earlier positions
receiving the leftover single percentage points.

This is a PURE calculation: it never mutates input, SQLAlchemy entities, the
PostgreSQL database, or Keitaro. Callers must persist results transactionally
and check StreamDraft.revision before applying any editor action.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


ACTIVE = "ACTIVE"
REMOVED = "REMOVED"


class WeightDistributionError(ValueError):
    """The requested allocation violates the draft editor's invariants."""


@dataclass(frozen=True, slots=True)
class WeightPosition:
    """Immutable inputs for one draft position, not a global Keitaro offer."""

    position_id: UUID
    position_index: int
    status: str = ACTIVE
    pinned_percent: int | None = None


@dataclass(frozen=True, slots=True)
class WeightResult:
    """Calculated integer percentage for one position."""

    position_id: UUID
    calculated_percent: int


def distribute_weights(positions: list[WeightPosition] | tuple[WeightPosition, ...]) -> tuple[WeightResult, ...]:
    """Calculate draft percentages, returning positions in display order.

    For N unpinned active positions, each receives ``remaining // N``.
    The first ``remaining % N`` positions (by position_index) receive one
    additional percentage point. Pinned active positions retain their PIN.

    A completely empty/removed draft displays zero everywhere; whether an
    offers-only stream with no active offers may be published is a separate
    Keitaro adapter decision. If every active position is pinned, their PINs
    must total exactly 100, since there is no position to absorb a remainder.
    """
    seen_ids: set[UUID] = set()
    seen_indices: set[int] = set()

    for position in positions:
        if not isinstance(position, WeightPosition):
            raise WeightDistributionError("each position must be a WeightPosition")
        if not isinstance(position.position_id, UUID):
            raise WeightDistributionError("position_id must be a UUID")
        if type(position.position_index) is not int or position.position_index < 0:
            raise WeightDistributionError("position_index must be a non-negative integer")
        if position.status not in (ACTIVE, REMOVED):
            raise WeightDistributionError("unknown draft position status")
        if position.pinned_percent is not None and (
            type(position.pinned_percent) is not int
            or not 0 <= position.pinned_percent <= 100
        ):
            raise WeightDistributionError("pinned_percent must be an integer from 0 to 100")
        if position.position_id in seen_ids:
            raise WeightDistributionError("duplicate local position_id")
        if position.position_index in seen_indices:
            raise WeightDistributionError("duplicate position_index")
        seen_ids.add(position.position_id)
        seen_indices.add(position.position_index)

    ordered = sorted(positions, key=lambda position: position.position_index)
    active = [position for position in ordered if position.status == ACTIVE]

    if not active:
        return tuple(WeightResult(position.position_id, 0) for position in ordered)

    pinned_total = sum(position.pinned_percent or 0 for position in active)
    if pinned_total > 100:
        raise WeightDistributionError("sum of active PIN percentages exceeds 100")

    free_positions = [position for position in active if position.pinned_percent is None]
    remaining = 100 - pinned_total
    if not free_positions and remaining != 0:
        raise WeightDistributionError("all active positions are pinned but their total is not 100")

    free_base, leftover = divmod(remaining, len(free_positions)) if free_positions else (0, 0)
    free_shares = {
        position.position_id: free_base + (index < leftover)
        for index, position in enumerate(free_positions)
    }

    return tuple(
        WeightResult(
            position_id=position.position_id,
            calculated_percent=(
                0 if position.status == REMOVED
                else position.pinned_percent if position.pinned_percent is not None
                else free_shares[position.position_id]
            ),
        )
        for position in ordered
    )
