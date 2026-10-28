"""Clean baseline and paired clean/random/attack measurements."""
import csv
import hashlib
import json
import random
import time
from pathlib import Path

import torch

from .backend import environment_manifest, image_files, load_delta, load_model, read_rgb, sync
from .metrics import postprocess, preservation
from .report import save_preview, write_report


def evaluate(args):
    torch.set_num_threads(args.threads)
    random.seed(args.seed)
    files = image_files(args.images)
    out = Path(args.output).resolve()
    if out.exists():
        raise FileExistsError(f"Escolha uma nova pasta de saída: {out}")
    delta_path = getattr(args, "delta", None)
    delta = load_delta(delta_path, args.size) if delta_path else None
    file_manifest = [{"path": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    if delta_path:
        train_manifest = Path(delta_path).parent / "manifest.json"
        if train_manifest.exists():
            train_hashes = {x["sha256"] for x in json.loads(train_manifest.read_text())["images"]}
            overlap = train_hashes & {x["sha256"] for x in file_manifest}
            if overlap:
                raise ValueError(f"{len(overlap)} imagens de teste também aparecem no treino.")
    model, device = load_model(args.device)
    conditions = {"clean": None}
    metadata = {k: v for k, v in vars(args).items() if k != "func"}
    metadata.update({"environment": environment_manifest(device), "files": file_manifest,
                     "timing": "synchronized wall time; batch=1 FP32; IO and perturbation application excluded",
                     "resize": "RGB square INTER_LINEAR; matches author A.Resize",
                     "multi_label": True, "max_det": 30000, "reference_max_det": 1000})
    if delta is not None:
        delta = delta.to(device)
        # Match support and L2 intensity with one fixed universal noise control.
        gen = torch.Generator().manual_seed(args.seed)
        noise = torch.rand(delta.shape, generator=gen).to(device) * (delta != 0)
        noise *= delta.norm() / noise.norm().clamp_min(1e-12)
        if noise.max().item() > 1:
            raise ValueError("O controle aleatório sairia de [0,1]; reduza a norma da perturbação.")
        conditions.update({"random": noise, "attack": delta})
        metadata.update({"delta_sha256": hashlib.sha256(Path(delta_path).read_bytes()).hexdigest(),
                         "random_l2": noise.norm().item(), "delta_l2": delta.norm().item()})
    out.mkdir(parents=True)
    (out / "previews").mkdir()
    (out / "manifest.json").write_text(json.dumps(metadata, indent=2))
    rows = []
    with (out / "frames.csv").open("w", newline="") as stream, torch.no_grad():
        writer = None
        warm = read_rgb(files[0], args.size).unsqueeze(0).to(device)
        for _ in range(args.warmup):
            for d in conditions.values():
                x = warm if d is None else (warm + d).clamp(0, 1)
                raw = model(x)[0][0]
                postprocess(raw, args.conf, args.iou, args.max_nms)
        for repeat in range(args.repeats):
            for frame_index, path in enumerate(files):
                start = time.perf_counter()
                clean = read_rgb(path, args.size).unsqueeze(0).to(device)
                sync(device)
                preprocess_ms = (time.perf_counter() - start) * 1000
                names = list(conditions)
                random.shuffle(names)
                detections, frame_rows, preview_images = {}, {}, {}
                for name in names:
                    d = conditions[name]
                    x = clean if d is None else (clean + d).clamp(0, 1)
                    sync(device)
                    start = time.perf_counter()
                    raw = model(x)[0][0]
                    sync(device)
                    infer_end = time.perf_counter()
                    det, metrics = postprocess(raw, args.conf, args.iou, args.max_nms)
                    end = time.perf_counter()
                    detections[name] = det.detach().cpu()
                    frame_rows[name] = {"repeat": repeat, "frame": str(path.resolve()),
                                        "condition": name, "decode_resize_upload_ms": preprocess_ms,
                                        "inference_ms": (infer_end - start) * 1000,
                                        "detector_ms": (end - start) * 1000,
                                        **metrics, "detections": len(det)}
                    if repeat == 0 and frame_index < args.previews:
                        preview_images[name] = x.detach().cpu()
                for name in names:
                    ratio, matched, total = preservation(detections["clean"][:1000], detections[name], args.iou)
                    row = frame_rows[name]
                    row.update({"preservation": ratio, "matched_clean": matched, "clean_reference": total})
                    if writer is None:
                        writer = csv.DictWriter(stream, fieldnames=list(row))
                        writer.writeheader()
                    writer.writerow(row)
                    rows.append(row)
                stream.flush()
                for name, image in preview_images.items():
                    save_preview(image, detections[name], model.names, out / "previews" / f"{frame_index:04d}_{path.stem}_{name}.png")
                print(f"{repeat + 1}/{args.repeats}: {path.name}", flush=True)
    summary = write_report(out, rows, args.budget_ms, metadata)
    for name, values in summary.items():
        print(f"{name}: NMS={values['nms_ms']['median']:.3f} ms; total={values['detector_ms']['median']:.3f} ms; candidatos={values['nms_input']['mean']:.1f}")
    print(f"Relatório: {out / 'report.md'}")
