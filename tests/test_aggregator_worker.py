"""Tests for the LogAggregatorWorker reader loop, copy, and config apply hook."""

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock

import pytest
from scietex.service import ValkeyWorker, ValkeyWorkerConfig

from scietex.log_aggregator_service.aggregator_worker import LogAggregatorWorker, _source_label
from scietex.log_aggregator_service.config import AGGREGATOR_SECTION, LogAggregatorSettings


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
async def test_reader_loop_retries_expire_until_stream_exists(tmp_path) -> None:
    """EXPIRE is retried each iteration until it succeeds (stream created)."""
    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    worker._settings = LogAggregatorSettings(source_services=["ModbusService"], ttl_seconds=3, scan_interval=0.0)
    client.scan.return_value = (b"0", [b"scietex:ModbusService:abc:log"])
    client.xread.return_value = None
    # First EXPIRE misses (stream absent), second succeeds.
    client.expire.side_effect = [False, True, True, True]

    task = asyncio.create_task(worker._reader_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert client.expire.await_count >= 2


@pytest.mark.asyncio
async def test_initialize_fails_on_bad_config(tmp_path, monkeypatch) -> None:
    """A bootstrap failure leaves settings unresolved, so initialize() returns False.

    In v6 the file is read by the bootstrap provider inside
    `seed_config_bootstrap()`; a `RuntimeError` there is caught by the framework,
    the section stays unresolved, and `current_config_settings` returns ``None``.
    """

    async def fake_initialize(self) -> bool:
        self.seed_config_bootstrap()
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    worker = _make_worker(tmp_path)
    monkeypatch.setattr(
        "scietex.log_aggregator_service.aggregator_worker.read_aggregator_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad config")),
    )

    assert await worker.initialize() is False
    assert worker._reader_task is None


@pytest.mark.asyncio
async def test_initialize_starts_reader_task(tmp_path, monkeypatch) -> None:
    """A successful initialize() creates the reader task and returns True."""

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    worker = _make_worker(tmp_path)
    monkeypatch.setattr(worker, "current_config_settings", lambda name: LogAggregatorSettings())

    assert await worker.initialize() is True
    assert worker._reader_task is not None
    assert worker._reader_task.get_name() == "LogAggregatorReader"

    await worker.cleanup()


@pytest.mark.asyncio
async def test_initialize_uses_merged_settings(tmp_path, monkeypatch) -> None:
    """initialize() stores the L0+L1 merged struct, not the raw file read."""

    async def fake_initialize(self) -> bool:
        self.seed_config_bootstrap()
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    worker = _make_worker(tmp_path)
    monkeypatch.setattr(
        "scietex.log_aggregator_service.aggregator_worker.read_aggregator_config",
        lambda *a, **k: {"max_len": 42},
    )

    assert await worker.initialize() is True
    assert worker.aggregator_settings.max_len == 42
    # A field absent from the patch keeps its L0 default.
    assert worker.aggregator_settings.target_stream == "scietex:log"

    await worker.cleanup()


def test_bootstrap_provider_returns_patch_dict(tmp_path, monkeypatch) -> None:
    """seed_config_bootstrap resolves a section whose bootstrap returns a partial dict.

    The bootstrap provider's return value is stored verbatim as the L1 patch and
    folded over the L0 base, so it must be a plain dict — proving the dict path
    end-to-end: a partial patch resolves with the untouched fields still at L0.
    """
    worker = _make_worker(tmp_path)
    monkeypatch.setattr(
        "scietex.log_aggregator_service.aggregator_worker.read_aggregator_config",
        lambda *a, **k: {"max_len": 42},
    )

    worker.seed_config_bootstrap()

    resolved = worker.current_config_settings(AGGREGATOR_SECTION)
    assert isinstance(resolved, LogAggregatorSettings)
    assert resolved.max_len == 42
    assert resolved.target_stream == "scietex:log"


@pytest.mark.asyncio
async def test_cleanup_cancels_running_reader_task(tmp_path, monkeypatch) -> None:
    """cleanup() cancels the reader task and clears the reference."""

    async def fake_initialize(self) -> bool:
        return True

    async def fake_cleanup(self) -> None:
        return None

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    monkeypatch.setattr(ValkeyWorker, "cleanup", fake_cleanup)
    worker = _make_worker(tmp_path)
    monkeypatch.setattr(worker, "current_config_settings", lambda name: LogAggregatorSettings())
    await worker.initialize()
    task = worker._reader_task
    assert task is not None

    await worker.cleanup()

    assert worker._reader_task is None
    assert task.cancelled()


@pytest.mark.asyncio
async def test_reader_loop_copies_entries_and_advances_last_ids(tmp_path) -> None:
    """The happy path reads entries, copies them, and advances last_ids."""
    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    worker._settings = LogAggregatorSettings(source_services=["ModbusService"], scan_interval=0.0)
    client.scan.return_value = (b"0", [b"scietex:ModbusService:abc:log"])
    client.xread.return_value = {
        b"scietex:ModbusService:abc:log": {b"1-0": [(b"message", b"boot")]},
    }

    async def yield_to_loop(*args, **kwargs):
        # The loop has no real I/O with mocked clients; yield so the test can
        # cancel it and so the tight loop does not starve the event loop.
        await asyncio.sleep(0.01)
        return client.xread.return_value

    client.xread.side_effect = yield_to_loop

    task = asyncio.create_task(worker._reader_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    client.xadd.assert_awaited()
    args, _ = client.xadd.await_args
    assert args[0] == "scietex:log"
    assert dict(args[1])["source"] == "ModbusService:abc"


@pytest.mark.asyncio
async def test_reader_loop_reports_glide_error_and_recovers(tmp_path, monkeypatch) -> None:
    """A GlideError is reported to TransportHealth and recover() is awaited."""
    from glide import RequestError

    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    worker._settings = LogAggregatorSettings(source_services=["ModbusService"], scan_interval=0.0)
    client.scan.return_value = (b"0", [b"scietex:ModbusService:abc:log"])
    client.xread.side_effect = RequestError("boom")
    report_failure = AsyncMock()
    recover = AsyncMock()
    monkeypatch.setattr(worker._health, "report_failure", report_failure)
    monkeypatch.setattr(worker._health, "recover", recover)

    task = asyncio.create_task(worker._reader_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    report_failure.assert_called()
    recover.assert_awaited()


@pytest.mark.asyncio
async def test_reader_loop_logs_unexpected_exception_and_continues(tmp_path, caplog) -> None:
    """A non-Glide exception is logged at exception level and the loop continues."""
    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    worker._settings = LogAggregatorSettings(source_services=["ModbusService"], scan_interval=0.0)
    client.scan.return_value = (b"0", [b"scietex:ModbusService:abc:log"])
    client.xread.side_effect = ValueError("unexpected")

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(worker._reader_loop())
        await asyncio.sleep(0.2)
        # The loop caught the error and is still running (not exited).
        assert not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert any("iteration failed" in record.message for record in caplog.records)
    assert client.xread.await_count >= 1


@pytest.mark.asyncio
async def test_reader_loop_rescans_when_settings_change(tmp_path) -> None:
    """A new settings object forces an immediate SCAN, bypassing the interval."""
    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    # A long scan interval: only the identity change can trigger a second SCAN.
    worker._settings = LogAggregatorSettings(source_services=["ModbusService"], scan_interval=3600.0)
    client.scan.return_value = (b"0", [b"scietex:ModbusService:abc:log"])

    async def yield_to_loop(*args, **kwargs):
        await asyncio.sleep(0.01)
        return None

    client.xread.side_effect = yield_to_loop

    task = asyncio.create_task(worker._reader_loop())
    await asyncio.sleep(0.1)
    assert client.scan.await_count == 1

    worker._settings = LogAggregatorSettings(source_services=["ModbusService"], scan_interval=3600.0)
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert client.scan.await_count == 2


@pytest.mark.asyncio
async def test_refresh_streams_follows_scan_cursor(tmp_path) -> None:
    """SCAN pagination is followed until the cursor returns to zero."""
    worker = _make_worker(tmp_path)
    client = AsyncMock()
    worker._client = client
    client.scan.side_effect = [
        (b"7", [b"scietex:ModbusService:a:log"]),
        (b"0", [b"scietex:ModbusService:b:log"]),
    ]
    settings = LogAggregatorSettings(source_services=["ModbusService"])
    last_ids: dict[str, str] = {}

    await worker._refresh_streams(settings, last_ids)

    assert client.scan.await_count == 2
    assert client.scan.await_args_list[0].args[0] == b"0"
    assert client.scan.await_args_list[1].args[0] == b"7"
    assert set(last_ids) == {"scietex:ModbusService:a:log", "scietex:ModbusService:b:log"}
