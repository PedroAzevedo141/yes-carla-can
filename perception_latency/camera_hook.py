"""Optional digital perturbation before H.264; no model in the camera callback."""
import atexit
import csv
import os
import queue
import threading
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import cv2
import numpy as np


class CameraExperiment:
    def __init__(self):
        self.delta = None
        self.scaled = None
        self.output = None
        self.dropped = 0
        self.stride = int(os.environ.get("YES_CARLA_CAPTURE_STRIDE", "15"))
        if self.stride < 1:
            raise ValueError("YES_CARLA_CAPTURE_STRIDE deve ser positivo.")
        delta_path = os.environ.get("YES_CARLA_DELTA")
        if delta_path:
            delta = np.load(delta_path, allow_pickle=False)
            if delta.shape != (3, 640, 640) or not np.isfinite(delta).all() or delta.min() < 0 or delta.max() > 1:
                raise ValueError("YES_CARLA_DELTA deve conter um delta RGB float (3,640,640), finito em [0,1].")
            self.delta = delta.transpose(1, 2, 0).astype(np.float32)
        folder = os.environ.get("YES_CARLA_CAPTURE_DIR")
        if folder:
            root = Path(folder).resolve()
            root.mkdir(parents=True, exist_ok=True)
            session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
            self.output = root / session
            self.output.mkdir()
            self.pending = queue.Queue(maxsize=4)
            self.stop = threading.Event()
            self.thread = threading.Thread(target=self._write, daemon=True)
            self.thread.start()
            atexit.register(self.close)

    def process_bgra(self, bgra, frame_id, timestamp):
        # The recorded dataset is always the clean input, before perturbation.
        if self.output and frame_id % self.stride == 0:
            try:
                self.pending.put_nowait((frame_id, timestamp, bgra[:, :, :3].copy()))
            except queue.Full:
                self.dropped += 1
        if self.delta is None:
            return bgra
        height, width = bgra.shape[:2]
        if self.scaled is None or self.scaled.shape[:2] != (height, width):
            self.scaled = cv2.resize(self.delta, (width, height), interpolation=cv2.INTER_LINEAR)
        rgb = bgra[:, :, :3][:, :, ::-1].astype(np.float32) / 255
        attacked = np.clip(rgb + self.scaled, 0, 1)
        out = bgra.copy()
        out[:, :, :3] = np.rint(attacked[:, :, ::-1] * 255).astype(np.uint8)
        return out

    def _write(self):
        try:
            with (self.output / "frames.csv").open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["frame_id", "carla_timestamp", "file"])
                while not self.stop.is_set() or not self.pending.empty():
                    try:
                        frame_id, timestamp, bgr = self.pending.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    name = f"frame_{frame_id:08d}.png"
                    if not cv2.imwrite(str(self.output / name), bgr):
                        raise OSError(f"Falha ao gravar {name}")
                    writer.writerow([frame_id, timestamp, name])
                    stream.flush()
        except Exception as exc:
            self.error = str(exc)
            print(f"[perception capture] {exc}", flush=True)

    def close(self):
        if self.output and not self.stop.is_set():
            self.stop.set()
            self.thread.join(timeout=5)
            (self.output / "capture_status.txt").write_text(
                f"queue_drops={self.dropped}\npending={self.pending.qsize()}\nerror={getattr(self, 'error', '')}\n"
            )
