"""Tests for raw payload storage backends."""

import json
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services import raw_payload_storage


def _reset_module_globals() -> None:
    raw_payload_storage._storage_backend = "disabled"
    raw_payload_storage._max_size_bytes = 10 * 1024 * 1024
    raw_payload_storage._s3_bucket = None
    raw_payload_storage._s3_prefix = "raw-payloads"
    raw_payload_storage._s3_client = None
    raw_payload_storage._fit_files_enabled = False


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset module-level globals around each test.

    The after-test reset matters: S3-enabled tests leave a mocked client in
    place, and user deletion (test_user_service.py) calls purge_user_payloads,
    which would paginate that MagicMock forever (a mock IsTruncated is always
    truthy).
    """
    _reset_module_globals()
    yield
    _reset_module_globals()


class TestConfigure:
    def test_configure_disabled(self) -> None:
        raw_payload_storage.configure("disabled", 1024)
        assert raw_payload_storage._storage_backend == "disabled"

    def test_configure_log(self) -> None:
        raw_payload_storage.configure("log", 2048)
        assert raw_payload_storage._storage_backend == "log"
        assert raw_payload_storage._max_size_bytes == 2048

    def test_configure_s3_without_bucket_falls_back_to_disabled(self) -> None:
        raw_payload_storage.configure("s3", 1024, s3_bucket=None)
        assert raw_payload_storage._storage_backend == "disabled"

    def test_configure_s3_with_bucket(self) -> None:
        mock_client = MagicMock()
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="my-bucket", s3_prefix="payloads")

        assert raw_payload_storage._storage_backend == "s3"
        assert raw_payload_storage._s3_bucket == "my-bucket"
        assert raw_payload_storage._s3_prefix == "payloads"
        assert raw_payload_storage._s3_client is mock_client

    def test_configure_transport_creates_a_client_without_archival(self) -> None:
        """Offload needs a usable bucket, not RAW_PAYLOAD_STORAGE=s3."""
        mock_client = MagicMock()
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("disabled", 1024, s3_bucket="my-bucket", transport_enabled=True)

        assert raw_payload_storage._storage_backend == "disabled"
        assert raw_payload_storage._s3_client is mock_client
        assert raw_payload_storage._s3_bucket == "my-bucket"

    def test_configure_s3_client_creation_fails(self) -> None:
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=None):
            raw_payload_storage.configure("s3", 1024, s3_bucket="my-bucket")

        assert raw_payload_storage._storage_backend == "disabled"


class TestStoreRawPayload:
    def test_disabled_is_noop(self) -> None:
        raw_payload_storage.configure("disabled", 1024)
        # Should not raise
        raw_payload_storage.store_raw_payload(source="webhook", provider="garmin", payload={"key": "val"})

    def test_payload_exceeding_max_size_is_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        raw_payload_storage.configure("log", 10)  # 10 bytes max
        raw_payload_storage.store_raw_payload(source="webhook", provider="garmin", payload={"big": "payload"})
        captured = capsys.readouterr()
        assert "raw_payload" not in captured.out

    def test_log_backend_outputs_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        raw_payload_storage.configure("log", 10 * 1024 * 1024)
        raw_payload_storage.store_raw_payload(
            source="webhook",
            provider="garmin",
            payload={"test": True},
            user_id="user-123",
            trace_id="trace-abc",
        )
        captured = capsys.readouterr()
        entry = json.loads(captured.out.strip())
        assert entry["message"] == "raw_payload"
        assert entry["source"] == "webhook"
        assert entry["provider"] == "garmin"
        assert entry["user_id"] == "user-123"
        assert entry["trace_id"] == "trace-abc"

    def test_s3_backend_uploads_payload(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.return_value = {"ETag": "test-etag"}

        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 10 * 1024 * 1024, s3_bucket="test-bucket", s3_prefix="raw")

        raw_payload_storage.store_raw_payload(
            source="webhook",
            provider="garmin",
            payload={"activity": "running"},
            user_id="user-456",
            trace_id="trace-xyz",
        )

        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert "garmin/webhook/" in call_kwargs["Key"]
        assert "/user-456/" in call_kwargs["Key"]
        assert call_kwargs["Key"].endswith(".json")
        assert call_kwargs["ContentType"] == "application/json"
        assert call_kwargs["Metadata"]["provider"] == "garmin"
        assert call_kwargs["Metadata"]["user_id"] == "user-456"
        assert call_kwargs["Metadata"]["trace_id"] == "trace-xyz"

        body = call_kwargs["Body"].decode("utf-8")
        assert json.loads(body) == {"activity": "running"}

    def test_s3_backend_handles_upload_error(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.side_effect = Exception("S3 error")

        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 10 * 1024 * 1024, s3_bucket="test-bucket")

        # Should not raise - errors are logged
        raw_payload_storage.store_raw_payload(source="sdk", provider="apple", payload="raw-xml-data")

    def test_s3_backend_unknown_user_fallback(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.return_value = {"ETag": "test-etag"}

        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 10 * 1024 * 1024, s3_bucket="test-bucket")

        raw_payload_storage.store_raw_payload(source="webhook", provider="garmin", payload={"x": 1})

        call_kwargs = mock_client.put_object.call_args[1]
        assert "/_unknown/" in call_kwargs["Key"]

    def test_s3_backend_pre_serialized_string(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.return_value = {"ETag": "test-etag"}

        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 10 * 1024 * 1024, s3_bucket="test-bucket")

        raw_payload_storage.store_raw_payload(source="sdk", provider="apple", payload='{"pre":"serialized"}')

        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Body"] == b'{"pre":"serialized"}'


def _pages_by_prefix(raw_pages: list[dict], fit_pages: list[dict] | None = None) -> Callable[..., dict]:
    """Dispatch list_objects_v2 pages per Prefix (raw payloads vs fit-files).

    The last queued page repeats when the client keeps paginating, so a
    single-page setup only needs one entry.
    """
    queues = {
        "raw": list(raw_pages),
        "fit": list(fit_pages) if fit_pages else [{"IsTruncated": False}],
    }

    def _list(**kwargs: Any) -> dict:
        queue = queues["fit"] if kwargs["Prefix"] == "fit-files/" else queues["raw"]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return _list


class TestPurgeUserPayloads:
    def test_disabled_is_noop(self) -> None:
        raw_payload_storage.configure("disabled", 1024)
        assert raw_payload_storage.purge_user_payloads("user-1") == 0

    def test_purges_only_matching_user_keys(self) -> None:
        mock_client = MagicMock()
        mock_client.list_objects_v2.side_effect = _pages_by_prefix(
            [
                {
                    "Contents": [
                        {"Key": "raw-payloads/garmin/webhook/2026-06-01/user-1/aaa.json"},
                        {"Key": "raw-payloads/strava/webhook/2026-06-02/user-2/bbb.json"},
                        {"Key": "raw-payloads/garmin/api/2026-06-03/user-1/ccc.json"},
                    ],
                    "IsTruncated": False,
                }
            ]
        )
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="bucket")

        deleted = raw_payload_storage.purge_user_payloads("user-1")

        assert deleted == 2
        mock_client.delete_objects.assert_called_once_with(
            Bucket="bucket",
            Delete={
                "Objects": [
                    {"Key": "raw-payloads/garmin/webhook/2026-06-01/user-1/aaa.json"},
                    {"Key": "raw-payloads/garmin/api/2026-06-03/user-1/ccc.json"},
                ],
                "Quiet": True,
            },
        )

    def test_paginates_until_not_truncated(self) -> None:
        mock_client = MagicMock()
        mock_client.list_objects_v2.side_effect = _pages_by_prefix(
            [
                {
                    "Contents": [{"Key": "raw-payloads/garmin/webhook/2026-06-01/user-1/aaa.json"}],
                    "IsTruncated": True,
                    "NextContinuationToken": "token-2",
                },
                {
                    "Contents": [{"Key": "raw-payloads/garmin/webhook/2026-06-02/user-1/bbb.json"}],
                    "IsTruncated": False,
                },
            ]
        )
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="bucket")

        deleted = raw_payload_storage.purge_user_payloads("user-1")

        assert deleted == 2
        # 2 pages sur le préfixe raw-payloads + 1 listing fit-files/, émis
        # dès que le client S3 existe, même si fit_files_enabled est False.
        assert mock_client.list_objects_v2.call_count == 3
        assert mock_client.delete_objects.call_count == 2
        # La pagination doit reprendre avec le token de la première page.
        second_call_kwargs = mock_client.list_objects_v2.call_args_list[1].kwargs
        assert second_call_kwargs["ContinuationToken"] == "token-2"

    def test_empty_prefix_is_idempotent(self) -> None:
        mock_client = MagicMock()
        mock_client.list_objects_v2.return_value = {"IsTruncated": False}
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="bucket")

        assert raw_payload_storage.purge_user_payloads("user-1") == 0
        mock_client.delete_objects.assert_not_called()

    def test_partial_delete_failures_are_not_counted(self) -> None:
        mock_client = MagicMock()
        mock_client.list_objects_v2.side_effect = _pages_by_prefix(
            [
                {
                    "Contents": [
                        {"Key": "raw-payloads/garmin/webhook/2026-06-01/user-1/aaa.json"},
                        {"Key": "raw-payloads/garmin/webhook/2026-06-02/user-1/bbb.json"},
                    ],
                    "IsTruncated": False,
                }
            ]
        )
        mock_client.delete_objects.return_value = {
            "Errors": [
                {"Key": "raw-payloads/garmin/webhook/2026-06-02/user-1/bbb.json", "Message": "AccessDenied"},
            ],
        }
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="bucket")

        # Quiet mode renvoie uniquement les échecs : ils ne comptent pas
        # comme purgés.
        assert raw_payload_storage.purge_user_payloads("user-1") == 1

    def test_purges_fit_files_prefix(self) -> None:
        """store_fit_file writes under fit-files/, outside _s3_prefix: those
        objects embed the user id too and must not survive the GDPR purge."""
        mock_client = MagicMock()
        mock_client.list_objects_v2.side_effect = _pages_by_prefix(
            [{"IsTruncated": False}],
            fit_pages=[
                {
                    "Contents": [
                        {"Key": "fit-files/garmin/2026-01-01/user-1/activity-1.fit"},
                        {"Key": "fit-files/garmin/2026-01-02/user-2/activity-2.fit"},
                        {"Key": "fit-files/fitbit/2026-01-03/user-1/activity-3.fit"},
                    ],
                    "IsTruncated": False,
                }
            ],
        )
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="bucket", fit_files_enabled=True)

        deleted = raw_payload_storage.purge_user_payloads("user-1")

        assert deleted == 2
        mock_client.delete_objects.assert_called_once_with(
            Bucket="bucket",
            Delete={
                "Objects": [
                    {"Key": "fit-files/garmin/2026-01-01/user-1/activity-1.fit"},
                    {"Key": "fit-files/fitbit/2026-01-03/user-1/activity-3.fit"},
                ],
                "Quiet": True,
            },
        )

    def test_purges_both_prefixes_and_counts_them_together(self) -> None:
        mock_client = MagicMock()
        mock_client.list_objects_v2.side_effect = _pages_by_prefix(
            [
                {
                    "Contents": [{"Key": "raw-payloads/garmin/webhook/2026-06-01/user-1/aaa.json"}],
                    "IsTruncated": False,
                }
            ],
            fit_pages=[
                {
                    "Contents": [{"Key": "fit-files/garmin/2026-01-01/user-1/activity-1.fit"}],
                    "IsTruncated": False,
                }
            ],
        )
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="bucket", fit_files_enabled=True)

        assert raw_payload_storage.purge_user_payloads("user-1") == 2
        assert mock_client.delete_objects.call_count == 2

    def test_purges_fit_files_when_raw_payload_archival_disabled(self) -> None:
        """STORE_FIT_FILES=true without RAW_PAYLOAD_STORAGE=s3: the raw-payload
        backend guard must not short-circuit the fit-files purge (GDPR)."""
        mock_client = MagicMock()
        mock_client.list_objects_v2.return_value = {
            "Contents": [{"Key": "fit-files/garmin/2026-01-01/user-1/activity-1.fit"}],
            "IsTruncated": False,
        }
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("disabled", 1024, s3_bucket="bucket", fit_files_enabled=True)

        deleted = raw_payload_storage.purge_user_payloads("user-1")

        assert deleted == 1
        mock_client.list_objects_v2.assert_called_once()
        assert mock_client.list_objects_v2.call_args.kwargs["Prefix"] == "fit-files/"
        mock_client.delete_objects.assert_called_once_with(
            Bucket="bucket",
            Delete={
                "Objects": [{"Key": "fit-files/garmin/2026-01-01/user-1/activity-1.fit"}],
                "Quiet": True,
            },
        )

    def test_purges_fit_files_even_when_fit_files_flag_disabled(self) -> None:
        """STORE_FIT_FILES flipped off after files were written: the purge
        must not depend on the runtime flag, or orphaned .fit files survive
        the GDPR erasure. fit-files/ is listed whenever an S3 client exists."""
        mock_client = MagicMock()
        mock_client.list_objects_v2.side_effect = _pages_by_prefix(
            [{"IsTruncated": False}],
            fit_pages=[
                {
                    "Contents": [{"Key": "fit-files/garmin/2026-01-01/user-1/activity-1.fit"}],
                    "IsTruncated": False,
                }
            ],
        )
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="bucket", fit_files_enabled=False)

        deleted = raw_payload_storage.purge_user_payloads("user-1")

        assert deleted == 1
        listed_prefixes = [call.kwargs["Prefix"] for call in mock_client.list_objects_v2.call_args_list]
        assert "fit-files/" in listed_prefixes
        mock_client.delete_objects.assert_called_once_with(
            Bucket="bucket",
            Delete={
                "Objects": [{"Key": "fit-files/garmin/2026-01-01/user-1/activity-1.fit"}],
                "Quiet": True,
            },
        )


def _configure_s3(
    bucket: str = "test-bucket",
    backend: str = "s3",
    max_size: int = 1024,
    s3_prefix: str = "raw-payloads",
    transport_enabled: bool = False,
) -> MagicMock:
    """Configure the module with a mocked S3 client and return it."""
    mock_client = MagicMock()
    with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
        raw_payload_storage.configure(
            backend,
            max_size,
            s3_bucket=bucket,
            s3_prefix=s3_prefix,
            transport_enabled=transport_enabled,
        )
    return mock_client


class TestPutPayloadToS3:
    """The transport write: no archival policy, always uploads, returns a reference."""

    def test_returns_an_s3_reference(self) -> None:
        mock_client = _configure_s3(s3_prefix="raw")

        ref = raw_payload_storage.put_payload_to_s3(
            source="sdk", provider="apple", payload='{"a":1}', user_id="user-1", trace_id="batch-1"
        )

        key = mock_client.put_object.call_args[1]["Key"]
        assert ref == f"s3://test-bucket/{key}"
        assert key.startswith("raw/apple/sdk/")
        assert "/user-1/" in key

    def test_ignores_the_archival_size_limit(self) -> None:
        """The limit caps what is worth archiving, not what gets processed."""
        mock_client = _configure_s3(max_size=10)

        ref = raw_payload_storage.put_payload_to_s3(source="sdk", provider="apple", payload="x" * 500)

        assert ref is not None
        mock_client.put_object.assert_called_once()

    def test_works_with_archival_disabled(self) -> None:
        mock_client = _configure_s3(backend="disabled", transport_enabled=True)

        ref = raw_payload_storage.put_payload_to_s3(source="sdk", provider="apple", payload='{"a":1}')

        assert ref is not None
        assert raw_payload_storage._storage_backend == "disabled"
        mock_client.put_object.assert_called_once()

    def test_size_metadata_counts_encoded_bytes(self) -> None:
        mock_client = _configure_s3()

        raw_payload_storage.put_payload_to_s3(source="sdk", provider="apple", payload="żółw")

        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Body"] == "żółw".encode()
        assert call_kwargs["Metadata"]["size_bytes"] == "7"

    def test_returns_none_without_a_client(self) -> None:
        assert raw_payload_storage.put_payload_to_s3(source="sdk", provider="apple", payload="{}") is None

    def test_returns_none_on_upload_failure(self) -> None:
        mock_client = _configure_s3()
        mock_client.put_object.side_effect = Exception("S3 error")

        assert raw_payload_storage.put_payload_to_s3(source="sdk", provider="apple", payload="{}") is None


class TestGetPayloadFromS3:
    def test_takes_bucket_and_key_from_the_reference(self) -> None:
        """The ref wins over local config, so app/worker drift is not a silent 404."""
        mock_client = _configure_s3(bucket="configured-bucket")
        mock_client.get_object.return_value = {"Body": MagicMock(read=lambda: b'{"a":1}')}

        content = raw_payload_storage.get_payload_from_s3("s3://other-bucket/raw/apple/sdk/x.json")

        assert content == '{"a":1}'
        mock_client.get_object.assert_called_once_with(Bucket="other-bucket", Key="raw/apple/sdk/x.json")

    def test_raises_without_a_client(self) -> None:
        with pytest.raises(RuntimeError, match="S3 client not configured"):
            raw_payload_storage.get_payload_from_s3("s3://bucket/key.json")

    @pytest.mark.parametrize("ref", ["s3://bucket", "s3://"])
    def test_raises_on_a_malformed_reference(self, ref: str) -> None:
        _configure_s3()

        with pytest.raises(ValueError, match="Malformed"):
            raw_payload_storage.get_payload_from_s3(ref)

    @pytest.mark.parametrize("ref", ["raw-payloads/apple/sdk/x.json", "bucket-only", ""])
    def test_rejects_a_reference_without_the_scheme(self, ref: str) -> None:
        """A bare key would otherwise split into a bucket named after our own prefix."""
        mock_client = _configure_s3()

        with pytest.raises(ValueError, match="must be an s3:// URI"):
            raw_payload_storage.get_payload_from_s3(ref)

        mock_client.get_object.assert_not_called()


class TestDeletePayloadFromS3:
    def test_deletes_using_the_reference(self) -> None:
        mock_client = _configure_s3()

        raw_payload_storage.delete_payload_from_s3("s3://other-bucket/raw/x.json")

        mock_client.delete_object.assert_called_once_with(Bucket="other-bucket", Key="raw/x.json")

    def test_is_a_noop_without_a_client(self) -> None:
        raw_payload_storage.delete_payload_from_s3("s3://bucket/key.json")

    def test_swallows_delete_failures(self) -> None:
        """A bucket policy without s3:DeleteObject must never fail the batch."""
        mock_client = _configure_s3()
        mock_client.delete_object.side_effect = Exception("AccessDenied")

        raw_payload_storage.delete_payload_from_s3("s3://test-bucket/raw/x.json")

    def test_swallows_a_malformed_reference(self) -> None:
        mock_client = _configure_s3()

        raw_payload_storage.delete_payload_from_s3("nonsense")

        mock_client.delete_object.assert_not_called()
