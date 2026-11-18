"""Pure offer allocation tests; no database, network, or Keitaro access."""

from dataclasses import FrozenInstanceError
from random import Random
from uuid import UUID

import pytest

from app.services.weight_distribution import (
    ACTIVE,
    REMOVED,
    WeightDistributionError,
    WeightPosition,
    distribute_weights,
)


def position(index: int, *, status: str = ACTIVE, pin: int | None = None) -> WeightPosition:
    return WeightPosition(
        position_id=UUID(int=index + 1),
        position_index=index,
        status=status,
        pinned_percent=pin,
    )


def percentages(*positions: WeightPosition) -> list[int]:
    return [result.calculated_percent for result in distribute_weights(list(positions))]


def test_empty_draft_has_no_percentages():
    assert distribute_weights([]) == ()


def test_every_removed_position_is_zero_even_with_a_saved_pin():
    assert percentages(position(0, status=REMOVED, pin=100), position(1, status=REMOVED)) == [0, 0]


def test_one_active_offer_gets_everything():
    assert percentages(position(0)) == [100]


def test_two_active_offers_split_evenly():
    assert percentages(position(0), position(1)) == [50, 50]


def test_three_active_offers_remainder_goes_first():
    assert percentages(position(0), position(1), position(2)) == [34, 33, 33]


def test_four_active_offers_have_25_percent_each():
    assert percentages(*(position(i) for i in range(4))) == [25, 25, 25, 25]


def test_pin_25_leaves_38_and_37_to_two_unpinned():
    assert percentages(position(0, pin=25), position(1), position(2)) == [25, 38, 37]


def test_removed_offer_does_not_take_part_in_distribution():
    assert percentages(position(0), position(1, status=REMOVED), position(2)) == [50, 0, 50]


def test_removed_pin_does_not_reserve_a_percentage():
    assert percentages(position(0, status=REMOVED, pin=90), position(1), position(2)) == [0, 50, 50]


def test_pin_zero_is_a_real_pin_not_an_unpinned_slot():
    assert percentages(position(0, pin=0), position(1)) == [0, 100]


def test_pin_100_leaves_unpinned_positions_at_zero():
    assert percentages(position(0, pin=100), position(1), position(2)) == [100, 0, 0]


def test_all_pinned_exactly_100_succeeds():
    assert percentages(position(0, pin=25), position(1, pin=75)) == [25, 75]


def test_single_pinned_100_succeeds():
    assert percentages(position(0, pin=100)) == [100]


def test_two_pins_and_two_free_positions_distribute_remainder():
    assert percentages(position(0, pin=10), position(1, pin=25), position(2), position(3)) == [10, 25, 33, 32]


def test_order_is_position_index_not_input_list_order():
    items = [position(2), position(0, pin=25), position(1)]
    result = distribute_weights(items)
    assert [row.position_id for row in result] == [UUID(int=1), UUID(int=2), UUID(int=3)]
    assert [row.calculated_percent for row in result] == [25, 38, 37]


def test_call_is_repeatable_and_does_not_mutate_inputs():
    rows = [position(2), position(0), position(1, status=REMOVED, pin=30)]
    original = list(rows)
    first = distribute_weights(rows)
    assert distribute_weights(rows) == first
    assert rows == original
    assert rows[2].pinned_percent == 30
    with pytest.raises(FrozenInstanceError):
        rows[0].position_index = 99  # type: ignore[misc]


def test_excessive_pin_total_is_rejected():
    with pytest.raises(WeightDistributionError, match="exceeds 100"):
        percentages(position(0, pin=60), position(1, pin=41), position(2))


def test_fully_pinned_without_100_is_rejected():
    with pytest.raises(WeightDistributionError, match="not 100"):
        percentages(position(0, pin=25), position(1, pin=25))


@pytest.mark.parametrize("pin", [0, 25, 99])
def test_single_active_offer_keeps_full_share_with_a_future_pin(pin):
    assert percentages(position(0, status=REMOVED), position(1, pin=pin)) == [0, 100]


@pytest.mark.parametrize("pin", [0, 25, 100])
def test_single_active_offer_uses_an_immediate_pin(pin):
    active = WeightPosition(UUID(int=2), 1, pinned_percent=pin, pin_immediately=True)
    assert percentages(position(0, status=REMOVED), active) == [0, pin]
    assert percentages(position(0, status=REMOVED), active, position(2)) == [0, pin, 100 - pin]


def test_duplicate_position_identity_is_rejected():
    with pytest.raises(WeightDistributionError, match="duplicate local"):
        distribute_weights([position(0), position(0)])


def test_duplicate_order_is_rejected_even_with_distinct_ids():
    with pytest.raises(WeightDistributionError, match="duplicate position_index"):
        distribute_weights([
            position(0),
            WeightPosition(position_id=UUID(int=99), position_index=0),
        ])


@pytest.mark.parametrize("bad_order", [-1, 1.2, True, "2"])
def test_reject_invalid_display_index(bad_order):
    with pytest.raises(WeightDistributionError, match="position_index"):
        distribute_weights([WeightPosition(position_id=UUID(int=1), position_index=bad_order)])


@pytest.mark.parametrize("bad_pin", [-1, 101, 25.0, True, "25"])
def test_reject_pin_outside_integer_range(bad_pin):
    with pytest.raises(WeightDistributionError, match="pinned_percent"):
        distribute_weights([WeightPosition(position_id=UUID(int=1), position_index=0, pinned_percent=bad_pin)])


def test_reject_unknown_position_status():
    with pytest.raises(WeightDistributionError, match="unknown"):
        distribute_weights([position(0, status="DELETED")])


def test_reject_non_uuid_identity():
    with pytest.raises(WeightDistributionError, match="UUID"):
        distribute_weights([WeightPosition(position_id="not-a-uuid", position_index=0)])  # type: ignore[arg-type]


def test_reject_non_position_input():
    with pytest.raises(WeightDistributionError, match="WeightPosition"):
        distribute_weights([{"id": 1}])  # type: ignore[list-item]


def test_sparse_position_indices_still_allocate_by_order():
    assert percentages(position(0), position(8), position(99)) == [34, 33, 33]


def test_many_random_but_seeded_cases_preserve_all_invariants():
    """Fixed seed: deterministic stress test without flaky external dependencies."""
    rng = Random(20261117)
    for _ in range(150):
        count = rng.randint(1, 30)
        pinned_count = rng.randrange(count)  # at least one unpinned active position
        ids = list(range(count))
        rng.shuffle(ids)
        pinned_indices = set(ids[:pinned_count])
        available = 100
        rows = []
        for index in range(count):
            pin = None
            if index in pinned_indices:
                pin = rng.randint(0, available)
                available -= pin
            rows.append(position(index, pin=pin))
        removed_indices = set(range(count, count + rng.randint(0, 4)))
        rows.extend(position(index, status=REMOVED, pin=70) for index in removed_indices)
        rng.shuffle(rows)
        results = distribute_weights(rows)
        by_id = {row.position_id: row.calculated_percent for row in results}
        assert len(results) == len(rows)
        assert sum(by_id[row.position_id] for row in rows if row.status == ACTIVE) == 100
        assert all(by_id[row.position_id] == 0 for row in rows if row.status == REMOVED)
        assert all(by_id[row.position_id] == row.pinned_percent for row in rows if row.status == ACTIVE and row.pinned_percent is not None)
        assert all(0 <= row.calculated_percent <= 100 for row in results)
        assert results == distribute_weights(list(reversed(rows)))
