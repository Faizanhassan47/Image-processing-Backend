"""Vendor-neutral scanner and converter extension contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from services.image_validation import RASTER_EXTENSIONS, WSI_EXTENSIONS, validate_image_file


class ScannerAdapter(Protocol):
    name: str
    extensions: frozenset[str]

    def validate(self, path: Path, openslide_module=None) -> dict: ...
    def suggested_identifier(self, path: Path, metadata: dict) -> str | None: ...


@dataclass(frozen=True)
class GenericImageAdapter:
    name: str
    extensions: frozenset[str]

    def validate(self, path: Path, openslide_module=None) -> dict:
        result = validate_image_file(path, openslide_module)
        result["adapter_name"] = self.name
        return result

    def suggested_identifier(self, path: Path, metadata: dict) -> str | None:
        normalized = metadata.get("normalized_metadata", {})
        return normalized.get("barcode") or normalized.get("slide_label") or path.stem


class AdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, ScannerAdapter] = {}

    def register(self, adapter: ScannerAdapter) -> None:
        if adapter.name in self._adapters:
            raise ValueError(f"Adapter already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ScannerAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise ValueError(f"Unknown scanner adapter: {name}") from exc

    def for_path(self, path: Path) -> ScannerAdapter:
        suffix = path.suffix.lower()
        for adapter in self._adapters.values():
            if suffix in adapter.extensions:
                return adapter
        raise ValueError(f"No registered adapter supports {suffix or 'files without an extension'}")

    def describe(self) -> list[dict]:
        return [{"name": item.name, "extensions": sorted(item.extensions)} for item in self._adapters.values()]


ADAPTERS = AdapterRegistry()
ADAPTERS.register(GenericImageAdapter("generic-raster", frozenset(RASTER_EXTENSIONS)))
ADAPTERS.register(GenericImageAdapter("generic-openslide", frozenset(WSI_EXTENSIONS)))


class FormatConverter(Protocol):
    name: str
    source_extensions: frozenset[str]
    target_extension: str

    def convert(self, source: Path, destination: Path) -> dict: ...


CONVERTERS: dict[str, FormatConverter] = {}


def register_converter(converter: FormatConverter) -> None:
    """Register only converters whose licensing and output validation are approved."""
    if converter.name in CONVERTERS:
        raise ValueError(f"Converter already registered: {converter.name}")
    CONVERTERS[converter.name] = converter
