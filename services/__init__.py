"""Services package. Heavy imports stay lazy so the MLLP process can start on its own."""

__all__ = [
    "ImageValidationError",
    "validate_image_file",
    "ADAPTERS",
    "CONVERTERS",
    "DeepZoomGenerator",
    "openslide",
    "slide_cache",
]


def __getattr__(name):
    if name in {"ImageValidationError", "validate_image_file"}:
        from .image_validation import ImageValidationError, validate_image_file
        return ImageValidationError if name == "ImageValidationError" else validate_image_file
    if name in {"ADAPTERS", "CONVERTERS"}:
        from .scanner_adapters import ADAPTERS, CONVERTERS
        return ADAPTERS if name == "ADAPTERS" else CONVERTERS
    if name in {"DeepZoomGenerator", "openslide", "slide_cache"}:
        from .slide_cache import DeepZoomGenerator, openslide, slide_cache
        values = {
            "DeepZoomGenerator": DeepZoomGenerator,
            "openslide": openslide,
            "slide_cache": slide_cache,
        }
        return values[name]
    raise AttributeError(name)
