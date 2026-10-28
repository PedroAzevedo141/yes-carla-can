import io
import json
import threading
import time
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")

from perception_latency.live import LiveControl, LiveState, paired_frame
from perception_latency.live_sources import AvtpSource, ByteStream, LatestFrame, LiveFrame, SequenceCounter
from perception_latency.live_server import ServerHandle


def frame(number=1):
    rgb = np.full((48, 64, 3), 50, dtype=np.uint8)
    return LiveFrame(rgb, number, time.perf_counter())


def test_input_is_bounded_and_keeps_newest_frame():
    slot = LatestFrame()
    for number in range(20):
        slot.publish(frame(number))
    assert slot.dropped == 19
    assert slot.take().frame_id == 19
    assert slot.take(timeout=.001) is None
    slot.close()
    slot.publish(frame(100))
    assert slot.take(timeout=.001) is None


def test_closing_input_wakes_waiter():
    slot, seen = LatestFrame(), []
    thread = threading.Thread(target=lambda: seen.append(slot.take(timeout=2)))
    thread.start()
    slot.close()
    thread.join(timeout=.5)
    assert not thread.is_alive()
    assert seen == [None]


def test_bounded_byte_stream_overflow_and_partial_reads():
    stream = ByteStream(capacity=8)
    assert stream.write(b"abcdef") == 6
    assert stream.write(b"XYZ") == 0
    assert stream.dropped_bytes == 3
    assert stream.read(4) == b"abcd"
    assert stream.write(b"ghij") == 4
    stream.finish()
    assert stream.read(10) == b"efghij"
    assert stream.read(1) == b""
    stream.close()


def test_control_generation_prevents_mislabelled_inflight_frame():
    control, state = LiveControl(), LiveState()
    state.publish({"generation": 0, "mode": "clean"})
    assert state.snapshot(control)[0]["mode"] == "clean"
    control.select("clean")
    assert control.snapshot() == ("clean", 0)
    control.toggle()
    assert state.snapshot(control)[0] is None
    # A result already in flight retains its original label and is rejected.
    state.publish({"generation": 0, "mode": "clean"})
    assert state.snapshot(control)[0] is None
    state.publish({"generation": 1, "mode": "attack"})
    assert state.snapshot(control)[0]["mode"] == "attack"
    with pytest.raises(ValueError):
        control.select("unknown")


def test_sequence_counter_wrap_gap_duplicate_and_old_packet():
    sequence = SequenceCounter()
    for value in [254, 255, 0, 2, 2, 1, 3]:
        sequence.observe(value)
    assert sequence.missing == 1
    assert sequence.duplicates == 1
    assert sequence.reordered == 1
    assert sequence.last == 3


class FakeDetector:
    def __init__(self):
        self.inputs = []

    def __call__(self, tensor):
        self.inputs.append(tensor.clone())
        return (torch.tensor([[[320, 320, 80, 80, .9, .9, .1]]], device=tensor.device),)


def test_live_comparison_uses_same_frame_and_applies_delta_only_to_active_input():
    model = FakeDetector()
    args = Namespace(conf=.25, iou=.45, max_nms=30000)
    delta = torch.full((3, 640, 640), .1)
    noise = torch.zeros_like(delta)
    result = paired_frame(frame(2), "attack", 7, model, torch.device("cpu"), delta, noise, args)
    assert len(model.inputs) == 2
    assert torch.allclose(model.inputs[1] - model.inputs[0], torch.full_like(model.inputs[0], .1))
    assert result["mode"] == "attack" and result["generation"] == 7
    assert result["preservation"] == 1
    assert result["clean"]["nms_input"] == 1
    assert result["active"]["nms_input"] == 1
    model.inputs.clear()
    paired_frame(frame(3), "clean", 8, model, torch.device("cpu"), delta, noise, args)
    assert len(model.inputs) == 1


def test_avtp_fragmented_video_reaches_live_decoder():
    av = pytest.importorskip("av")
    from avtp_network.avtp import fragment_mpegts_stream

    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mpegts") as container:
        stream = container.add_stream("libx264", rate=10, options={"tune": "zerolatency", "preset": "ultrafast"})
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for index in range(6):
            image = av.VideoFrame.from_ndarray(np.full((48, 64, 3), index * 30, dtype=np.uint8), format="rgb24")
            image.pts = index
            for packet in stream.encode(image):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    raw = buffer.getvalue()
    blocks = b"".join(b"\0" * 4 + raw[i:i + 188] for i in range(0, len(raw), 188))
    packets = fragment_mpegts_stream(blocks, stream_id=123, seq_counter=254, src_mac="00:11:22:33:44:55")
    frames = LatestFrame()
    source = AvtpSource(frames, "unused", stream_id=123)
    wrong_stream = AvtpSource(LatestFrame(), "unused", stream_id=124)
    for packet in packets:
        source.accept_packet(packet)
        wrong_stream.accept_packet(packet)
    assert wrong_stream.packets == 0
    source.stream.finish()
    source.decode()
    assert source.received == 6
    assert source.sequence.missing == 0
    assert frames.take().rgb.shape == (48, 64, 3)
    assert frames.dropped == 5


def test_existing_server_is_reused_without_process_ownership(monkeypatch, tmp_path):
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr("perception_latency.live_server.socket.create_connection", lambda *_args, **_kwargs: Connection())
    def forbidden(*args, **kwargs):
        raise AssertionError("An existing server must not be replaced or stopped")
    monkeypatch.setattr("perception_latency.live_server.subprocess.Popen", forbidden)
    monkeypatch.setattr("perception_latency.live_server.os.killpg", forbidden)
    handle = ServerHandle()
    handle.ensure(Namespace(host="127.0.0.1", port=2000, start_server=True), threading.Event(), tmp_path)
    assert handle.process is None
    handle.close()


def test_visible_controls_and_renderer_with_sdl_dummy(monkeypatch, tmp_path):
    pg = pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    from perception_latency.live_view import LiveView
    view = LiveView(headless=False)
    control = LiveControl()
    try:
        pg.event.post(pg.event.Event(pg.KEYDOWN, key=pg.K_SPACE))
        assert view.events(control)
        assert control.snapshot()[0] == "attack"
        pg.event.post(pg.event.Event(pg.MOUSEBUTTONDOWN, button=1, pos=view.buttons["clean"].center))
        assert view.events(control)
        assert control.snapshot()[0] == "clean"
        view.draw("Fonte de teste", "clean", None, "Carregando...", 0, 0, False)
        target = tmp_path / "panel.png"
        view.save(target)
        assert target.stat().st_size > 1000
    finally:
        view.close()


def test_carla_cleanup_unregisters_autopilot_before_destroying_owned_ids(monkeypatch):
    from types import SimpleNamespace
    import sys
    from perception_latency.live_sources import CarlaSource
    calls = []
    fake_carla = SimpleNamespace(command=SimpleNamespace(
        SetAutopilot=lambda actor_id, enabled: ("autopilot", actor_id, enabled),
        DestroyActor=lambda actor_id: ("destroy", actor_id)))
    monkeypatch.setitem(sys.modules, "carla", fake_carla)
    source = CarlaSource.__new__(CarlaSource)
    source.owned = [SimpleNamespace(id=11), SimpleNamespace(id=12)]
    source.camera = SimpleNamespace(id=13, stop=lambda: calls.append("stop-camera"))
    source.client = SimpleNamespace(
        apply_batch_sync=lambda commands, tick: calls.append(commands),
        get_world=lambda: SimpleNamespace(wait_for_tick=lambda timeout: calls.append("tick")))
    source.server = SimpleNamespace(close=lambda: calls.append("close-server"))
    source.cleanup()
    assert calls == ["stop-camera", [("autopilot", 11, False), ("autopilot", 12, False)],
                     "tick", [("destroy", 11), ("destroy", 12), ("destroy", 13)], "close-server"]
    assert source.owned == [] and source.camera is None


def test_supervisor_stops_owned_server_only_after_client_process_exits(monkeypatch, tmp_path):
    from perception_latency.live_server import supervise
    calls = []
    class FakeServer:
        process = None
        def ensure(self, args, stop, output):
            calls.append("server-ready")
            self.process = Namespace(pid=123)
        def close(self):
            calls.append("stop-server")
    class Child:
        code = None
        def __init__(self, command, **kwargs):
            calls.append("start-client")
            assert "--start-server" not in command
            assert "--prepared-output" in command
        def wait(self, timeout):
            calls.append("client-exited")
            self.code = 0
            return 0
        def poll(self):
            return self.code
    monkeypatch.setattr("perception_latency.live_server.ServerHandle", FakeServer)
    monkeypatch.setattr("perception_latency.live_server.subprocess.Popen", Child)
    monkeypatch.setattr("perception_latency.backend.load_delta", lambda path: None)
    supervise(Namespace(output=str(tmp_path / "run"), delta="unused", source="carla", start_server=True,
                        command="live", func="live", prepared_output=False, headless=False))
    assert calls == ["server-ready", "start-client", "client-exited", "stop-server"]
    assert json.loads((tmp_path / "run/supervisor.json").read_text())["child_exit_code"] == 0
