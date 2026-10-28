"""Load the exact YOLOv5 bundled by the authors, without automatic downloads."""
import hashlib
import importlib
import os
import sys
from functools import partial
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import torch

PROJECT = Path(__file__).resolve().parents[1]
STATE = PROJECT / ".perception"
ARTIFACT = Path(os.environ.get("GROUNDSWELL_ARTIFACT", STATE / "artifacts/RPBAttack")).resolve()
WEIGHT_SHA256 = "8b3b748c1e592ddd8868022e8732fde20025197328490623cc16c6f24d0782ee"


def prepare_imports():
    os.environ["YOLOv5_AUTOINSTALL"] = "false"
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("MPLCONFIGDIR", str(STATE / "cache/matplotlib"))
    # The original YOLO code creates a configuration directory on import.
    os.environ.setdefault("YOLOV5_CONFIG_DIR", str(STATE / "cache/yolov5"))
    if str(ARTIFACT) not in sys.path:
        sys.path.insert(0, str(ARTIFACT))
    runtime = str(STATE / "python")
    if runtime not in sys.path:
        sys.path.insert(0, runtime)


def load_model(device="cuda:0"):
    prepare_imports()
    device = torch.device(device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA indisponível. Use --device cpu apenas para testes funcionais.")
    weights = ARTIFACT / "local_yolos/yolov5/weights/yolov5s.pt"
    if not weights.is_file():
        raise FileNotFoundError("Artefato ausente. Execute: python scripts/fetch_groundswell.py")
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    if WEIGHT_SHA256 and digest != WEIGHT_SHA256:
        raise ValueError("O checkpoint difere do artefato verificado.")
    module = importlib.import_module("local_yolos.yolov5.models.experimental")
    # This legacy checkpoint contains a serialized nn.Module. Only the pinned,
    # checksum-verified author checkpoint is loaded this way, in a scoped call.
    with patch("torch.load", partial(torch.load, weights_only=False)):
        model = module.attempt_load(str(weights), device=device)
    model.eval().requires_grad_(False)
    return model, device


def environment_manifest(device, batch_size=1):
    """Record actual source and runtime, including locally adapted vendor files."""
    import platform
    from importlib.metadata import version, PackageNotFoundError
    packages = {}
    for name in ["torch", "torchvision", "numpy", "opencv-python", "av", "matplotlib", "pandas", "seaborn"]:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    source_hashes = {}
    for name in ["attack/uap_RPB.py", "local_yolos/yolov5/models/yolo.py", "local_yolos/yolov5/utils/general.py"]:
        path = ARTIFACT / name
        if path.is_file():
            source_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"python": platform.python_version(), "packages": packages, "torch_build": torch.__version__,
            "cuda_build": torch.version.cuda, "device": str(device),
            "gpu": torch.cuda.get_device_name(device) if device.type == "cuda" else None,
            "artifact_root": str(ARTIFACT), "artifact_source_sha256": source_hashes,
            "model_weight_sha256": WEIGHT_SHA256,
            "batch_size": batch_size, "precision": "FP32"}


def read_rgb(path, size=640):
    image = cv2.imread(str(path))
    if image is None:
        raise ValueError(f"Imagem inválida: {path}")
    # Match the artifact's A.Resize: square resize, RGB, float [0, 1].
    image = cv2.resize(image, (size, size), interpolation=cv2.INTER_LINEAR)
    return torch.from_numpy(np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1))).float() / 255


def sync(device):
    if device.type == "cuda":
        torch.cuda.synchronize(device)


def image_files(folder):
    files = sorted(p for p in Path(folder).rglob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg"})
    if not files:
        raise ValueError(f"Nenhuma imagem encontrada: {folder}")
    return files


def load_delta(path, size=640):
    delta = np.load(path, allow_pickle=False)
    if delta.shape != (3, size, size) or not np.isfinite(delta).all():
        raise ValueError(f"Perturbação deve ser finita, RGB, shape (3, {size}, {size}).")
    if delta.min() < 0 or delta.max() > 1:
        raise ValueError("O artefato usa uma perturbação aditiva no intervalo [0, 1].")
    return torch.from_numpy(delta.astype(np.float32))
