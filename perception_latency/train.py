"""CLI adapter using the author's actual RPB gradient and boundary methods."""
import contextlib
import csv
import hashlib
import io
import json
import os
import random
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

from .backend import ARTIFACT, environment_manifest, image_files, load_model, read_rgb


def train(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.set_num_threads(args.threads)
    model, device = load_model(args.device)
    from attack import uap_RPB as original

    files = image_files(args.images)
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    metadata = vars(args).copy()
    metadata.pop("func", None)
    metadata.update({
        "backend": "author-20397573-RPB-methods-with-adapted-training-loop",
        "artifact": str(ARTIFACT), "torch": torch.__version__, "environment": environment_manifest(device, batch_size=args.batch_size),
        "gpu": torch.cuda.get_device_name(device) if device.type == "cuda" else None,
        "images": [{"path": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files],
        "notes": ["No annotations used: preserve clean model detections.",
                  "No artifact early stopping; fixed epoch count; epoch-level mask update retained.",
                  "Positive-only additive patch, as in artifact 20397573; exact float NPY export."],
    })
    (out / "manifest.json").write_text(json.dumps(metadata, indent=2))
    # The original constructor creates relative experiment directories. Scope
    # its working directory; its model is injected to reuse our pinned loader.
    before = Path.cwd()
    try:
        os.chdir(out)
        with patch.object(original, "get_model", return_value=model):
            trainer = original.UAPRPB(
                patch_folder="author", train_loader=None, val_loader=None,
                epsilon=args.epsilon, iter_eps=args.alpha, lambda_1=1,
                lambda_2=args.area_weight, patch_size=(args.size, args.size),
                models_vers=[5], epochs=args.epochs, threshold_ratio=args.ratio,
                rate=0.01, use_cuda=device.type == "cuda",
            )
    finally:
        os.chdir(before)
    delta = torch.zeros(3, args.size, args.size, device=device, requires_grad=True)
    order = list(range(len(files)))
    with (out / "loss.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["epoch", "loss", "l2", "middle_start", "middle_end"])
        writer.writeheader()
        for epoch in range(args.epochs):
            random.shuffle(order)
            trainer.current_train_loss = 0.0
            batches = 0
            for begin in range(0, len(order), args.batch_size):
                x = torch.stack([read_rgb(files[i], args.size) for i in order[begin:begin + args.batch_size]]).to(device)
                # Reuse the artifact's losses, detection-ratio calculation,
                # sign update, deadband, minimum band, and delayed mask update.
                with contextlib.redirect_stdout(io.StringIO()):
                    delta = trainer.fastGradientSignMethod_mask(delta, x, None, epsilon=args.alpha)
                # Zero-norm guard and graph detach are engineering fixes; the
                # L2 radius and positive-only clipping match artifact 20397573.
                norm = delta.norm()
                factor = min(1.0, args.epsilon / max(norm.item(), 1e-12))
                delta = (delta * factor).clamp(0, 1).detach().requires_grad_(True)
                batches += 1
            trainer.current_middle_start = trainer.middle_start
            trainer.current_middle_end = trainer.middle_end
            row = {"epoch": epoch + 1, "loss": trainer.current_train_loss / batches,
                   "l2": delta.norm().item(), "middle_start": trainer.middle_start,
                   "middle_end": trainer.middle_end}
            writer.writerow(row)
            stream.flush()
            np.save(out / "delta.npy", delta.detach().cpu().numpy())
            if epoch == 0 or (epoch + 1) % 10 == 0 or epoch + 1 == args.epochs:
                print(json.dumps(row), flush=True)
    print(f"Perturbação salva: {out / 'delta.npy'}")
