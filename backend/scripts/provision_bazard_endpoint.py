"""Provisionne l'endpoint Svix → api.bazard.run (plus de setup manuel).

Usage : uv run python -m scripts.provision_bazard_endpoint <url> [--filter ...]
Ex.   : uv run python -m scripts.provision_bazard_endpoint \
            https://api.bazard.run/api/v1/webhooks/openwearables \
            --filter connection.created --filter connection.revoked --filter workout.created

Crée (ou réutilise) l'application Svix du developer Bazard et l'endpoint,
puis imprime le whsec_... à copier dans OPENWEARABLES_WEBHOOK_SECRET.
"""

from __future__ import annotations

import argparse

from app.database import SessionLocal
from app.models import Developer
from app.services import developer_service
from app.services.outgoing_webhooks import svix as svix_service

# Filtres par défaut : les events consommés par api.bazard.run.
DEFAULT_FILTERS = ["connection.created", "connection.revoked", "workout.created"]


def _resolve_developer(email: str | None) -> Developer:
    """Résout le developer Bazard depuis la DB — même source que l'émission Celery."""
    with SessionLocal() as db:
        developers = developer_service.crud.get_all(db, filters={}, offset=0, limit=100, sort_by=None)

    if email is not None:
        for dev in developers:
            if dev.email == email:
                return dev
        raise SystemExit(f"Aucun developer avec l'email {email}")
    if not developers:
        raise SystemExit("Aucun developer en base — en créer un via le frontend OW d'abord.")
    if len(developers) > 1:
        emails = ", ".join(d.email for d in developers)
        raise SystemExit(f"Plusieurs developers en base ({emails}) — préciser --developer-email.")
    return developers[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Provisionne l'endpoint Svix → api.bazard.run.")
    parser.add_argument("url", help="URL du webhook, ex. https://api.bazard.run/api/v1/webhooks/openwearables")
    parser.add_argument(
        "--filter",
        action="append",
        dest="filters",
        default=None,
        help="Event type à souscrire (répétable). Défaut : " + ", ".join(DEFAULT_FILTERS),
    )
    parser.add_argument("--developer-email", default=None, help="Requis si plusieurs developers en base.")
    args = parser.parse_args()

    filters = args.filters if args.filters else DEFAULT_FILTERS

    if not svix_service.is_enabled():
        raise SystemExit("Svix n'est pas configuré (OUTGOING_WEBHOOKS_ENABLED / SVIX_AUTH_TOKEN / SVIX_JWT_SECRET).")

    dev = _resolve_developer(args.developer_email)
    print(f"Developer : {dev.email} (id={dev.id})")
    app_id = svix_service.ensure_application(str(dev.id), dev.email)

    # Check-before-create : ne pas dupliquer un endpoint déjà pointé sur la même URL.
    existing = svix_service.list_endpoints(app_id)
    endpoint = next((ep for ep in existing.data if ep.url == args.url), None)
    if endpoint is not None:
        print(f"Endpoint existant réutilisé (id={endpoint.id}) — filtres en place : {endpoint.filter_types}")
    else:
        endpoint = svix_service.create_endpoint(app_id, args.url, "api.bazard.run webhooks", filters)
        print(f"Endpoint créé (id={endpoint.id}) — filtres : {endpoint.filter_types}")

    secret = svix_service.get_endpoint_secret(app_id, endpoint.id)
    print(f"\nendpoint_id={endpoint.id}\nwhsec={secret}")
    print(f"\nÀ copier dans api.bazard.run/.env : OPENWEARABLES_WEBHOOK_SECRET={secret}")


if __name__ == "__main__":
    main()
