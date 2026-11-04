"""Read an RGB patch from SVS or DICOM WSI through the same OpenSlide handle."""

from __future__ import annotations

from pathlib import Path


def read_rgb_patch(path: Path, x: int, y: int, width: int, height: int, openslide_module):
    if openslide_module is None:
        raise RuntimeError("OpenSlide is not available")
    slide = openslide_module.OpenSlide(str(path))
    try:
        image = slide.read_region((int(x), int(y)), 0, (int(width), int(height))).convert("RGB")
        mpp = slide.properties.get("openslide.mpp-x")
        if not mpp:
            spacing_mm = slide.properties.get("dicom.PixelSpacing[0]")
            mpp = float(spacing_mm) * 1000 if spacing_mm else None
        return image, float(mpp) if mpp else None
    finally:
        slide.close()
