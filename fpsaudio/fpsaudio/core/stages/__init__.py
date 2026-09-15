"""Pipeline stages.

Each stage answers ``describe()`` and ``commands()`` without side effects, so a
plan can be explained in full before anything runs.  ``run()`` is the only
method that touches the filesystem.
"""

from __future__ import annotations

from .atmos import AtmosDecodeStage, HandoffStage, retime_atmos_metadata
from .base import BaseStage
from .context import RunContext
from .decode import DecodeStage
from .demux import DemuxStage
from .encode import EncodeStage, QuantizeStage
from .mux import MuxStage, rescale_chapters, rescaled_delay_ms
from .retime import RedeclareStage, ResampleStage, StretchStage
from .verify_stage import VerifyStage

__all__ = [
    "AtmosDecodeStage",
    "BaseStage",
    "DecodeStage",
    "DemuxStage",
    "EncodeStage",
    "HandoffStage",
    "MuxStage",
    "QuantizeStage",
    "RedeclareStage",
    "ResampleStage",
    "RunContext",
    "StretchStage",
    "VerifyStage",
    "rescale_chapters",
    "rescaled_delay_ms",
    "retime_atmos_metadata",
]
