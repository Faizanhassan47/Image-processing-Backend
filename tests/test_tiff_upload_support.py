"""
Tests for TIFF/OME-TIFF upload support alongside SVS and raster formats.

Covers:
  1. JPG/PNG: accepted <= 10 MB, rejected > 10 MB
  2. SVS: accepted <= WSI_MAX_BYTES, rejected if OpenSlide unavailable
  3. TIFF/TIF: accepted as WSI (using WSI_MAX_BYTES), rejected > WSI_MAX_BYTES
  4. Unsupported extensions: BMP, DICOM, etc.
  5. Oversized raster (> 10 MB) rejected with 413
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Run an async coroutine synchronously."""
    return anyio.from_thread.run_sync(lambda: anyio.run(coro))


def _make_upload(filename: str, size_bytes: int = 1024) -> MagicMock:
    """Create a mock UploadFile with the given filename and simulated payload."""
    upload = MagicMock()
    upload.filename = filename
    upload.close = AsyncMock()

    # Simulate streaming: return `size_bytes` bytes in one chunk, then b"".
    data = b"X" * size_bytes
    upload.read = AsyncMock(side_effect=[data, b""])
    return upload


def _make_validate_and_store(openslide_mock=None, wsi_max_mb: int = 2048, raster_max_mb: int = 10):
    """
    Build a self-contained async validate_and_store function that mirrors
    the branching logic in cases_api.py without requiring a live DB or FS.
    """
    _WSI_SUFFIXES = {".svs", ".tif", ".tiff"}
    _RASTER_SUFFIXES = {".jpg", ".jpeg", ".png"}
    WSI_MAX_BYTES = wsi_max_mb * 1024 * 1024
    RASTER_MAX_BYTES = raster_max_mb * 1024 * 1024

    async def validate_and_store(upload):
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in _RASTER_SUFFIXES | _WSI_SUFFIXES:
            await upload.close()
            raise HTTPException(400, "Upload a JPG, PNG, SVS, or TIFF file")
        is_wsi = suffix in _WSI_SUFFIXES
        if is_wsi and openslide_mock is None:
            await upload.close()
            raise HTTPException(503, "WSI upload requires OpenSlide")
        limit = WSI_MAX_BYTES if is_wsi else RASTER_MAX_BYTES
        written = 0
        while chunk := await upload.read(8 * 1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise HTTPException(413, f"File exceeds the {limit // 1048576} MB limit")
        if not written:
            raise HTTPException(400, "Uploaded file is empty")
        return suffix, is_wsi, written

    return validate_and_store


# ---------------------------------------------------------------------------
# Extension acceptance / rejection tests
# ---------------------------------------------------------------------------

class TestExtensionFiltering:

    def test_jpg_accepted(self):
        async def _run():
            vns = _make_validate_and_store()
            suffix, is_wsi, written = await vns(_make_upload("slide.jpg", 1024))
            assert suffix == ".jpg" and not is_wsi and written == 1024
        anyio.run(_run)

    def test_jpeg_accepted(self):
        async def _run():
            vns = _make_validate_and_store()
            suffix, is_wsi, _ = await vns(_make_upload("slide.JPEG", 512))
            assert suffix == ".jpeg" and not is_wsi
        anyio.run(_run)

    def test_png_accepted(self):
        async def _run():
            vns = _make_validate_and_store()
            suffix, is_wsi, _ = await vns(_make_upload("stain.png", 2048))
            assert suffix == ".png" and not is_wsi
        anyio.run(_run)

    def test_svs_accepted_with_openslide(self):
        async def _run():
            vns = _make_validate_and_store(openslide_mock=MagicMock())
            suffix, is_wsi, _ = await vns(_make_upload("wsi.svs", 1024))
            assert suffix == ".svs" and is_wsi
        anyio.run(_run)

    def test_tif_accepted_with_openslide(self):
        """TIFF files (.tif) must be treated as WSI and accepted."""
        async def _run():
            vns = _make_validate_and_store(openslide_mock=MagicMock())
            suffix, is_wsi, _ = await vns(_make_upload("scan.tif", 1024))
            assert suffix == ".tif"
            assert is_wsi, ".tif must route as WSI, not raster"
        anyio.run(_run)

    def test_tiff_accepted_with_openslide(self):
        """TIFF files (.tiff) must be treated as WSI and accepted."""
        async def _run():
            vns = _make_validate_and_store(openslide_mock=MagicMock())
            suffix, is_wsi, _ = await vns(_make_upload("ome.tiff", 4096))
            assert suffix == ".tiff" and is_wsi
        anyio.run(_run)

    def test_tif_rejected_without_openslide(self):
        """TIFF must be rejected with 503 when OpenSlide is unavailable."""
        async def _run():
            vns = _make_validate_and_store(openslide_mock=None)
            with pytest.raises(HTTPException) as exc_info:
                await vns(_make_upload("scan.tif", 1024))
            assert exc_info.value.status_code == 503
        anyio.run(_run)

    def test_tiff_rejected_without_openslide(self):
        """TIFF (.tiff) must also be rejected with 503 when OpenSlide is unavailable."""
        async def _run():
            vns = _make_validate_and_store(openslide_mock=None)
            with pytest.raises(HTTPException) as exc_info:
                await vns(_make_upload("ome.tiff", 1024))
            assert exc_info.value.status_code == 503
        anyio.run(_run)

    def test_unsupported_bmp_rejected(self):
        async def _run():
            vns = _make_validate_and_store()
            with pytest.raises(HTTPException) as exc_info:
                await vns(_make_upload("photo.bmp", 1024))
            assert exc_info.value.status_code == 400
            assert "TIFF" in exc_info.value.detail
        anyio.run(_run)

    def test_unsupported_dcm_rejected(self):
        async def _run():
            vns = _make_validate_and_store()
            with pytest.raises(HTTPException) as exc_info:
                await vns(_make_upload("scan.dcm", 1024))
            assert exc_info.value.status_code == 400
        anyio.run(_run)

    def test_no_extension_rejected(self):
        async def _run():
            vns = _make_validate_and_store()
            with pytest.raises(HTTPException) as exc_info:
                await vns(_make_upload("unknown_file", 1024))
            assert exc_info.value.status_code == 400
        anyio.run(_run)


# ---------------------------------------------------------------------------
# Size-limit enforcement tests
# ---------------------------------------------------------------------------

class TestSizeLimits:
    RASTER_MAX_MB = 10
    WSI_MAX_MB = 2048

    def test_jpg_at_limit_accepted(self):
        """JPG at exactly 10 MB must be accepted."""
        async def _run():
            vns = _make_validate_and_store()
            size = self.RASTER_MAX_MB * 1024 * 1024
            _, _, written = await vns(_make_upload("slide.jpg", size))
            assert written == size
        anyio.run(_run)

    def test_jpg_over_limit_rejected(self):
        """JPG exceeding 10 MB must be rejected with 413."""
        async def _run():
            vns = _make_validate_and_store()
            size = self.RASTER_MAX_MB * 1024 * 1024 + 1
            with pytest.raises(HTTPException) as exc_info:
                await vns(_make_upload("big.jpg", size))
            assert exc_info.value.status_code == 413
        anyio.run(_run)

    def test_png_over_limit_rejected(self):
        """PNG exceeding 10 MB must be rejected with 413."""
        async def _run():
            vns = _make_validate_and_store()
            size = self.RASTER_MAX_MB * 1024 * 1024 + 512
            with pytest.raises(HTTPException) as exc_info:
                await vns(_make_upload("big.png", size))
            assert exc_info.value.status_code == 413
        anyio.run(_run)

    def test_tiff_between_raster_and_wsi_limit_accepted(self):
        """TIFF at 50 MB (> raster 10 MB limit) must be accepted using the WSI limit."""
        async def _run():
            vns = _make_validate_and_store(openslide_mock=MagicMock())
            size = 50 * 1024 * 1024
            _, is_wsi, written = await vns(_make_upload("large.tiff", size))
            assert is_wsi and written == size
        anyio.run(_run)

    def test_tif_between_raster_and_wsi_limit_accepted(self):
        """TIF at 50 MB (> raster 10 MB limit) must be accepted using the WSI limit."""
        async def _run():
            vns = _make_validate_and_store(openslide_mock=MagicMock())
            size = 50 * 1024 * 1024
            _, is_wsi, written = await vns(_make_upload("large.tif", size))
            assert is_wsi and written == size
        anyio.run(_run)

    def test_tiff_over_wsi_limit_rejected(self):
        """TIFF exceeding WSI_MAX_BYTES must be rejected with 413."""
        async def _run():
            wsi_max_mb = 100  # smaller limit for the test
            vns = _make_validate_and_store(openslide_mock=MagicMock(), wsi_max_mb=wsi_max_mb)
            size = wsi_max_mb * 1024 * 1024 + 1
            with pytest.raises(HTTPException) as exc_info:
                await vns(_make_upload("huge.tiff", size))
            assert exc_info.value.status_code == 413
        anyio.run(_run)

    def test_svs_large_within_wsi_limit_accepted(self):
        """SVS at 500 MB (well above raster limit) is accepted under the WSI limit."""
        async def _run():
            vns = _make_validate_and_store(openslide_mock=MagicMock())
            size = 500 * 1024 * 1024
            _, is_wsi, written = await vns(_make_upload("wsi.svs", size))
            assert is_wsi and written == size
        anyio.run(_run)


# ---------------------------------------------------------------------------
# image_validation.py extension set tests
# ---------------------------------------------------------------------------

class TestImageValidationExtensions:
    """Verify that image_validation.py defines the correct extension sets."""

    def test_wsi_extensions_include_tif(self):
        from services.image_validation import WSI_EXTENSIONS
        assert ".tif" in WSI_EXTENSIONS

    def test_wsi_extensions_include_tiff(self):
        from services.image_validation import WSI_EXTENSIONS
        assert ".tiff" in WSI_EXTENSIONS

    def test_wsi_extensions_include_svs(self):
        from services.image_validation import WSI_EXTENSIONS
        assert ".svs" in WSI_EXTENSIONS

    def test_raster_extensions_complete(self):
        from services.image_validation import RASTER_EXTENSIONS
        assert {".jpg", ".jpeg", ".png"}.issubset(RASTER_EXTENSIONS)

    def test_supported_extensions_is_union(self):
        from services.image_validation import SUPPORTED_EXTENSIONS, RASTER_EXTENSIONS, WSI_EXTENSIONS
        assert SUPPORTED_EXTENSIONS == RASTER_EXTENSIONS | WSI_EXTENSIONS


# ---------------------------------------------------------------------------
# Frontend validation logic unit test (Python simulation)
# ---------------------------------------------------------------------------

class TestFrontendValidateFileLogic:
    """
    Reproduce the JS validateFile() logic in Python to assert correct
    branching for each file type without requiring a browser.
    """

    WSI_LIMIT_MB = 2048
    RASTER_LIMIT_MB = 10

    def _validate(self, filename: str, size_mb: float) -> tuple[bool, str | None]:
        """
        Returns (is_valid, error_message | None).
        Mirrors the updated PathologyWorkspace.jsx validateFile().
        """
        import re
        m = re.search(r'\.[^.]+$', filename.lower())
        ext = m.group(0) if m else ''
        is_wsi = ext in {'.svs', '.tif', '.tiff'}
        limit_mb = self.WSI_LIMIT_MB if is_wsi else self.RASTER_LIMIT_MB
        limit_bytes = limit_mb * 1024 * 1024
        size_bytes = int(size_mb * 1024 * 1024)
        if size_bytes > limit_bytes:
            return False, f"File exceeds the {limit_mb} MB limit."
        return True, None

    def test_jpg_under_limit(self):
        ok, err = self._validate("slide.jpg", 5)
        assert ok and err is None

    def test_jpg_over_limit(self):
        ok, err = self._validate("slide.jpg", 15)
        assert not ok

    def test_png_over_limit(self):
        ok, err = self._validate("image.PNG", 11)
        assert not ok

    def test_svs_large_accepted(self):
        ok, err = self._validate("wsi.svs", 500)
        assert ok and err is None

    def test_tif_large_accepted_as_wsi(self):
        """TIF at 500 MB must pass because it uses the WSI limit, not raster 10 MB."""
        ok, err = self._validate("scan.tif", 500)
        assert ok and err is None

    def test_tiff_large_accepted_as_wsi(self):
        ok, err = self._validate("ome.tiff", 800)
        assert ok and err is None

    def test_tiff_over_wsi_limit_rejected(self):
        ok, err = self._validate("huge.tiff", self.WSI_LIMIT_MB + 1)
        assert not ok

    def test_tif_uppercase_case_insensitive(self):
        ok, err = self._validate("scan.TIF", 100)
        assert ok

    def test_tiff_uppercase_case_insensitive(self):
        ok, err = self._validate("scan.TIFF", 200)
        assert ok

    def test_svs_not_treated_as_raster(self):
        """SVS must never be capped at 10 MB."""
        ok_as_wsi, _ = self._validate("slide.svs", 11)
        assert ok_as_wsi, "SVS should use WSI limit, not 10 MB raster limit"

