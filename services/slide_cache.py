"""
OpenSlide C-library dynamic loader and slide caching infrastructure.
Provides thread-safe OpenSlide instance pooling and DeepZoom generators.
"""

from collections import OrderedDict
from contextlib import contextmanager
import os
from pathlib import Path
from threading import RLock

from core.config import TILE_OVERLAP, TILE_SIZE


def load_openslide():
    """Dynamically loads the OpenSlide C-library safely across platforms."""
    try:
        dll_path = os.getenv("OPENSLIDE_PATH")
        if os.name == "nt" and dll_path and hasattr(os, "add_dll_directory"):
            with os.add_dll_directory(dll_path):
                import openslide
                from openslide.deepzoom import DeepZoomGenerator
        else:
            import openslide
            from openslide.deepzoom import DeepZoomGenerator
        return openslide, DeepZoomGenerator, None
    except (ImportError, OSError) as exc:
        return None, None, str(exc)


openslide, DeepZoomGenerator, OPENSLIDE_IMPORT_ERROR = load_openslide()


class SlideCache:
    """Bounded LRU; caches OpenSlide handles and DeepZoomGenerator instances."""

    def __init__(self, capacity: int = 4):
        self.capacity = capacity
        self._entries = OrderedDict()
        self._cache_lock = RLock()

    def _get_entry(self, path: Path):
        key = str(path)
        with self._cache_lock:
            entry = self._entries.get(key)
            if entry is None:
                if openslide is None:
                    raise RuntimeError("OpenSlide is not available")
                slide = openslide.OpenSlide(key)
                deep_zoom = DeepZoomGenerator(slide, tile_size=TILE_SIZE, overlap=TILE_OVERLAP)
                entry = {
                    "slide": slide,
                    "deep_zoom": deep_zoom,
                    "lock": RLock(),
                }
                self._entries[key] = entry
                while len(self._entries) > self.capacity:
                    _, old_entry = self._entries.popitem(last=False)
                    old_entry["slide"].close()
            else:
                self._entries.move_to_end(key)
            return entry

    @contextmanager
    def acquire(self, path: Path):
        entry = self._get_entry(path)
        with entry["lock"]:
            yield entry["slide"]

    @contextmanager
    def acquire_generator(self, path: Path):
        entry = self._get_entry(path)
        with entry["lock"]:
            yield entry["deep_zoom"]

    def clear(self):
        with self._cache_lock:
            for entry in self._entries.values():
                entry["slide"].close()
            self._entries.clear()
        clear_tile_cache()
        clear_slide_meta_cache()


slide_cache = SlideCache(int(os.getenv("OPENSLIDE_CACHE_SIZE", "4")))

TILE_CACHE_CAPACITY = int(os.getenv("TILE_CACHE_CAPACITY", "1000"))
_tile_cache = OrderedDict()
_tile_cache_lock = RLock()


def get_cached_tile(slide_id: int, level: int, col: int, row: int):
    key = (slide_id, level, col, row)
    with _tile_cache_lock:
        if key in _tile_cache:
            _tile_cache.move_to_end(key)
            return _tile_cache[key]
    return None


def set_cached_tile(slide_id: int, level: int, col: int, row: int, data: bytes):
    key = (slide_id, level, col, row)
    with _tile_cache_lock:
        _tile_cache[key] = data
        if len(_tile_cache) > TILE_CACHE_CAPACITY:
            _tile_cache.popitem(last=False)


def clear_tile_cache():
    with _tile_cache_lock:
        _tile_cache.clear()


_slide_meta_cache = {}
_slide_meta_lock = RLock()


def get_cached_wsi_slide_helper(slide_id: int, loader_fn):
    with _slide_meta_lock:
        if slide_id in _slide_meta_cache:
            return _slide_meta_cache[slide_id]
    row, path = loader_fn(slide_id)
    with _slide_meta_lock:
        _slide_meta_cache[slide_id] = (row, path)
    return row, path


def clear_slide_meta_cache():
    with _slide_meta_lock:
        _slide_meta_cache.clear()
