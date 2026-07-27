"""Applique l'ordre de priorité providers Bazard (dedup multi-source).

Usage : uv run python -m scripts.set_provider_priorities

Écrit l'ordre Bazard via priority_service.bulk_update_priorities (même
couche service que la route PUT /api/v1/priorities/providers), puis ré-imprime
l'état complet en base. Idempotent : ré-exécuter ne change rien.
Remplace la configuration manuelle via le dashboard OW.
"""

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.schemas.enums import ProviderName
from app.schemas.model_crud.data_priority import ProviderPriorityBase, ProviderPriorityBulkUpdate
from app.services.priority_service import priority_service

# Ordre Bazard — plus petit = gagne quand plusieurs sources couvrent la même donnée.
BAZARD_PROVIDER_PRIORITIES: list[tuple[ProviderName, int]] = [
    (ProviderName.GARMIN, 1),
    (ProviderName.WHOOP, 2),
    (ProviderName.OURA, 3),
    (ProviderName.POLAR, 4),
    (ProviderName.SUUNTO, 5),
    (ProviderName.FITBIT, 6),
    (ProviderName.ULTRAHUMAN, 7),
    (ProviderName.STRAVA, 8),
]


def apply_bazard_priorities(db: Session) -> list[tuple[str, int]]:
    """Écrit l'ordre Bazard et retourne l'état complet (provider, priority) trié."""
    update = ProviderPriorityBulkUpdate(
        priorities=[
            ProviderPriorityBase(provider=provider, priority=priority)
            for provider, priority in BAZARD_PROVIDER_PRIORITIES
        ]
    )
    priority_service.bulk_update_priorities(db, update)

    current = priority_service.get_provider_priorities(db)
    return [(item.provider.value, item.priority) for item in current.items]


def main() -> None:
    with SessionLocal() as db:
        order = apply_bazard_priorities(db)

    print("Priorités providers appliquées — ordre courant en base :")
    for provider, priority in order:
        print(f"  {priority:>3}. {provider}")


if __name__ == "__main__":
    main()
