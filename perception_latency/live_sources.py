"""Bounded inputs for the live perception consumer. No Torch in callbacks."""
import io
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class LiveFrame:
    rgb: np.ndarray
    frame_id: int
    arrived_at: float
    source_time: float = None


class LatestFrame:
    """Keep one unconsumed frame, replacing old work rather than building a lag."""
    def __init__(self):
        self.condition = threading.Condition()
        self.value = None
        self.dropped = 0
        self.closed = False

    def publish(self, frame):
        with self.condition:
            if self.closed:
                return
            if self.value is not None:
                self.dropped += 1
            self.value = frame
            self.condition.notify()

    def take(self, timeout=.1):
        with self.condition:
            self.condition.wait_for(lambda: self.value is not None or self.closed, timeout)
            value, self.value = self.value, None
            return value

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class ByteStream(io.RawIOBase):
    """A bounded MPEG-TS stream for PyAV. Overflow is visible in diagnostics."""
    def __init__(self, capacity=4 * 1024 * 1024):
        super().__init__()
        self.capacity = capacity
        self.chunks = deque()
        self.size = 0
        self.dropped_bytes = 0
        self.condition = threading.Condition()
        self.ended = False

    def readable(self):
        return True

    def seekable(self):
        return False

    def write(self, data):
        with self.condition:
            if self.ended:
                return 0
            if self.size + len(data) > self.capacity:
                self.dropped_bytes += len(data)
                return 0
            self.chunks.append(bytes(data))
            self.size += len(data)
            self.condition.notify()
            return len(data)

    def read(self, size=-1):
        if size == 0:
            return b""
        with self.condition:
            self.condition.wait_for(lambda: self.chunks or self.ended)
            if not self.chunks:
                return b""
            limit = self.size if size < 0 else min(size, self.size)
            parts = []
            while limit and self.chunks:
                chunk = self.chunks.popleft()
                head, tail = chunk[:limit], chunk[limit:]
                parts.append(head)
                self.size -= len(head)
                limit -= len(head)
                if tail:
                    self.chunks.appendleft(tail)
            return b"".join(parts)

    def finish(self):
        with self.condition:
            self.ended = True
            self.condition.notify_all()

    def close(self):
        self.finish()
        super().close()


class SequenceCounter:
    def __init__(self):
        self.last = None
        self.missing = self.duplicates = self.reordered = 0

    def observe(self, value):
        if self.last is None:
            self.last = value
            return
        step = (value - self.last) & 255
        if step == 0:
            self.duplicates += 1
        elif step < 128:
            self.missing += step - 1
            self.last = value
        else:
            self.reordered += 1


class Source:
    label = ""

    def __init__(self, frames):
        self.frames = frames
        self.stop = threading.Event()
        self.done = threading.Event()
        self.threads = []
        self.error = None
        self.received = 0
        self.decode_errors = 0

    def launch(self, function, name):
        def guarded():
            try:
                function()
            except Exception as exc:
                if not self.stop.is_set():
                    self.error = str(exc)
            finally:
                self.done.set()
        thread = threading.Thread(target=guarded, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()

    def diagnostics(self):
        return {"received": self.received, "decode_errors": self.decode_errors, "error": self.error}

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3)


class FileSource(Source):
    label = "Gravacao (reproducao)"

    def __init__(self, frames, path, loop=False, fps=0):
        super().__init__(frames)
        self.path, self.loop, self.fps = path, loop, fps

    def start(self):
        self.launch(self.decode, "video-replay")

    def decode(self):
        import av
        while not self.stop.is_set():
            with av.open(self.path) as container:
                stream = container.streams.video[0]
                stream.codec_context.thread_count = 1
                fps = self.fps or float(stream.average_rate or 30)
                deadline = time.perf_counter()
                for packet in container.demux(stream):
                    if self.stop.is_set():
                        return
                    try:
                        decoded = packet.decode()
                    except av.error.InvalidDataError:
                        self.decode_errors += 1
                        continue
                    for frame in decoded:
                        if frame.is_corrupt:
                            self.decode_errors += 1
                            continue
                        if self.stop.wait(max(0, deadline - time.perf_counter())):
                            return
                        now = time.perf_counter()
                        self.received += 1
                        self.frames.publish(LiveFrame(frame.to_ndarray(format="rgb24"), self.received, now,
                                                      float(frame.time) if frame.time is not None else None))
                        deadline = max(deadline + 1 / fps, now)
            if not self.loop:
                return


class AvtpSource(Source):
    label = "AVTP / MPEG-TS ao vivo"

    def __init__(self, frames, interface, stream_id=None):
        super().__init__(frames)
        self.interface, self.stream_id = interface, stream_id
        self.stream = ByteStream()
        self.sequence = SequenceCounter()
        self.packets = 0
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x22F0))
        try:
            self.sock.bind((self.interface, 0))
            self.sock.settimeout(.2)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except Exception:
            self.sock.close()
            raise
        self.launch(self.capture, "avtp-capture")
        self.launch(self.decode, "avtp-decoder")

    def accept_packet(self, packet):
        from avtp_network.avtp import parse_mpegts_stream_packet
        if len(packet) < 26 or packet[12:14] != b"\x22\xf0":
            return
        if self.stream_id is not None and int.from_bytes(packet[18:26], "big") != self.stream_id:
            return
        payload = parse_mpegts_stream_packet(packet)
        if payload:
            self.packets += 1
            self.sequence.observe(packet[16])
            self.stream.write(payload)

    def capture(self):
        while not self.stop.is_set():
            try:
                packet, address = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            if len(address) > 2 and address[2] == getattr(socket, "PACKET_OUTGOING", 4):
                continue
            self.accept_packet(packet)

    def decode(self):
        import av
        try:
            with av.open(self.stream, format="mpegts", options={"probesize": "65536", "analyzeduration": "200000"}) as container:
                stream = container.streams.video[0]
                stream.codec_context.thread_count = 1
                for packet in container.demux(stream):
                    if self.stop.is_set():
                        return
                    try:
                        decoded = packet.decode()
                    except av.error.InvalidDataError:
                        self.decode_errors += 1
                        continue
                    for frame in decoded:
                        if frame.is_corrupt:
                            self.decode_errors += 1
                            continue
                        self.received += 1
                        self.frames.publish(LiveFrame(frame.to_ndarray(format="rgb24"), self.received, time.perf_counter(),
                                                      float(frame.time) if frame.time is not None else None))
        finally:
            self.stream.close()

    def diagnostics(self):
        return {**super().diagnostics(), "packets": self.packets, "missing_packets": self.sequence.missing,
                "duplicate_packets": self.sequence.duplicates, "reordered_packets": self.sequence.reordered,
                "dropped_bytes": self.stream.dropped_bytes}

    def close(self):
        self.stop.set()
        self.stream.close()
        if self.sock is not None:
            self.sock.close()
        super().close()


class CarlaSource(Source):
    label = "Camera CARLA ao vivo (conexao direta)"

    def __init__(self, frames, args, output):
        super().__init__(frames)
        self.args = args
        self.camera = None
        self.owned = []
        self.output = output
        from .live_server import ServerHandle
        self.server = ServerHandle()

    def start(self):
        def connect_and_cleanup():
            try:
                self.connect()
            finally:
                self.cleanup()
        self.launch(connect_and_cleanup, "carla-camera")

    def connect(self):
        import carla
        args = self.args
        self.server.ensure(args, self.stop, self.output)
        if self.stop.is_set():
            return
        self.client = carla.Client(args.host, args.port)
        self.client.set_timeout(5)
        deadline = time.monotonic() + args.server_timeout
        while not self.stop.is_set():
            try:
                world = self.client.get_world()
                break
            except RuntimeError:
                if time.monotonic() > deadline:
                    raise
                self.stop.wait(.5)
        if self.stop.is_set():
            return
        vehicles = list(world.get_actors().filter("vehicle.*"))
        if args.actor_id:
            ego = world.get_actor(args.actor_id)
            if ego is None or not ego.type_id.startswith("vehicle."):
                raise ValueError(f"Veiculo {args.actor_id} nao encontrado.")
        else:
            ego = next((v for v in vehicles if v.attributes.get("role_name") == args.rolename), None)
        if ego is None and args.spawn_demo:
            if world.get_settings().synchronous_mode:
                raise RuntimeError("A demonstracao independente precisa de mundo assincrono; o mundo atual nao foi alterado.")
            blueprints = world.get_blueprint_library()
            bp = blueprints.find("vehicle.tesla.model3")
            bp.set_attribute("role_name", "perception_demo")
            points = world.get_map().get_spawn_points()
            for point in points:
                if self.stop.is_set():
                    return
                ego = world.try_spawn_actor(bp, point)
                if ego:
                    break
            if ego is None:
                raise RuntimeError("Nao foi possivel criar o veiculo da demonstracao.")
            self.owned.append(ego)
            self.label = "CARLA ao vivo | demonstracao independente (sem CAN / AVTP)"
            ego.set_autopilot(True)
            origin = ego.get_location()
            traffic_bp = blueprints.find("vehicle.audi.a2")
            traffic_bp.set_attribute("role_name", "perception_demo_traffic")
            count = 0
            for point in sorted(points, key=lambda p: p.location.distance(origin)):
                if self.stop.is_set() or count >= args.traffic:
                    break
                actor = world.try_spawn_actor(traffic_bp, point)
                if actor:
                    self.owned.append(actor)
                    actor.set_autopilot(True)
                    count += 1
        if ego is None:
            raise RuntimeError("Veiculo do Yes Carla Can nao encontrado. Inicie o cliente ou use --spawn-demo para uma demonstracao independente.")
        if self.stop.is_set():
            return
        bp = world.get_blueprint_library().find("sensor.camera.rgb")
        for key, value in {"image_size_x": "960", "image_size_y": "400", "fov": "90", "sensor_tick": str(1 / args.fps)}.items():
            bp.set_attribute(key, value)
        # Match the project's RGB sensor mount without importing its CAN layer.
        transform = carla.Transform(carla.Location(x=ego.bounding_box.extent.x + .8,
                                                   z=ego.bounding_box.extent.z + .6), carla.Rotation(pitch=0))
        self.camera = world.spawn_actor(bp, transform, attach_to=ego, attachment_type=carla.AttachmentType.Rigid)
        self.camera.listen(self.on_image)
        while not self.stop.wait(.2):
            if not ego.is_alive:
                raise RuntimeError("O veiculo foi reiniciado; reabra o painel para anexar a nova camera.")

    def on_image(self, image):
        if self.stop.is_set():
            return
        bgra = np.frombuffer(image.raw_data, dtype=np.uint8).reshape(image.height, image.width, 4)
        rgb = np.ascontiguousarray(bgra[:, :, :3][:, :, ::-1])
        self.received += 1
        self.frames.publish(LiveFrame(rgb, int(image.frame), time.perf_counter(), float(image.timestamp)))

    def diagnostics(self):
        return {**super().diagnostics(), "cleanup_error": getattr(self, "cleanup_error", None)}

    def cleanup(self):
        import carla
        # Unregister autopilot vehicles before destruction. Destroy by IDs in
        # one batch, so stale Actor wrappers are never called individually.
        actor_ids = [actor.id for actor in self.owned]
        if self.camera is not None:
            try:
                self.camera.stop()
            except RuntimeError:
                pass
        if hasattr(self, "client"):
            try:
                if actor_ids:
                    self.client.apply_batch_sync([carla.command.SetAutopilot(actor_id, False) for actor_id in actor_ids], False)
                    self.client.get_world().wait_for_tick(1.0)
                destroy_ids = actor_ids + ([self.camera.id] if self.camera is not None else [])
                if destroy_ids:
                    self.client.apply_batch_sync([carla.command.DestroyActor(actor_id) for actor_id in destroy_ids], False)
            except RuntimeError as exc:
                self.cleanup_error = str(exc)
        self.camera = None
        self.owned.clear()
        self.server.close()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=12)
        self.server.close()
