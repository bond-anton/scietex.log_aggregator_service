"""Log Aggregator Service."""

from .aggregator_worker import LogAggregatorWorker
from .config import (
    AGGREGATOR_CONFIG_FILE,
    AGGREGATOR_CONFIG_SUBDIR,
    AGGREGATOR_SECTION,
    LogAggregatorSettings,
    read_aggregator_config,
)
from .version import __version__

__all__ = [
    "__version__",
    "LogAggregatorWorker",
    "AGGREGATOR_CONFIG_FILE",
    "AGGREGATOR_CONFIG_SUBDIR",
    "AGGREGATOR_SECTION",
    "LogAggregatorSettings",
    "read_aggregator_config",
]
