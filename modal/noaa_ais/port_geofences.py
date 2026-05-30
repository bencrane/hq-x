"""Major US port bounding boxes (lat/lon).

Used by the RisingWave mv_vessel_arrivals MV as the port-active filter. v0
uses static bboxes — coarse but dependency-free. A v1 swap-in would JOIN
against Overture port polygons (entities.source_overture_places, kind='port'
or similar) for sub-pier accuracy.

bbox = (lat_min, lat_max, lon_min, lon_max). Lon is negative (west of GMT)
for all US ports. Boxes are deliberately generous (~5-10 km outside breakwater)
so vessels at anchor or transiting outer harbors still match.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortBBox:
    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


MAJOR_US_PORTS: tuple[PortBBox, ...] = (
    PortBBox("Long Beach / Los Angeles", 33.60, 33.80, -118.40, -118.00),
    PortBBox("Oakland", 37.70, 37.90, -122.40, -122.20),
    PortBBox("Seattle / Tacoma", 47.10, 47.70, -122.60, -122.20),
    PortBBox("Houston", 29.40, 29.85, -95.40, -94.70),
    PortBBox("New Orleans", 29.85, 30.10, -90.20, -89.90),
    PortBBox("Mobile", 30.55, 30.75, -88.10, -87.95),
    PortBBox("Newark / NY-NJ", 40.50, 40.80, -74.30, -73.95),
    PortBBox("Norfolk / Hampton Roads", 36.70, 37.05, -76.45, -76.00),
    PortBBox("Baltimore", 39.20, 39.30, -76.65, -76.40),
    PortBBox("Charleston", 32.60, 32.95, -80.05, -79.80),
    PortBBox("Savannah", 31.90, 32.20, -81.20, -80.80),
    PortBBox("Jacksonville", 30.30, 30.50, -81.65, -81.40),
    PortBBox("Miami", 25.70, 25.85, -80.20, -80.10),
)
