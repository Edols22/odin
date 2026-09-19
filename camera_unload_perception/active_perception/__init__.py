"""Architecture-level active perception core.

This package is sensor-agnostic. Odin is one adapter used for validation;
future camera and radar adapters can provide the same CapturePacket contract.
"""

from .schema import (
    BoxObservation,
    BoxRecord,
    CapturePacket,
    FaceType,
    GeometryEstimate,
    GeometryStatus,
    LifeState,
    RayComparison,
    RayLabel,
    VisibilityState,
)
from .ray_compare import compare_raw_returns, predict_surface_hits
from .map_manager import ActivePerceptionMap
from .scheduler import ActiveObservationScheduler, ObservationMode

__all__ = [
    'ActivePerceptionMap', 'ActiveObservationScheduler', 'BoxObservation',
    'BoxRecord', 'CapturePacket', 'FaceType', 'GeometryEstimate',
    'GeometryStatus', 'LifeState', 'ObservationMode', 'RayComparison',
    'RayLabel', 'VisibilityState', 'compare_raw_returns',
    'predict_surface_hits',
]
