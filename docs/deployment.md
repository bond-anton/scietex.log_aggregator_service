# Deployment

How to run `scietex.log_aggregator_service` as a container (Docker or Podman) and
how to configure it. The service is a plain Python console script; the included
`Containerfile` packages it with a non-root user and a mounted config volume.

## Entry point

The package installs one console script:

```bash
start-log-aggregator
```

It accepts three flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--conf-dir` | auto-resolved | Configuration directory |
| `--service-name` | `$SCIETEX_SERVICE_NAME` or `LogAggregatorService` | Service name reported to the framework |
| `--logging-level` | `$SCIETEX_LOGGING_LEVEL` or `INFO` | Logging level |

Precedence is **CLI flag > environment variable > built-in default**. The
`--conf-dir` flag defaults to `None` so the framework resolves the directory
itself (it already honors `SCIETEX_CONFIG_DIR`).

The service runs in the foreground until it receives `SIGINT` or `SIGTERM`.
`docker stop` / `podman stop` send `SIGTERM`, which triggers graceful shutdown —
no extra wiring is needed.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `SCIETEX_CONFIG_DIR` | Config-directory candidate (used if set and an existing dir) | unset |
| `SCIETEX_SERVICE_NAME` | Fallback for `--service-name` | `LogAggregatorService` |
| `SCIETEX_LOGGING_LEVEL` | Fallback for `--logging-level` | `INFO` |
| `XDG_CONFIG_HOME` | XDG base dir; candidate becomes `$XDG_CONFIG_HOME/scietex` | unset → `~/.config/scietex` |

## Container image

The `Containerfile` is a two-stage build on `python:3.14-slim-trixie`:

- **Builder stage** installs the released `scietex.log_aggregator_service` wheel
  from PyPI (selected by the `VERSION` build arg) and its dependencies into
  `/opt/venv`, then slims the venv (drops pip/setuptools/wheel, bytecode caches,
  and the unused per-interpreter `glide_shared` extensions).
- **Runtime stage** copies the venv, creates a non-root `appuser` (uid 1000),
  prepares `/config`, and runs `start-log-aggregator`.

The image installs the published package rather than building from the local
checkout, so the image and the PyPI artifact for a given version are the same
code. The `VERSION` build arg must name a version that exists on PyPI.

The image sets these defaults:

```dockerfile
ENV SCIETEX_CONFIG_DIR=/config \
    SCIETEX_SERVICE_NAME=LogAggregatorService \
    SCIETEX_LOGGING_LEVEL=INFO
VOLUME ["/config"]
```

### Build

```bash
podman build --build-arg VERSION=0.2.0 -t scietex-log-aggregator-service .
```

`build_image.sh` builds a multi-arch manifest (`linux/amd64,linux/arm64`) and
pushes it to `registry.buro-nts.ru/scietex-log-aggregator-service`, tagged with
the version from `version.py`. It passes that version as the `VERSION` build arg,
so the image always installs the matching PyPI release. Pass `--latest` to also
push the `latest` tag.

### Run

```bash
podman run --rm \
  -v ./config:/config \
  scietex-log-aggregator-service
```

The aggregator needs no devices and no privileged ports — it only talks to
Valkey.

## Config volume

`/config` is declared as a volume and owned by `appuser`. Mount a host directory
there so configuration survives container replacement:

```bash
-v ./config:/config
```

The service writes its files under `/config/log_aggregator/`:

```
/config/log_aggregator/log_aggregator.yml   # L1 bootstrap patch (service-owned)
/config/log_aggregator/config.yml           # L2 snapshot (framework-owned)
```

On first run, `log_aggregator.yml` is created with defaults. Edit it on the host
and restart the container to apply changes. The framework's `config.yml` snapshot
is written automatically after a successful remote apply and overlays the
bootstrap on the next start; the remote `log_aggregator` section (L3) overlays
both. See the [README](../README.md#configuration-at-a-glance) for the settings
table.

## Stream-name coupling

The aggregator writes the shared stream (`target_stream`, default `scietex:log`)
and the API backend reads it (`API156_VALKEY_LOG_STREAM_NAME`). **Both must name
the same stream**, or the system log viewer stays empty. The backend is the
config authority: it delivers `target_stream`, `max_len`, and `ttl_seconds` to
the aggregator as its remote `log_aggregator` section, so the two stay in sync
once the service is registered.

Register the service so the backend can deliver that section:

```bash
curl -X POST http://localhost:8000/api/v1/services/discovered/LogAggregatorService/register \
  -H 'Content-Type: application/json' \
  -d '{"description": "Aggregates per-worker log streams into the shared system log stream."}'
```

The service type is resolved from the plugin matcher, so no `service_type_id` is
needed. Re-deliver on demand with
`POST /api/v1/services/LogAggregatorService/publish`.

## Health and shutdown

There is **no HTTP server and no `/health` endpoint**. A container healthcheck
must observe the framework's broker-side heartbeat (a Valkey key) or the process
itself, not an HTTP probe.

`SIGTERM` triggers graceful shutdown: the worker cancels its reader loop, then
releases the framework resources.
