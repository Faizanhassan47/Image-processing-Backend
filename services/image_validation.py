"""Image integrity and metadata validation shared by all ingestion paths."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


RASTER_EXTENSIONS = {".jpg", ".jpeg", ".png"}
WSI_EXTENSIONS = {".svs", ".tif", ".tiff", ".ndpi", ".mrxs", ".scn"}
SUPPORTED_EXTENSIONS = RASTER_EXTENSIONS | WSI_EXTENSIONS


class ImageValidationError(ValueError):
    """Raised when an image is unsupported, corrupt, incomplete, or unreadable."""


def calculate_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _safe_metadata(values: dict[str, Any], limit: int = 200) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        text = str(value)
        result[str(key)[:200]] = text[:2000]
        if len(result) >= limit:
            break
    return result


def _validate_raster(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            if image.format not in {"JPEG", "PNG"}:
                raise ImageValidationError(f"Unsupported raster encoding: {image.format or 'unknown'}")
            width, height = image.size
            if width < 1 or height < 1:
                raise ImageValidationError("Image dimensions are invalid")
            dpi = image.info.get("dpi")
            dpi_x = _number(dpi[0]) if isinstance(dpi, (tuple, list)) and dpi else None
            dpi_y = _number(dpi[1]) if isinstance(dpi, (tuple, list)) and len(dpi) > 1 else dpi_x
            pixel_x = 25400.0 / dpi_x if dpi_x else None
            pixel_y = 25400.0 / dpi_y if dpi_y else None
            metadata = _safe_metadata({
                "mode": image.mode,
                "dpi_x": dpi_x,
                "dpi_y": dpi_y,
                **{f"info.{key}": value for key, value in image.info.items() if key not in {"icc_profile", "exif"}},
            })
            return {
                "file_type": "raster",
                "format": image.format,
                "width": width,
                "height": height,
                "level_count": 1,
                "pyramid_levels": [{"level": 0, "width": width, "height": height, "downsample": 1.0}],
                "metadata": metadata,
                "normalized_metadata": {
                    "slide_label": None,
                    "barcode": None,
                    "scanner_vendor": None,
                    "scanner_model": None,
                    "magnification": None,
                    "acquisition_time": None,
                },
                "physical_calibration": {
                    "available": bool(pixel_x and pixel_y),
                    "pixel_size_microns_x": pixel_x,
                    "pixel_size_microns_y": pixel_y,
                    "source": "dpi" if pixel_x and pixel_y else None,
                },
            }
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError("File is corrupt, incomplete, or not a supported JPG/PNG image") from exc


def _validate_wsi(path: Path, openslide_module) -> dict[str, Any]:
    if openslide_module is None:
        raise ImageValidationError("Whole-slide validation requires OpenSlide")
    try:
        with openslide_module.OpenSlide(str(path)) as slide:
            width, height = slide.dimensions
            if width < 1 or height < 1 or slide.level_count < 1:
                raise ImageValidationError("Whole-slide dimensions or pyramid levels are invalid")
            levels = [
                {
                    "level": index,
                    "width": int(dimensions[0]),
                    "height": int(dimensions[1]),
                    "downsample": float(slide.level_downsamples[index]),
                }
                for index, dimensions in enumerate(slide.level_dimensions)
            ]
            # Decode a small region from the lowest-resolution level to detect
            # truncated files that expose headers but cannot return pixels.
            lowest = slide.level_count - 1
            sample_width = min(32, levels[lowest]["width"])
            sample_height = min(32, levels[lowest]["height"])
            slide.read_region((0, 0), lowest, (sample_width, sample_height)).load()
            properties = dict(slide.properties)
            mpp_x = _number(properties.get(getattr(openslide_module, "PROPERTY_NAME_MPP_X", "openslide.mpp-x")))
            mpp_y = _number(properties.get(getattr(openslide_module, "PROPERTY_NAME_MPP_Y", "openslide.mpp-y")))
            normalized = {
                "slide_label": properties.get("aperio.Title") or properties.get("hamamatsu.Reference") or properties.get("openslide.comment"),
                "barcode": properties.get("aperio.Barcode") or properties.get("hamamatsu.Reference"),
                "scanner_vendor": properties.get(getattr(openslide_module, "PROPERTY_NAME_VENDOR", "openslide.vendor")),
                "scanner_model": properties.get("aperio.ScannerModel") or properties.get("hamamatsu.Product"),
                "magnification": _number(properties.get(getattr(openslide_module, "PROPERTY_NAME_OBJECTIVE_POWER", "openslide.objective-power")) or properties.get("aperio.AppMag")),
                "acquisition_time": properties.get("aperio.Date") or properties.get("hamamatsu.Created"),
            }
            return {
                "file_type": "wsi",
                "format": properties.get(getattr(openslide_module, "PROPERTY_NAME_VENDOR", "openslide.vendor"), path.suffix.lstrip(".").upper()),
                "width": int(width),
                "height": int(height),
                "level_count": int(slide.level_count),
                "pyramid_levels": levels,
                "metadata": _safe_metadata(properties),
                "normalized_metadata": normalized,
                "physical_calibration": {
                    "available": bool(mpp_x and mpp_y),
                    "pixel_size_microns_x": mpp_x,
                    "pixel_size_microns_y": mpp_y,
                    "source": "openslide.mpp-x/mpp-y" if mpp_x and mpp_y else None,
                },
            }
    except ImageValidationError:
        raise
    except Exception as exc:
        raise ImageValidationError("File is corrupt, incomplete, or not readable by OpenSlide") from exc


def validate_image_file(path: Path, openslide_module=None) -> dict[str, Any]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ImageValidationError(f"Unsupported image format: {suffix or 'no extension'}")
    if not path.is_file():
        raise ImageValidationError("Image file does not exist")
    file_size = path.stat().st_size
    if file_size < 1:
        raise ImageValidationError("Image file is empty")

    details = _validate_raster(path) if suffix in RASTER_EXTENSIONS else _validate_wsi(path, openslide_module)
    details.update({
        "valid": True,
        "supported": True,
        "integrity": "valid",
        "extension": suffix,
        "file_size": file_size,
        "checksum_sha256": calculate_sha256(path),
        "duplicate": False,
        "duplicate_of": None,
    })
    return details
