"""Tests for the Fitbit webhook handler (subscription API)."""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from pydantic import SecretStr

from app.config import settings
from app.services.providers.fitbit.strategy import FitbitStrategy
from app.services.providers.fitbit.webhook_handler import FitbitWebhookHandler

_SECRET = "fitbit-test-secret"


def _handler() -> FitbitWebhookHandler:
    return FitbitWebhookHandler(workouts=MagicMock())


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return base64.b64encode(hmac.new(f"{secret}&".encode(), body, hashlib.sha1).digest()).decode()


def _request(headers: dict[str, str] | None = None, query: dict[str, str] | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = headers or {}
    request.query_params = query or {}
    return request


_NOTIFICATION = {
    "collectionType": "activities",
    "date": "2026-07-01",
    "ownerId": "FITBIT123",
    "ownerType": "user",
    "subscriptionId": "sub-1",
}


class TestFitbitStrategyWiring:
    def test_webhooks_wired(self) -> None:
        strategy = FitbitStrategy()
        assert isinstance(strategy.webhooks, FitbitWebhookHandler)

    def test_capabilities_expose_webhook_ping(self) -> None:
        caps = FitbitStrategy().capabilities
        assert caps.rest_pull is True
        assert caps.webhook_ping is True


class TestVerifySignature:
    def test_valid_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "fitbit_client_secret", SecretStr(_SECRET))
        body = json.dumps([_NOTIFICATION]).encode()
        request = _request(headers={"X-Fitbit-Signature": _sign(body)})
        assert _handler().verify_signature(request, body) is True

    def test_invalid_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "fitbit_client_secret", SecretStr(_SECRET))
        body = json.dumps([_NOTIFICATION]).encode()
        request = _request(headers={"X-Fitbit-Signature": _sign(body, secret="wrong")})
        assert _handler().verify_signature(request, body) is False

    def test_missing_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "fitbit_client_secret", SecretStr(_SECRET))
        assert _handler().verify_signature(_request(), b"[]") is False

    def test_unconfigured_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "fitbit_client_secret", None)
        request = _request(headers={"X-Fitbit-Signature": "anything"})
        assert _handler().verify_signature(request, b"[]") is False


class TestHandleChallenge:
    def test_correct_code_returns_204(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "fitbit_webhook_verify_code", SecretStr("code-ok"))
        response = _handler().handle_challenge(_request(query={"verify": "code-ok"}))
        assert isinstance(response, Response)
        assert response.status_code == 204

    def test_wrong_code_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "fitbit_webhook_verify_code", SecretStr("code-ok"))
        with pytest.raises(HTTPException) as exc:
            _handler().handle_challenge(_request(query={"verify": "code-bad"}))
        assert exc.value.status_code == 404

    def test_unconfigured_code_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "fitbit_webhook_verify_code", None)
        with pytest.raises(HTTPException) as exc:
            _handler().handle_challenge(_request(query={"verify": "whatever"}))
        assert exc.value.status_code == 404


class TestParsePayload:
    def test_valid_array(self) -> None:
        parsed = _handler().parse_payload(json.dumps([_NOTIFICATION]).encode())
        assert len(parsed) == 1
        assert parsed[0].collection_type == "activities"
        assert parsed[0].owner_id == "FITBIT123"

    def test_non_array_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _handler().parse_payload(json.dumps(_NOTIFICATION).encode())
        assert exc.value.status_code == 400

    def test_malformed_json_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _handler().parse_payload(b"{not json")
        assert exc.value.status_code == 400


class TestDispatch:
    @patch("app.services.providers.fitbit.webhook_handler.store_raw_payload")
    @patch("app.services.providers.fitbit.webhook_handler.celery_app")
    def test_acks_204_and_enqueues(self, mock_celery: MagicMock, mock_store: MagicMock) -> None:
        handler = _handler()
        payload = handler.parse_payload(json.dumps([_NOTIFICATION]).encode())

        response = handler.dispatch(MagicMock(), payload)

        assert isinstance(response, Response)
        assert response.status_code == 204
        mock_store.assert_called_once()
        mock_celery.send_task.assert_called_once()
        task_args = mock_celery.send_task.call_args.kwargs["args"]
        assert task_args[0] == "fitbit"
        assert task_args[1]["notifications"][0]["ownerId"] == "FITBIT123"


class TestProcessPayload:
    def test_activities_notification_pulls_that_day(self) -> None:
        handler = _handler()
        connection = MagicMock()
        connection.user_id = uuid4()
        handler.connection_repo = MagicMock()
        handler.connection_repo.get_by_provider_user_id.return_value = connection

        result = handler.process_payload(MagicMock(), {"notifications": [_NOTIFICATION]}, "trace")

        assert result["processed"] == 1
        handler.workouts.load_data.assert_called_once()
        kwargs = handler.workouts.load_data.call_args.kwargs
        day = datetime(2026, 7, 1, tzinfo=timezone.utc)
        assert kwargs["start_date"] == day - timedelta(days=1)
        assert kwargs["end_date"] == day + timedelta(days=1)

    def test_unknown_owner_is_orphaned(self) -> None:
        handler = _handler()
        handler.connection_repo = MagicMock()
        handler.connection_repo.get_by_provider_user_id.return_value = None

        result = handler.process_payload(MagicMock(), {"notifications": [_NOTIFICATION]}, "trace")

        assert result["orphaned"] == 1
        handler.workouts.load_data.assert_not_called()

    def test_non_activity_collections_ignored(self) -> None:
        handler = _handler()
        sleep_notification = {**_NOTIFICATION, "collectionType": "sleep"}

        result = handler.process_payload(MagicMock(), {"notifications": [sleep_notification]}, "trace")

        assert result["ignored"] == 1
        handler.workouts.load_data.assert_not_called()


class TestEnsureUserSubscription:
    def test_creates_subscription(self) -> None:
        handler = _handler()
        user_id = uuid4()

        handler.ensure_user_subscription(MagicMock(), user_id)

        handler.workouts._make_api_request.assert_called_once()  # noqa: SLF001
        args = handler.workouts._make_api_request.call_args  # noqa: SLF001
        assert args[0][2] == f"/1/user/-/activities/apiSubscriptions/{user_id}.json"
        assert args.kwargs["method"] == "POST"

    def test_swallows_provider_errors(self) -> None:
        handler = _handler()
        handler.workouts._make_api_request.side_effect = RuntimeError("409 already exists")  # noqa: SLF001

        # Must not raise — a missed subscription degrades to polling.
        handler.ensure_user_subscription(MagicMock(), uuid4())

    def test_409_is_silent_success(self) -> None:
        handler = _handler()
        handler.workouts._make_api_request.side_effect = HTTPException(  # noqa: SLF001
            status_code=409, detail="already exists"
        )

        # Must not raise, and must not be reported as a failure.
        handler.ensure_user_subscription(MagicMock(), uuid4())


class TestVerifySignatureEmptySecret:
    def test_empty_string_secret_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty secret would make the signing key predictable ("&")."""
        monkeypatch.setattr(settings, "fitbit_client_secret", SecretStr(""))
        body = b"[]"
        request = _request(headers={"X-Fitbit-Signature": _sign(body, secret="")})
        assert _handler().verify_signature(request, body) is False


class TestProcessPayloadIsolation:
    def test_faulty_notification_does_not_abort_batch(self) -> None:
        """A bad date on one notification must not kill the rest of the batch."""
        handler = _handler()
        connection = MagicMock()
        connection.user_id = uuid4()
        handler.connection_repo = MagicMock()
        handler.connection_repo.get_by_provider_user_id.return_value = connection

        bad = {**_NOTIFICATION, "date": "not-a-date"}
        result = handler.process_payload(MagicMock(), {"notifications": [bad, _NOTIFICATION]}, "trace")

        assert result["failed"] == 1
        assert result["processed"] == 1
        handler.workouts.load_data.assert_called_once()
