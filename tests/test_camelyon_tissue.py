import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "camelyon"))

from tissue import clean_tissue_mask, schedule_patches, tissue_mask


def test_blank_background_has_no_tissue():
    image = np.full((64, 64, 3), 255, dtype=np.uint8)
    assert not tissue_mask(image).any()


def test_pale_tissue_is_kept_and_two_fragments_are_scheduled():
    image = np.full((80, 160, 3), 255, dtype=np.uint8)
    image[10:40, 10:40] = (210, 170, 180)
    image[10:40, 110:150] = (190, 140, 160)
    mask = clean_tissue_mask(tissue_mask(image), radius=1)
    assert mask[20, 20] and mask[20, 130]
    assert not mask[0, 0]
    # One mask pixel represents 224 level-0 pixels, so both fragments are separate patches.
    schedule = schedule_patches(mask, 160 * 224, 80 * 224, downsample=224, max_patches=100000)
    xs = {patch["x"] for patch in schedule["selected"]}
    assert any(x < 80 * 224 for x in xs)
    assert any(x >= 80 * 224 for x in xs)
    assert all(0 <= patch["x"] and patch["x"] + 224 <= 160 * 224 for patch in schedule["selected"])
    assert all(0 <= patch["y"] and patch["y"] + 224 <= 80 * 224 for patch in schedule["selected"])


def test_patch_limit_is_recorded_as_partial_coverage():
    mask = np.ones((4, 8), dtype=bool)
    schedule = schedule_patches(mask, 8 * 224, 4 * 224, downsample=224, max_patches=3)
    assert schedule["eligible_count"] == 32
    assert schedule["partial"] is True
    assert len(schedule["selected"]) <= 3
    assert schedule["coverage_fraction"] < 1
    assert schedule["selected"][0]["x"] == 0
    assert schedule["selected"][-1]["x"] > schedule["selected"][0]["x"]
