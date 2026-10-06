"""Configuration models and YAML loader for the log aggregator service.

`LogAggregatorSettings` is the service-owned bootstrap snapshot stored in
``log_aggregator.yml`` under the config directory. It is kept deliberately thin:
it names the source services to aggregate, the target stream, and the retention
policy the aggregator enforces on that stream.

The aggregator is the sole writer of the target stream, so it owns both the
``MAXLEN`` trim and the whole-key ``EXPIRE``. The API delivers these values as
the remote ``log_aggregator`` section; the local YAML is only the bootstrap
fallback used before the first remote apply.
"""

from pathlib import Path

import msgspec

#: Remote-config section name the settings are registered under.
AGGREGATOR_SECTION: str = "log_aggregator"

#: Subdirectory under the shared config dir that namespaces this service's
#: files. The framework's ``config.yml`` snapshot and the service-owned
#: ``log_aggregator.yml`` both live here, so services sharing one config dir
#: (the framework resolves a single dir for all scietex services) cannot collide.
AGGREGATOR_CONFIG_SUBDIR: str = "log_aggregator"

#: Filename of the service-owned bootstrap snapshot in the config subdirectory.
AGGREGATOR_CONFIG_FILE: str = "log_aggregator.yml"


class LogAggregatorSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Top-level log aggregator settings.

    ``source_services`` lists the service names whose per-worker log streams are
    aggregated; ``exclude_services`` removes names from that set (the backend
    sets it to the aggregator's own service name to prevent self-ingestion).
    ``target_stream`` is the shared stream the aggregator writes; ``max_len`` and
    ``ttl_seconds`` are the retention policy it enforces on it. A ``max_len`` or
    ``ttl_seconds`` of ``0`` disables that half of the policy.
    """

    source_services: list[str] = msgspec.field(default_factory=list)
    exclude_services: list[str] = msgspec.field(default_factory=list)
    target_stream: str = "scietex:log"
    max_len: int = 100_000
    ttl_seconds: int = 604800
    batch_size: int = 200
    block_ms: int = 1000
    scan_interval: float = 15.0


def read_aggregator_config(conf_dir: Path | None, *, create_default: bool = True) -> LogAggregatorSettings:
    """Read aggregator settings from ``log_aggregator/log_aggregator.yml``.

    The service's files are namespaced in a ``log_aggregator/`` subdirectory so
    they do not collide with other services sharing the framework's single config
    dir. Mirrors `read_modbus_config`: the file (and, when missing, its
    directory) is only created when ``create_default=True`` (the bootstrap path).
    A ``None`` or non-directory ``conf_dir``, a missing file/directory with
    ``create_default=False``, or an unparseable file each raise `RuntimeError`.
    An existing-but-invalid file is left untouched regardless of ``create_default``.

    Args:
        conf_dir: Path to the configuration directory.
        create_default: Whether to create the directory and write a default
            ``log_aggregator.yml`` when missing. Default ``True``.

    Returns:
        A `LogAggregatorSettings` loaded from ``log_aggregator.yml`` or defaults.

    Raises:
        RuntimeError: If ``conf_dir`` is ``None`` or not a directory, the file
            is missing with ``create_default=False``, or the file cannot be parsed.
    """
    if not isinstance(conf_dir, Path):
        raise RuntimeError("Configuration dir was not set!")
    service_dir = conf_dir / AGGREGATOR_CONFIG_SUBDIR
    if not service_dir.exists():
        if create_default:
            try:
                service_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise RuntimeError(f"Failed to create configuration directory {service_dir}!") from exc
        else:
            raise RuntimeError(
                f"Configuration directory {service_dir} does not exist and create_default=False (no default generated)."
            )
    elif not service_dir.is_dir():
        raise RuntimeError(f"Provided configuration directory path {service_dir} is not a directory!")
    config_yml = service_dir.joinpath(AGGREGATOR_CONFIG_FILE)
    if not config_yml.exists():
        if create_default:
            settings = LogAggregatorSettings()
            with open(config_yml, "wb") as f:
                f.write(msgspec.yaml.encode(settings))
            return settings
        raise RuntimeError(
            f"Log aggregator configuration file {config_yml} does not exist and create_default=False "
            "(pass create_default=True to generate defaults)."
        )
    try:
        with open(config_yml, "rb") as f:
            return msgspec.yaml.decode(f.read(), type=LogAggregatorSettings, strict=True)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse log aggregator configuration file {config_yml}. "
            "Fix the file or remove it to regenerate defaults."
        ) from exc
