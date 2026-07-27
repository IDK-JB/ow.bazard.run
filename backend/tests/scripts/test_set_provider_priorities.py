"""Tests for scripts/set_provider_priorities.py (Bazard provider priority order).

Covers the bulk_update with the Bazard order and the idempotence of the script
(running it twice must leave the same state).
"""

from sqlalchemy.orm import Session

from app.schemas.enums import ProviderName
from app.schemas.model_crud.data_priority import ProviderPriorityBase, ProviderPriorityBulkUpdate
from app.services.priority_service import priority_service
from scripts.set_provider_priorities import BAZARD_PROVIDER_PRIORITIES, apply_bazard_priorities

EXPECTED_ORDER = [(provider.value, priority) for provider, priority in BAZARD_PROVIDER_PRIORITIES]


def test_apply_bazard_priorities_writes_expected_order(db: Session) -> None:
    order = apply_bazard_priorities(db)

    assert order == EXPECTED_ORDER


def test_apply_bazard_priorities_overrides_existing_order(db: Session) -> None:
    # Arrange — a pre-existing order (e.g. set by hand in the dashboard) must be replaced.
    priority_service.bulk_update_priorities(
        db,
        ProviderPriorityBulkUpdate(
            priorities=[
                ProviderPriorityBase(provider=ProviderName.STRAVA, priority=1),
                ProviderPriorityBase(provider=ProviderName.GARMIN, priority=8),
            ]
        ),
    )

    order = apply_bazard_priorities(db)

    assert order == EXPECTED_ORDER


def test_apply_bazard_priorities_is_idempotent(db: Session) -> None:
    first = apply_bazard_priorities(db)
    db.expire_all()
    second = apply_bazard_priorities(db)

    assert first == second == EXPECTED_ORDER


def test_bazard_order_uses_distinct_contiguous_priorities() -> None:
    priorities = [priority for _, priority in BAZARD_PROVIDER_PRIORITIES]

    assert priorities == list(range(1, len(BAZARD_PROVIDER_PRIORITIES) + 1))
