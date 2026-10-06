"""Tests for the LogAggregatorWorker reader loop, copy, and config apply hook."""

import logging
from unittest.mock import AsyncMock

import pytest
from scietex.service import ValkeyWorker, ValkeyWorkerConfig

from scietex.log_aggregator_service.aggregator_worker import LogAggregatorWorker, _source_label
from scietex.log_aggregator_service.config import LogAggregatorSettings


def _make_worker(tmp_path) -> LogAggregatorWorker:
    """Build a worker with a real conf_dir and no remote-config handlers."""
    return LogAggregatorWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=False,
        )
    )


def test_source_label_derives_service_and_instance() -> None:
    """A per-worker stream key maps to ``{service}:{instance_id}``."""
    assert _source_label("scietex:modbus:abc123:log") == "modbus:abc123"


def test_source_label_passes_through_unexpected_keys() -> None:
    """A key that does not match the expected shape is returned unchanged."""
    assert _source_label("scietex:log") == "scietex:log"
    assert _source_label("other:modbus:abc:log") == "other:modbus:abc:log"


@pytest.mark.asyncio
async def test_cleanup_safe_when_never_started(tmp_path) -> None:
    """cleanup() is a no-op when the reader loop was never started."""
    worker = _make_worker(tmp_path)

    await worker.cleanup()

    assert worker.aggregator_settings is None


def test_apply_hook_stores_settings(tmp_path, caplog) -> None:
    """The apply hook stores the settings and logs the source count."""
    worker = _make_worker(tmp_path)
    settings = LogAggregatorSettings(source_services=["ModbusService"])

    with caplog.at_level(logging.INFO):
        worker._apply_settings(settings)

    assert worker.aggregator_settings is settings
    assert any("settings applied" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_copy_stamps_source_and_applies_maxlen(tmp_path) -> None:
    """_copy decodes fields, stamps source, and passes MAXLEN on the write."""
    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    settings = LogAggregatorSettings(max_len=500)

    await worker._copy(
        settings,
        "scietex:modbus:abc123:log",
        [(b"level", b"INF"), (b"message", b"boot"), (b"name", b"modbus")],
    )

    client.xadd.assert_awaited_once()
    args, kwargs = client.xadd.await_args
    assert args[0] == "scietex:log"
    fields = dict(args[1])
    assert fields["source"] == "modbus:abc123"
    assert fields["message"] == "boot"
    trim = kwargs["options"].trim
    assert trim.threshold == 500


@pytest.mark.asyncio
async def test_copy_omits_trim_when_maxlen_disabled(tmp_path) -> None:
    """A max_len of 0 leaves the stream unbounded (no trim option)."""
    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    settings = LogAggregatorSettings(max_len=0)

    await worker._copy(settings, "scietex:modbus:abc123:log", [(b"message", b"boot")])

    _, kwargs = client.xadd.await_args
    assert kwargs["options"] is None


@pytest.mark.asyncio
async def test_refresh_streams_prunes_vanished_and_excludes_self(tmp_path) -> None:
    """SCAN results replace the source set; excluded services are skipped."""
    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    client.scan.return_value = (b"0", [b"scietex:modbus:abc123:log"])
    settings = LogAggregatorSettings(
        source_services=["ModbusService", "LogAggregatorService"],
        exclude_services=["LogAggregatorService"],
    )
    last_ids = {"scietex:modbus:gone:log": "5-0"}

    await worker._refresh_streams(settings, last_ids)

    assert last_ids == {"scietex:modbus:abc123:log": "0"}
    # Only the non-excluded service is scanned.
    assert client.scan.await_count == 1
    assert client.scan.await_args.kwargs["match"] == "scietex:ModbusService:*:log"


@pytest.mark.asyncio
async def test_initialize_fails_on_bad_config(tmp_path, monkeypatch) -> None:
    """A config load failure returns False without starting the reader loop."""

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    worker = _make_worker(tmp_path)
    monkeypatch.setattr(
        "scietex.log_aggregator_service.aggregator_worker.read_aggregator_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad config")),
    )

    assert await worker.initialize() is False
    assert worker._reader_task is None
