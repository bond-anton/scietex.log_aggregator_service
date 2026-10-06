"""Log aggregator worker: a Valkey-backed service that merges worker log streams.

`LogAggregatorWorker` extends `ValkeyWorker` and runs a single supervisor loop
that tails every per-worker log stream of the configured source services and
copies each entry into one shared stream. It is the sole writer of that stream,
so it owns both the ``MAXLEN`` trim and the whole-key ``EXPIRE``.

The loop is a single task (not one task per stream): a multi-key ``XREAD``
merges all sources naturally, and a periodic ``SCAN`` picks up workers that
appear or disappear. Each copied entry is stamped with ``source`` =
``{service}:{instance_id}``, derived from the stream key, so consumers can tell
which worker emitted it. The aggregator's own service is excluded from the
source set to prevent its logs from looping back into the stream it writes.
"""

import asyncio
import contextlib
import time
from collections.abc import Mapping
from typing import Any, cast

from glide import StreamAddOptions, StreamReadOptions, TrimByMaxLen
from scietex.service import ValkeyWorker, ValkeyWorkerConfig

from .config import AGGREGATOR_SECTION, LogAggregatorSettings, read_aggregator_config

#: Suffix of a per-worker log stream key: ``scietex:{service}:{instance_id}:log``.
_LOG_STREAM_SUFFIX: str = ":log"


class LogAggregatorWorker(ValkeyWorker):
    """A `ValkeyWorker` that aggregates per-worker log streams into one stream.

    Settings precedence: constructor default < ``log_aggregator.yml`` < framework
    ``config.yml`` / remote ``log_aggregator`` section. The reader loop starts
    after `super().initialize()` so the remote apply (which runs the
    ``log_aggregator`` section hook) has already updated ``self._settings``.
    """

    def __init__(self, config: ValkeyWorkerConfig | None = None, *, client_factory=None, theme=None) -> None:
        super().__init__(config, client_factory=client_factory, theme=theme)
        self._settings: LogAggregatorSettings | None = None
        self._reader_task: asyncio.Task | None = None
        self.register_config_settings(AGGREGATOR_SECTION, LogAggregatorSettings, apply=self._apply_settings)

    @property
    def aggregator_settings(self) -> LogAggregatorSettings | None:
        """The effective aggregator settings, or `None` before startup."""
        return self._settings

    async def initialize(self) -> bool:
        """Load settings, initialize the framework, then start the reader loop.

        The reader loop is started only after `super().initialize()` succeeds, so
        the remote config apply has already run and ``self._settings`` reflects
        the effective configuration.

        Returns:
            `True` if the framework initialized and the reader loop started;
            `False` on any configuration or startup failure.
        """
        try:
            self._settings = read_aggregator_config(self.conf_dir)
        except RuntimeError as exc:
            self.logger.error("Failed to load log aggregator configuration: %s", exc)
            return False

        if not await super().initialize():
            return False

        self._reader_task = asyncio.create_task(self._reader_loop(), name="LogAggregatorReader")
        return True

    async def cleanup(self) -> None:
        """Cancel the reader loop, then tear down the framework resources."""
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await super().cleanup()

    def _apply_settings(self, settings: LogAggregatorSettings) -> None:
        """Store remote aggregator settings; the reader loop picks them up.

        The loop detects the settings object identity change and forces a SCAN,
        so a changed ``source_services`` set is reflected without restarting the
        task. ``max_len`` and ``ttl_seconds`` apply on the next iteration.
        """
        self._settings = settings
        self.logger.info(
            "Log aggregator settings applied: %d source service(s), target=%s",
            len(settings.source_services),
            settings.target_stream,
        )

    async def _reader_loop(self) -> None:
        """Tail every source stream and copy entries into the target stream.

        A single loop owns all sources: ``last_ids`` maps each source stream key
        to the last entry id read, and a multi-key ``XREAD`` merges them. The
        source set is refreshed by SCAN on a timer (and immediately when the
        settings object changes), so workers appearing or disappearing are
        handled without per-stream tasks. Any iteration error is logged and the
        loop continues; only cancellation ends it.
        """
        last_ids: dict[str, str] = {}
        settings_seen: LogAggregatorSettings | None = None
        last_scan = 0.0
        last_expire = 0.0
        while True:
            try:
                settings = self._settings
                client = self.client
                if settings is None or client is None:
                    await asyncio.sleep(1.0)
                    continue

                now = time.monotonic()
                if settings is not settings_seen or now - last_scan >= settings.scan_interval:
                    await self._refresh_streams(settings, last_ids)
                    last_scan = now
                    settings_seen = settings

                if settings.ttl_seconds > 0 and now - last_expire >= max(1.0, settings.ttl_seconds / 3):
                    await client.expire(settings.target_stream, settings.ttl_seconds)
                    last_expire = now

                if not last_ids:
                    await asyncio.sleep(1.0)
                    continue

                entries = await client.xread(
                    keys_and_ids=cast("Mapping[str | bytes, str | bytes]", last_ids),
                    options=StreamReadOptions(count=settings.batch_size, block_ms=settings.block_ms),
                )
                for key_bytes, group in (entries or {}).items():
                    key = key_bytes.decode() if isinstance(key_bytes, bytes) else str(key_bytes)
                    for entry_id, fields in group.items():
                        await self._copy(settings, key, fields)
                        last_ids[key] = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Log aggregation iteration failed; continuing")
                await asyncio.sleep(1.0)

    async def _refresh_streams(self, settings: LogAggregatorSettings, last_ids: dict[str, str]) -> None:
        """Rebuild ``last_ids`` from a SCAN of the configured source services.

        Each source service contributes keys matching ``scietex:{service}:*:log``.
        Keys that vanished are dropped; new keys start from ``"0"`` so their
        backlog is copied. The dict is mutated in place so the reader loop keeps
        a single reference.
        """
        client = self.client
        if client is None:
            return
        excluded = set(settings.exclude_services)
        found: set[str] = set()
        for service in settings.source_services:
            if service in excluded:
                continue
            cursor: bytes = b"0"
            while True:
                result = await client.scan(cursor, match=f"scietex:{service}:*{_LOG_STREAM_SUFFIX}", count=100)
                cursor = cast(bytes, result[0])
                for name in cast("list[bytes]", result[1]):
                    found.add(name.decode() if isinstance(name, bytes) else str(name))
                if cursor == b"0":
                    break
        for key in list(last_ids):
            if key not in found:
                del last_ids[key]
        for key in found:
            last_ids.setdefault(key, "0")

    async def _copy(self, settings: LogAggregatorSettings, source_key: str, fields: Any) -> None:
        """Copy one source entry into the target stream, stamped with ``source``.

        ``fields`` is the Glide ``list[tuple[bytes, bytes]]`` payload. The
        ``source`` label is derived from the stream key
        (``scietex:a:1:log`` -> ``a:1``) and overrides any existing field, so a
        consumer always sees the emitting worker. ``MAXLEN ~`` is applied on the
        write when ``max_len > 0``.
        """
        client = self.client
        if client is None:
            return
        decoded: dict[str, str] = {}
        for pair in fields:
            name = pair[0].decode() if isinstance(pair[0], bytes) else str(pair[0])
            value = pair[1].decode() if isinstance(pair[1], bytes) else str(pair[1])
            decoded[name] = value
        decoded["source"] = _source_label(source_key)
        options = (
            StreamAddOptions(trim=TrimByMaxLen(exact=False, threshold=settings.max_len, limit=None))
            if settings.max_len > 0
            else None
        )
        await client.xadd(settings.target_stream, list(decoded.items()), options=options)


def _source_label(stream_key: str) -> str:
    """Derive ``{service}:{instance_id}`` from a per-worker log stream key.

    ``scietex:{service}:{instance_id}:log`` -> ``{service}:{instance_id}``. A key
    that does not match the expected shape is returned unchanged, so a
    misconfigured source still produces a usable label.
    """
    parts = stream_key.split(":")
    if len(parts) == 4 and parts[0] == "scietex" and parts[3] == "log":
        return f"{parts[1]}:{parts[2]}"
    return stream_key
