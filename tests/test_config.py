"""Tests for the log aggregator configuration models and YAML loader."""

from pathlib import Path

import msgspec
import pytest

from scietex.log_aggregator_service.config import (
    AGGREGATOR_CONFIG_FILE,
    AGGREGATOR_CONFIG_SUBDIR,
    AGGREGATOR_SETTINGS_DEFAULTS,
    LogAggregatorSettings,
    read_aggregator_config,
)


def _config_path(conf_dir: Path) -> Path:
    """The namespaced path the loader reads and writes."""
    return conf_dir / AGGREGATOR_CONFIG_SUBDIR / AGGREGATOR_CONFIG_FILE


def test_defaults_are_sane() -> None:
    """The default settings aggregate nothing and target the shared stream."""
    settings = LogAggregatorSettings()

    assert settings.source_services == []
    assert settings.exclude_services == []
    assert settings.target_stream == "scietex:log"
    assert settings.max_len == 100_000
    assert settings.ttl_seconds == 604800


def test_defaults_constant_is_l0_base() -> None:
    """AGGREGATOR_SETTINGS_DEFAULTS is the concrete L0 base instance."""
    assert isinstance(AGGREGATOR_SETTINGS_DEFAULTS, LogAggregatorSettings)
    assert AGGREGATOR_SETTINGS_DEFAULTS == LogAggregatorSettings()


def test_read_aggregator_config_writes_defaults_when_missing(tmp_path: Path) -> None:
    """A missing file is created under the subdir with the default patch dict."""
    patch = read_aggregator_config(tmp_path)

    assert patch == msgspec.to_builtins(LogAggregatorSettings())
    assert _config_path(tmp_path).is_file()


def test_read_aggregator_config_returns_dict_not_struct(tmp_path: Path) -> None:
    """The loader returns the L1 patch as a plain dict, not a struct."""
    patch = read_aggregator_config(tmp_path)

    assert type(patch) is dict
    assert not isinstance(patch, LogAggregatorSettings)


def test_read_aggregator_config_malformed_raises_and_leaves_file(tmp_path: Path) -> None:
    """An unparseable file raises RuntimeError and is left untouched."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("unterminated: [flow\n")

    with pytest.raises(RuntimeError):
        read_aggregator_config(tmp_path)

    assert path.read_text() == "unterminated: [flow\n"


def test_read_aggregator_config_none_dir_raises() -> None:
    """A None conf_dir is rejected with RuntimeError."""
    with pytest.raises(RuntimeError):
        read_aggregator_config(None)


def test_read_aggregator_config_missing_without_create_raises(tmp_path: Path) -> None:
    """A missing file with create_default=False raises and writes nothing."""
    with pytest.raises(RuntimeError):
        read_aggregator_config(tmp_path, create_default=False)

    assert not _config_path(tmp_path).exists()


def test_read_aggregator_config_roundtrips_values(tmp_path: Path) -> None:
    """Explicit values survive a write/read round-trip as patch-dict entries."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        "source_services:\n"
        "  - ModbusService\n"
        "exclude_services:\n"
        "  - LogAggregatorService\n"
        "target_stream: scietex:log\n"
        "max_len: 500\n"
        "ttl_seconds: 60\n"
    )

    result = read_aggregator_config(tmp_path)

    assert result["source_services"] == ["ModbusService"]
    assert result["exclude_services"] == ["LogAggregatorService"]
    assert result["target_stream"] == "scietex:log"
    assert result["max_len"] == 500
    assert result["ttl_seconds"] == 60


def test_read_aggregator_config_partial_file_returns_only_present_keys(tmp_path: Path) -> None:
    """A partial file yields a partial patch: absent keys are not filled in.

    The loader hands back only the keys present in the file; the framework's
    merge fills the rest from L0. Filling defaults here would turn every key
    into an explicit set and defeat the "absent = inherit" layer semantics.
    """
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("max_len: 500\n")

    result = read_aggregator_config(tmp_path)

    assert result == {"max_len": 500}
