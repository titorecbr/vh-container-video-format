"""VH Video Container - SQLite-based video format optimized for AI workloads."""

from .vhlib import VHFile
from .vh_stream import VHStream

__version__ = "1.0.1"
__all__ = ["VHFile", "VHStream"]
