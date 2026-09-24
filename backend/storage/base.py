"""Storage adapter contract."""
from __future__ import annotations

from typing import Any, Protocol


class StorageAdapter(Protocol):
    """Read/write vector layers for a single OFM world.

    Layer = the set of features under a single named key (table for
    PostGIS/SpatiaLite, single .geojson file otherwise). All adapters speak
    GeoJSON FeatureCollections at this boundary; the adapter handles the
    storage-specific schema (`geom` vs `the_geom`, WKT vs JSON, etc.).
    """

    mode: str

    def list_layers(self) -> list[dict[str, Any]]:
        """Return [{name, count, geometry_type}, ...]."""
        ...

    def load_layer(self, layer: str) -> dict[str, Any]:
        """Return a FeatureCollection."""
        ...

    def save_layer(self, layer: str, feature_collection: dict[str, Any]) -> int:
        """Replace the layer's contents. Returns feature count written."""
        ...

    def delete_layer(self, layer: str) -> None:
        ...
