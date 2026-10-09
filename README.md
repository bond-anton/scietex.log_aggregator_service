# scietex.log_aggregator_service

**scietex.log_aggregator_service** is a `scietex.service` worker that merges the
per-worker log streams of the configured services into one shared stream. Each
`scietex.service` worker writes its own stream
(`scietex:{service}:{instance_id}:log`); this service tails all of them and
copies every entry into a single target stream (`scietex:log` by default),
stamping each entry with a `source` field (`{service}:{instance_id}`) so
consumers can tell which worker emitted it.

The aggregator is the **sole writer** of the target stream, so it owns both the
`MAXLEN ~` trim and the whole-key `EXPIRE`. The API backend reads that stream
for the system log viewer and no longer trims it.

**Python ≥ 3.10** · **License: MIT**

## How it works

```
per-worker log streams                     shared stream                viewer
scietex:{svc}:{inst}:log  ─┐
scietex:{svc2}:{inst}:log  ─┤  XREAD (multi-key)   ┌────────────────┐   GET /logs/          ─► LogViewer
scietex:{svcN}:{inst}:log  ─┘ ───────────────►    │ scietex:log    │   GET /logs/stream (SSE)   (Source col)
                                                   │ XADD MAXLEN ~  │
   ▲ each writer owns its stream                   │ EXPIRE ttl     │
   │ (worker heartbeat refreshes its TTL)          └────────────────┘
                                                   ▲ single owner
                                   ┌───────────────┴────────────────┐
                                   │ LogAggregatorService worker     │
                                   │ (reads scietex:{agg}:config)    │
                                   └─────────────────────────────────┘
```

- A **single supervisor loop** (not one task per stream) tails every source with
  a multi-key `XREAD`, so the merge is natural and there is no task fan-out.
- A periodic `SCAN` (`scietex:{service}:*:log`) picks up workers that appear or
  disappear; the source set is rebuilt in place.
- The target stream's TTL is refreshed on a timer (Valkey `EXPIRE` sets a TTL on
  the key; `XADD` does not refresh it).
- The aggregator's own service is excluded from the source set, so its logs do
  not loop back into the stream it writes.

## Installation

```bash
pip install scietex.log_aggregator_service
```

This pulls in `scietex.service[valkey]` (the worker framework and its Valkey
transport).

## Quick start

### 1. Write a configuration file

The service reads `log_aggregator.yml` from a `log_aggregator/` subdirectory of
its config directory. Create it by hand, or let the service generate defaults on
first run. The file is the bootstrap layer of a four-layer merge: constructor
defaults < `log_aggregator.yml` < the framework's `config.yml` snapshot < the
remote `log_aggregator` section. Each layer is a field-level patch — a key absent
from a layer inherits the layer below, `null` clears it back to the constructor
default, and a value sets it:

```yaml
source_services:
  - ModbusService
exclude_services:
  - LogAggregatorService
target_stream: scietex:log
max_len: 100000
ttl_seconds: 604800
batch_size: 200
block_ms: 1000
scan_interval: 15.0
```

### 2. Run the service

```bash
start-log-aggregator --conf-dir /etc/scietex
```

The service resolves its config directory automatically when `--conf-dir` is
omitted. It runs in the foreground until it receives `SIGINT` or `SIGTERM`.

### 3. Register it with the backend

The backend is the config authority: register the service so it can deliver the
`log_aggregator` section (source services, target stream, `max_len`, `ttl`):

```bash
curl -X POST http://localhost:8000/api/v1/services/discovered/LogAggregatorService/register
```

## Configuration at a glance

The defaults below are the constructor (L0) values: a field absent from every
layer, or explicitly cleared with `null`, resolves to the value shown.

| Setting | Default | Meaning |
| --- | --- | --- |
| `source_services` | `[]` | Service names whose per-worker log streams are aggregated |
| `exclude_services` | `[]` | Names removed from the source set (self-exclusion) |
| `target_stream` | `scietex:log` | Shared stream the aggregator writes |
| `max_len` | `100000` | `MAXLEN ~` applied on each write (`0` disables) |
| `ttl_seconds` | `604800` | Whole-key TTL refreshed on a timer (`0` disables) |
| `batch_size` | `200` | Entries per `XREAD` |
| `block_ms` | `1000` | `XREAD` block timeout |
| `scan_interval` | `15.0` | Seconds between source-set SCANs |

## Development

Dependencies are managed with **uv**; checks and tests run through **tox**:

```bash
uv sync --all-extras
uv run tox                    # format, lint, type, py314
uv run tox -e py314           # tests only
```
