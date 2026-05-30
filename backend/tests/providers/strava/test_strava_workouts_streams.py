"""Unit tests for StravaWorkouts.get_workout_streams_from_api (live passthrough)."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.providers.strava.workouts import DEFAULT_STREAM_KEYS, StravaWorkouts


def _make_strava_workouts() -> StravaWorkouts:
    return StravaWorkouts(
        workout_repo=MagicMock(),
        connection_repo=MagicMock(),
        provider_name="strava",
        api_base_url="https://www.strava.com",
        oauth=MagicMock(),
    )


def test_get_workout_streams_from_api_uses_default_keys() -> None:
    workouts = _make_strava_workouts()
    db = MagicMock()
    user_id = uuid4()
    payload = {"heartrate": {"data": [120, 121], "series_type": "distance"}}

    with patch.object(workouts, "_make_api_request", return_value=payload) as mock_req:
        result = workouts.get_workout_streams_from_api(db, user_id, "12345")

    mock_req.assert_called_once_with(
        db,
        user_id,
        "/api/v3/activities/12345/streams",
        params={
            "keys": DEFAULT_STREAM_KEYS,
            "key_by_type": "true",
        },
    )
    assert result == payload


def test_get_workout_streams_from_api_accepts_custom_keys() -> None:
    workouts = _make_strava_workouts()
    db = MagicMock()
    user_id = uuid4()

    with patch.object(workouts, "_make_api_request", return_value={}) as mock_req:
        workouts.get_workout_streams_from_api(db, user_id, "999", keys="heartrate,watts")

    mock_req.assert_called_once_with(
        db,
        user_id,
        "/api/v3/activities/999/streams",
        params={"keys": "heartrate,watts", "key_by_type": "true"},
    )


def test_get_workout_streams_resolves_ow_uuid_to_external_id() -> None:
    """An OW internal event id (UUID) is resolved to the Strava activity id."""
    workouts = _make_strava_workouts()
    db = MagicMock()
    user_id = uuid4()
    ow_uuid = "6dfb515f-1ed8-41b5-833d-5c791e16ca51"
    record = MagicMock()
    record.external_id = "18678971482"
    workouts.workout_repo.get.return_value = record

    with patch.object(workouts, "_make_api_request", return_value={}) as mock_req:
        workouts.get_workout_streams_from_api(db, user_id, ow_uuid)

    assert mock_req.call_args[0][2] == "/api/v3/activities/18678971482/streams"


def test_get_workout_streams_native_id_passes_through() -> None:
    """A numeric (non-UUID) activity id is used as-is; no DB lookup happens."""
    workouts = _make_strava_workouts()
    db = MagicMock()
    user_id = uuid4()

    with patch.object(workouts, "_make_api_request", return_value={}) as mock_req:
        workouts.get_workout_streams_from_api(db, user_id, "18678971482")

    assert mock_req.call_args[0][2] == "/api/v3/activities/18678971482/streams"
    workouts.workout_repo.get.assert_not_called()
