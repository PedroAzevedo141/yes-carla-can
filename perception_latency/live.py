"""Visible live comparison, with a separate decoder and inference worker."""
import csv
import hashlib
import json
import signal
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
import torch

from .backend import PROJECT, environment_manifest, load_delta, load_model, sync
from .live_sources import AvtpSource, CarlaSource, FileSource, LatestFrame
from .metrics import postprocess, preservation

MODES = ("clean", "attack", "random")


class LiveControl:
    def __init__(self, mode="clean"):
        self.lock = threading.Lock()
        self.mode = mode
        self.generation = 0
        self.events = [{"time": time.time(), "mode": mode, "generation": 0}]

    def snapshot(self):
        with self.lock:
            return self.mode, self.generation

    def select(self, mode):
        if mode not in MODES:
            raise ValueError(f"Condicao invalida: {mode}")
        with self.lock:
            if mode != self.mode:
                self.mode = mode
                self.generation += 1
                self.events.append({"time": time.time(), "mode": mode, "generation": self.generation})

    def toggle(self):
        mode, _ = self.snapshot()
        self.select("clean" if mode == "attack" else "attack")


class LiveState:
    def __init__(self):
        self.lock = threading.Lock()
        self.result = None
        self.status = "Carregando detector..."
        self.error = None
        self.processed = 0
        self.model_ready = False

    def publish(self, result):
        with self.lock:
            self.result = result
            self.processed += 1

    def snapshot(self, control):
        mode, generation = control.snapshot()
        with self.lock:
            # An in-flight old frame must never be labelled as the new mode.
            result = self.result if self.result is not None and self.result["generation"] == generation else None
            return result, self.status, self.error, self.processed


def infer(model, tensor, args, device):
    sync(device)
    start = time.perf_counter()
    raw = model(tensor)[0][0]
    sync(device)
    infer_end = time.perf_counter()
    boxes, stats = postprocess(raw, args.conf, args.iou, args.max_nms)
    end = time.perf_counter()
    return boxes.detach().cpu(), {**stats, "inference_ms": (infer_end - start) * 1000,
                                "detector_ms": (end - start) * 1000, "detections": len(boxes)}


def paired_frame(frame, mode, generation, model, device, delta, noise, args):
    started = time.perf_counter()
    rgb = cv2.resize(frame.rgb, (640, 640), interpolation=cv2.INTER_LINEAR)
    clean = torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1))).float().unsqueeze(0).to(device) / 255
    patch = delta if mode == "attack" else noise if mode == "random" else None
    active = clean if patch is None else (clean + patch).clamp(0, 1)
    inputs = {"clean": clean}
    if mode != "clean":
        inputs["active"] = active
    names = list(inputs)
    if frame.frame_id % 2:
        names.reverse()
    outputs = {name: infer(model, inputs[name], args, device) for name in names}
    if mode == "clean":
        outputs["active"] = outputs["clean"]
    fraction, matches, reference = preservation(outputs["clean"][0][:1000], outputs["active"][0], args.iou)
    pixels = np.rint(active[0].detach().cpu().permute(1, 2, 0).numpy() * 255).clip(0, 255).astype(np.uint8)
    finished = time.perf_counter()
    return {"generation": generation, "mode": mode, "frame_id": frame.frame_id,
            "arrived_at": frame.arrived_at, "source_time": frame.source_time, "completed_at": finished,
            "queue_wait_ms": (started - frame.arrived_at) * 1000,
            "pair_ms": (finished - started) * 1000, "preservation": fraction,
            "matches": matches, "reference": reference, "clean_rgb": rgb, "active_rgb": pixels,
            "source_aspect": frame.rgb.shape[1] / frame.rgb.shape[0],
            "clean_boxes": outputs["clean"][0], "active_boxes": outputs["active"][0],
            "clean": outputs["clean"][1], "active": outputs["active"][1]}


def row_for(result):
    row = {key: result[key] for key in ["frame_id", "generation", "mode", "source_time", "queue_wait_ms", "pair_ms", "preservation", "matches", "reference"]}
    row["time"] = time.time()
    for name in ["clean", "active"]:
        row.update({f"{name}_{key}": value for key, value in result[name].items()})
    return row


def worker(args, frames, control, state, stop, output):
    try:
        torch.set_num_threads(args.threads)
        model, device = load_model(args.device)
        delta = load_delta(args.delta).to(device)
        generator = torch.Generator().manual_seed(args.seed)
        noise = torch.rand(delta.shape, generator=generator).to(device) * (delta != 0)
        noise *= delta.norm() / noise.norm().clamp_min(1e-12)
        if noise.max().item() > 1:
            raise ValueError("O ruido de controle sairia de [0,1].")
        metadata = vars(args).copy()
        metadata.pop("func", None)
        metadata.update({"environment": environment_manifest(device),
                         "delta_sha256": hashlib.sha256(Path(args.delta).read_bytes()).hexdigest(),
                         "attack_stage": "digital input after decoding and 640x640 resize, before detector",
                         "comparison": "same source frame; alternating inference order; two inferences when active",
                         "timing": "synchronized individual detector wall time; local frame age is after arrival/decode, not source-to-display"})
        (output / "manifest.json").write_text(json.dumps(metadata, indent=2))
        with torch.no_grad():
            warm = torch.zeros(1, 3, 640, 640, device=device)
            for _ in range(args.warmup):
                infer(model, warm, args, device)
                infer(model, (warm + delta).clamp(0, 1), args, device)
        with state.lock:
            state.model_ready = True
            state.status = "Aguardando imagens da fonte..."
        windows = defaultdict(lambda: deque(maxlen=1000))
        totals = defaultdict(Counter)
        with (output / "frames.csv").open("w", newline="") as stream, torch.no_grad():
            writer = None
            while not stop.is_set():
                frame = frames.take()
                if frame is None:
                    continue
                mode, generation = control.snapshot()
                result = paired_frame(frame, mode, generation, model, device, delta, noise, args)
                result["names"] = model.names
                row = row_for(result)
                if writer is None:
                    writer = csv.DictWriter(stream, fieldnames=list(row))
                    writer.writeheader()
                writer.writerow(row)
                stream.flush()
                windows[mode].append(row)
                totals[mode].update({"frames": 1, "matches": result["matches"], "reference": result["reference"]})
                state.publish(result)
        # Small aggregate summary only; full per-frame evidence stays in CSV.
        summary = {}
        for mode in MODES:
            selected = list(windows[mode])
            if not selected:
                continue
            metrics = {key: float(np.median([r[key] for r in selected])) for key in ["clean_nms_ms", "active_nms_ms", "clean_detector_ms", "active_detector_ms", "clean_nms_input", "active_nms_input", "queue_wait_ms", "pair_ms"]}
            references = totals[mode]["reference"]
            summary[mode] = {"frames": totals[mode]["frames"], "median_window_frames": len(selected), "medians": metrics,
                             "preservation_micro": totals[mode]["matches"] / references if references else None}
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
    except Exception as exc:
        with state.lock:
            state.error = f"{type(exc).__name__}: {exc}"
            state.status = "Falha no detector"
        stop.set()


def run(args):
    if args.start_server:
        from .live_server import supervise
        return supervise(args)
    from .live_view import LiveView

    default_name = "live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6]
    output = Path(args.output or PROJECT / ".perception/runs" / default_name).resolve()
    if args.prepared_output:
        if not output.is_dir() or any((output / name).exists() for name in ["manifest.json", "frames.csv", "session.json"]):
            raise FileExistsError("Pasta preparada invalida ou ja usada.")
    else:
        output.mkdir(parents=True, exist_ok=False)
    # Validate a patch before starting a simulator or creating a window.
    load_delta(args.delta)
    frames, control, state, stop = LatestFrame(), LiveControl(args.mode), LiveState(), threading.Event()
    if args.source == "carla":
        source = CarlaSource(frames, args, output)
    elif args.source == "avtp":
        source = AvtpSource(frames, args.interface, args.stream_id)
    else:
        source = FileSource(frames, args.video, args.loop, args.fps)
    view = LiveView(args.headless)
    worker_thread = threading.Thread(target=worker, args=(args, frames, control, state, stop, output), daemon=True, name="perception-inference")
    previous_signals = {}
    for sig in [signal.SIGINT, signal.SIGTERM]:
        previous_signals[sig] = signal.signal(sig, lambda *_: stop.set())
    started = time.monotonic()
    next_toggle = float("inf")
    active_started = None
    saved, counts = set(), Counter()
    last_processed = -1
    last_state_write = 0
    fatal = None
    try:
        source.start()
        worker_thread.start()
        print(f"Painel iniciado. Resultados: {output}", flush=True)
        while not stop.is_set():
            if not view.events(control):
                break
            now = time.monotonic()
            if args.duration and active_started is not None and now - active_started >= args.duration:
                break
            if active_started is None and now - started > args.server_timeout + 30:
                fatal = "Nenhum frame processado dentro do tempo de inicializacao. Verifique a fonte e o simulador."
                break
            if now >= next_toggle:
                control.toggle()
                next_toggle = now + args.auto_toggle
            result, status, error, processed = state.snapshot(control)
            if source.error:
                fatal = source.error
                break
            if error:
                fatal = error
                break
            mode, generation = control.snapshot()
            if result is not None and active_started is None:
                active_started = now
                if args.auto_toggle:
                    next_toggle = now + args.auto_toggle
            if result is not None and processed != last_processed:
                counts[(mode, generation)] += 1
                last_processed = processed
            view.draw(source.label, mode, result, status, processed, frames.dropped, args.auto_toggle > 0)
            if view.screenshot_requested:
                view.save(output / ("snapshot-" + str(time.time_ns()) + ".png"))
                view.screenshot_requested = False
            if result is not None and counts[(mode, generation)] >= 3 and mode not in saved and args.snapshots:
                view.save(output / f"{mode}.png")
                saved.add(mode)
            if now - last_state_write >= .5:
                public = {"running": True, "source": args.source, "source_label": source.label, "mode": mode, "generation": generation,
                          "processed": processed, "model_ready": state.model_ready, "replaced_frames": frames.dropped,
                          "diagnostics": source.diagnostics(), "error": error}
                if result is not None:
                    public.update({k: result[k] for k in ["frame_id", "preservation", "queue_wait_ms", "pair_ms", "clean", "active"]})
                temporary = output / "status.tmp"
                temporary.write_text(json.dumps(public, indent=2))
                temporary.replace(output / "status.json")
                last_state_write = now
            if source.done.is_set() and source.error is None and result is not None and time.perf_counter() - result["arrived_at"] > 1:
                break
            if source.done.is_set() and source.received == 0 and state.model_ready:
                fatal = "A fonte encerrou sem produzir imagens validas."
                break
            view.tick()
        _, _, error, _ = state.snapshot(control)
        fatal = fatal or error
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
        frames.close()
        source.close()
        fatal = fatal or getattr(source, "cleanup_error", None)
        if any(thread.is_alive() for thread in source.threads):
            fatal = fatal or "Source worker did not stop within timeout"
        if worker_thread.ident is not None:
            worker_thread.join(timeout=10)
        if args.snapshots:
            view.save(output / "last.png")
        view.close()
        (output / "events.json").write_text(json.dumps(control.events, indent=2))
        session = {"source": args.source, "elapsed_seconds": time.monotonic() - started,
                   "displayed_seconds": time.monotonic() - active_started if active_started is not None else 0,
                   "processed": state.processed, "replaced_frames": frames.dropped,
                   "diagnostics": source.diagnostics(), "error": fatal,
                   "worker_stopped": not worker_thread.is_alive(), "snapshots": sorted(saved)}
        if worker_thread.is_alive():
            session["error"] = session["error"] or "Inference worker did not stop within timeout"
        (output / "session.json").write_text(json.dumps(session, indent=2))
        (output / "status.json").write_text(json.dumps({"running": False, **session}, indent=2))
        for sig, previous in previous_signals.items():
            signal.signal(sig, previous)
    if session["error"]:
        raise RuntimeError(f"{session['error']} | Diagnostico: {output}")
    print(f"Sessao encerrada: {state.processed} frames processados. Resultados: {output}", flush=True)
