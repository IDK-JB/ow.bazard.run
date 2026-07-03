"""Fitbit webhook handler (subscription API).

Fitbit sends notify-only webhooks: a JSON *array* of lightweight notifications
(``collectionType``, ``date``, ``ownerId``) and expects the consumer to pull
the actual data back via the REST API.

Signature scheme
----------------
  Header   : X-Fitbit-Signature: BASE64(HMAC-SHA1(raw_body, client_secret + "&"))
  Algorithm: HMAC-SHA1 keyed with the OAuth client secret suffixed by ``&``
             (Fitbit follows the OAuth 1.0a signing-key convention).

Verification (GET)
------------------
  On subscriber creation, Fitbit GETs the endpoint twice with a ``verify``
  query parameter: once with the correct code (expects **204**) and once with
  an intentionally wrong code (expects **404**). Any other status fails the
  verification, so ``handle_challenge`` returns a raw ``Response``.

Delivery model
--------------
  ``dispatch()`` acknowledges immediately with **204** (Fitbit requirement)
  and enqueues a Celery task per notification batch. ``process_payload()``
  resolves the connection from ``ownerId`` and pulls the affected date's
  activities through the standard ``FitbitWorkouts.load_data`` path.

Per-user subscriptions
----------------------
  Unlike Strava's app-level subscription, Fitbit requires one subscription per
  user (created with the user's token). ``ensure_user_subscription`` is called
  best-effort after every successful OAuth callback.

See: https://dev.fitbit.com/build/reference/web-api/developer-guide/using-subscriptions/
"""

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from celery import current_app as celery_app
from fastapi import HTTPException, Request, Response
from pydantic import ValidationError

from app.config import settings
from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.schemas.providers.fitbit import FitbitWebhookNotification
from app.services.providers.fitbit.workouts import FitbitWorkouts
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.raw_payload_storage import store_raw_payload
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_PROCESS_PUSH_TASK = "app.integrations.celery.tasks.webhook_push_task.process_webhook_push"

#: Collections we react to. Other subscribed collections (sleep, body, ...)
#: are acknowledged and ignored — consumers read biometrics through the
#: timeseries pull path, not through activity records.
_ACTIVITY_COLLECTIONS = {"activities"}


class FitbitWebhookHandler(BaseWebhookHandler):
    """Webhook handler for Fitbit subscription-API notifications."""

    def __init__(self, workouts: FitbitWorkouts) -> None:
        super().__init__("fitbit")
        self.workouts = workouts
        self.connection_repo = UserConnectionRepository()

    # ------------------------------------------------------------------
    # BaseWebhookHandler interface
    # ------------------------------------------------------------------

    def verify_signature(self, request: Request, body: bytes) -> bool:
        """Validate X-Fitbit-Signature: BASE64(HMAC-SHA1(body, secret + "&"))."""
        signature = request.headers.get("X-Fitbit-Signature", "")
        if not signature:
            return False
        secret = settings.fitbit_client_secret.get_secret_value() if settings.fitbit_client_secret else ""
        if not secret:
            # An unset OR empty secret would make the signing key predictable
            # ("&" alone) — refuse to verify rather than weaken the HMAC.
            log_structured(
                logger,
                "warning",
                "Fitbit webhook received but FITBIT_CLIENT_SECRET is not configured",
                provider="fitbit",
                action="webhook_signature_unconfigured",
            )
            return False

        signing_key = f"{secret}&".encode()
        expected = base64.b64encode(hmac.new(signing_key, body, hashlib.sha1).digest()).decode()
        return hmac.compare_digest(expected, signature)

    def parse_payload(self, body: bytes) -> list[FitbitWebhookNotification]:
        try:
            data = json.loads(body)
            if not isinstance(data, list):
                raise HTTPException(status_code=400, detail="Expected a JSON array of notifications")
            return [FitbitWebhookNotification(**item) for item in data]
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
        except (ValidationError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid payload: {exc}") from exc

    def dispatch(self, db: DbSession, payload: list[FitbitWebhookNotification]) -> Response:
        """Store raw payload, enqueue async processing, ack with 204.

        Fitbit requires a 204 within 5 seconds; everything slow (token refresh,
        REST pulls) happens in the Celery task.
        """
        trace_id = str(uuid4())[:8]
        raw = [n.model_dump(by_alias=True) for n in payload]

        log_structured(
            logger,
            "info",
            "Received Fitbit webhook notifications",
            provider="fitbit",
            trace_id=trace_id,
            count=len(raw),
        )

        store_raw_payload(source="webhook", provider="fitbit", payload={"notifications": raw}, trace_id=trace_id)

        task = celery_app.send_task(
            _PROCESS_PUSH_TASK,
            args=["fitbit", {"notifications": raw}, trace_id],
            queue="webhook_sync",
        )
        log_structured(
            logger,
            "info",
            "Enqueued Fitbit webhook processing task",
            provider="fitbit",
            trace_id=trace_id,
            task_id=str(getattr(task, "id", "")),
        )

        return Response(status_code=204)

    def handle_challenge(self, request: Request) -> Response:
        """Handle Fitbit GET subscriber verification.

        Fitbit sends the correct code (must answer **204**) then a wrong one
        (must answer **404**); any other pair of statuses fails verification.
        """
        verify = request.query_params.get("verify", "")
        expected = (
            settings.fitbit_webhook_verify_code.get_secret_value() if settings.fitbit_webhook_verify_code else None
        )
        if not expected:
            log_structured(
                logger,
                "warning",
                "Fitbit verification received but FITBIT_WEBHOOK_VERIFY_CODE is not configured",
                provider="fitbit",
                action="webhook_challenge_unconfigured",
            )
            raise HTTPException(status_code=404, detail="Verification not configured")

        if verify and hmac.compare_digest(expected, verify):
            log_structured(
                logger,
                "info",
                "Fitbit webhook subscriber verified",
                provider="fitbit",
                action="webhook_challenge_accepted",
            )
            return Response(status_code=204)
        raise HTTPException(status_code=404, detail="Invalid verification code")

    def supported_event_types(self) -> list[str]:
        return sorted(_ACTIVITY_COLLECTIONS)

    # ------------------------------------------------------------------
    # Async processing (Celery)
    # ------------------------------------------------------------------

    def process_payload(self, db: DbSession, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
        """Pull back the data referenced by a notification batch.

        Called by the ``process_webhook_push`` Celery task with its own DB
        session. Only activity collections trigger a pull; other collections
        are acknowledged (biometrics flow through the timeseries pull path).
        """
        notifications = payload.get("notifications", [])
        processed, ignored, orphaned, failed = 0, 0, 0, 0

        for item in notifications:
            # One faulty notification (bad date, Fitbit API failure on the
            # pull) must never abort the rest of the batch — count it,
            # report it to Sentry, move on.
            try:
                n = FitbitWebhookNotification(**item)

                if n.collection_type not in _ACTIVITY_COLLECTIONS:
                    ignored += 1
                    continue

                connection = self.connection_repo.get_by_provider_user_id(db, "fitbit", n.owner_id)
                if not connection:
                    orphaned += 1
                    log_structured(
                        logger,
                        "warning",
                        "No connection found for Fitbit owner",
                        provider="fitbit",
                        trace_id=trace_id,
                        action="webhook_no_connection",
                        fitbit_owner_id=n.owner_id,
                    )
                    continue

                user_id = connection.user_id
                self.connection_repo.update_last_synced_at(db, connection)

                # The notification only says "this date changed": pull a 1-day
                # window around it through the standard load path (idempotence
                # is the event-record layer's responsibility, same as periodic
                # sync).
                day = datetime.strptime(n.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                self.workouts.load_data(
                    db,
                    user_id,
                    start_date=day - timedelta(days=1),
                    end_date=day + timedelta(days=1),
                )
                processed += 1
                log_structured(
                    logger,
                    "info",
                    "Processed Fitbit webhook notification",
                    provider="fitbit",
                    trace_id=trace_id,
                    user_id=str(user_id),
                    collection=n.collection_type,
                    date=n.date,
                )
            except Exception as exc:  # noqa: BLE001 — batch isolation by design
                failed += 1
                log_and_capture_error(
                    exc,
                    logger,
                    "Failed to process Fitbit webhook notification",
                    extra={"provider": "fitbit", "trace_id": trace_id, "item": item},
                )

        return {
            "status": "processed",
            "processed": processed,
            "ignored": ignored,
            "orphaned": orphaned,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # Per-user subscription management
    # ------------------------------------------------------------------

    def ensure_user_subscription(self, db: DbSession, user_id: UUID) -> None:
        """Subscribe the user's activities collection (idempotent, best-effort).

        Fitbit subscriptions are per-user and created with the user's token:
        POST /1/user/-/activities/apiSubscriptions/{subscription-id}.json.
        The OW user id doubles as the subscription id (unique per user). A 409
        means the subscription already exists — success for our purposes. Any
        failure is logged, never raised: a missed subscription degrades to the
        polling path, it must not break the OAuth callback.
        """
        endpoint = f"/1/user/-/activities/apiSubscriptions/{user_id}.json"
        params = {"subscriberId": settings.fitbit_subscriber_id} if settings.fitbit_subscriber_id else None
        try:
            self.workouts._make_api_request(db, user_id, endpoint, method="POST", params=params)  # noqa: SLF001
            log_structured(
                logger,
                "info",
                "Fitbit activities subscription ensured",
                provider="fitbit",
                action="webhook_subscription_created",
                user_id=str(user_id),
            )
        except HTTPException as exc:
            if exc.status_code == 409:
                # Already subscribed — success for our purposes, not a failure.
                log_structured(
                    logger,
                    "info",
                    "Fitbit activities subscription already exists",
                    provider="fitbit",
                    action="webhook_subscription_exists",
                    user_id=str(user_id),
                )
                return
            log_structured(
                logger,
                "warning",
                f"Could not create Fitbit activities subscription: {exc.detail}",
                provider="fitbit",
                action="webhook_subscription_failed",
                user_id=str(user_id),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            log_structured(
                logger,
                "warning",
                f"Could not create Fitbit activities subscription: {exc}",
                provider="fitbit",
                action="webhook_subscription_failed",
                user_id=str(user_id),
            )
