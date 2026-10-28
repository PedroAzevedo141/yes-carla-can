import numpy as np
import pytest
torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")

from perception_latency.backend import ARTIFACT, load_delta, prepare_imports
from perception_latency.camera_hook import CameraExperiment
from perception_latency.metrics import postprocess, preservation


def predictions():
    # Same-class overlapping boxes, a separate box, two classes on one box,
    # low objectness, and confidence below the product threshold.
    return torch.tensor([
        [50, 50, 20, 20, .9, .9, .8],
        [51, 51, 20, 20, .8, .9, .1],
        [150, 150, 20, 20, .8, .1, .8],
        [250, 250, 20, 20, .2, .99, .99],
        [350, 350, 20, 20, .8, .2, .1],
    ], dtype=torch.float32)


def test_nms_matches_author_multilabel():
    if not ARTIFACT.is_dir():
        pytest.skip("Optional Groundswell artifact is not installed")
    prepare_imports()
    from local_yolos.yolov5.utils.general import non_max_suppression
    raw = predictions()
    expected = non_max_suppression(raw[None].clone(), .25, .45, max_det=30000)[0]
    actual, stats = postprocess(raw, .25, .45)
    assert torch.equal(actual, expected)
    assert stats["qualified_proposals"] == 3
    assert stats["expanded_candidates"] == 4
    assert stats["nms_input"] == 4


def test_candidate_limit_is_applied_before_nms():
    result, stats = postprocess(predictions(), max_nms=1)
    assert stats["expanded_candidates"] == 4
    assert stats["nms_input"] == 1
    assert len(result) == 1


def test_empty_nms():
    result, stats = postprocess(torch.zeros(2, 7))
    assert result.shape == (0, 6)
    assert stats["nms_input"] == 0


def test_preservation_requires_class_and_overlap():
    clean = torch.tensor([[0, 0, 10, 10, .9, 2], [20, 20, 30, 30, .9, 1]])
    attacked = torch.tensor([[0, 0, 10, 10, .9, 2], [20, 20, 30, 30, .9, 2]])
    assert preservation(clean, attacked) == (.5, 1, 2)
    assert preservation(clean[:0], attacked) == (None, 0, 0)
    assert preservation(clean, attacked[:0]) == (0, 0, 2)


def test_invalid_delta_rejected(tmp_path):
    target = tmp_path / "delta.npy"
    np.save(target, np.zeros((640, 640, 3)))
    with pytest.raises(ValueError):
        load_delta(target)
    invalid = np.zeros((3, 640, 640), dtype=np.float32)
    invalid[0, 0, 0] = np.nan
    np.save(target, invalid)
    with pytest.raises(ValueError):
        load_delta(target)


def test_disabled_camera_hook_preserves_pixels(monkeypatch):
    monkeypatch.delenv("YES_CARLA_DELTA", raising=False)
    monkeypatch.delenv("YES_CARLA_CAPTURE_DIR", raising=False)
    hook = CameraExperiment()
    image = np.array([[[10, 20, 30, 255]]], dtype=np.uint8)
    assert hook.process_bgra(image, 0, 0) is image


def test_camera_patch_rgb_order_alpha_and_clean_capture(monkeypatch, tmp_path):
    import cv2
    delta = np.zeros((3, 640, 640), dtype=np.float32)
    delta[0] = .1  # red channel, not blue
    patch = tmp_path / "delta.npy"
    np.save(patch, delta)
    folder = tmp_path / "capture"
    monkeypatch.setenv("YES_CARLA_DELTA", str(patch))
    monkeypatch.setenv("YES_CARLA_CAPTURE_DIR", str(folder))
    monkeypatch.setenv("YES_CARLA_CAPTURE_STRIDE", "1")
    hook = CameraExperiment()
    image = np.full((4, 6, 4), 100, dtype=np.uint8)
    result = hook.process_bgra(image, 1, .1)
    hook.close()
    assert np.all(result[:, :, 2] == 126)
    assert np.array_equal(result[:, :, [0, 1, 3]], image[:, :, [0, 1, 3]])
    assert np.all(image == 100)
    saved = cv2.imread(str(hook.output / "frame_00000001.png"))
    assert np.array_equal(saved, image[:, :, :3])


def test_report_distinguishes_frames_from_repeated_measurements():
    from perception_latency.report import summarize
    base = {"condition": "clean", "qualified_proposals": 2, "expanded_candidates": 2,
            "nms_input": 2, "detections": 1, "inference_ms": 2.0,
            "nms_ms": .2, "postprocess_ms": .3, "detector_ms": 2.3,
            "clean_reference": 1, "matched_clean": 1}
    rows = [dict(base, frame=f"frame{i % 2}.png") for i in range(6)]
    result = summarize(rows, 33.3)["clean"]
    assert result["samples"] == 6
    assert result["unique_frames"] == 2
    assert result["preservation_micro"] == 1.0
    assert result["budget_exceeded_fraction"] == 0.0


def test_camera_restart_uses_separate_sessions(monkeypatch, tmp_path):
    monkeypatch.delenv("YES_CARLA_DELTA", raising=False)
    monkeypatch.setenv("YES_CARLA_CAPTURE_DIR", str(tmp_path / "captures"))
    first, second = CameraExperiment(), CameraExperiment()
    try:
        assert first.output != second.output
    finally:
        first.close()
        second.close()


def test_dataset_split_never_reuses_a_file_between_training_and_test(tmp_path):
    from argparse import Namespace
    import json
    from perception_latency.__main__ import split_images
    source = tmp_path / "source"
    source.mkdir()
    for i in range(5):
        (source / f"{i}.png").write_bytes(f"unique sample {i}".encode())
    output = tmp_path / "split"
    split_images(Namespace(images=str(source), output=str(output), train_count=3, test_count=2, seed=42))
    manifest = json.loads((output / "manifest.json").read_text())
    train = {r["sha256"] for r in manifest["files"] if r["split"] == "train"}
    test = {r["sha256"] for r in manifest["files"] if r["split"] == "test"}
    assert len(train) == 3 and len(test) == 2
    assert train.isdisjoint(test)
