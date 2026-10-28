import json
import os
import signal
import struct
import subprocess
import threading
import time
from argparse import Namespace
from pathlib import Path

import pytest

from perception_latency import ycc_avtp
from perception_latency.live_sources import AvtpSource, LatestFrame


def test_privileged_command_drops_root_before_running_detector(monkeypatch):
    monkeypatch.setattr(ycc_avtp, "child_environment", lambda: ["HOME=/test/user", "DISPLAY=:0"])
    command = ycc_avtp.ethernet_command(["python", "-m", "perception_latency", "live"], "receiver")
    assert command[:7] == ["sudo", "-n", "--", "ip", "netns", "exec", "receiver"]
    assert command.index("setpriv") < command.index("python")
    assert f"--reuid={os.getuid()}" in command
    assert "--ambient-caps=+net_raw" in command
    assert "--no-new-privs" in command
    assert command[-4:] == ["python", "-m", "perception_latency", "live"]
    sender = ycc_avtp.ethernet_command(["python", "CARLA_client_module.py"])
    assert "netns" not in sender
    assert sender[-1] == "CARLA_client_module.py"


def verified_session():
    return {"source": "avtp", "processed": 12, "worker_stopped": True, "error": None,
            "diagnostics": {"packets": 300, "transport": "ethernet", "interface": "veth-r"}}


@pytest.mark.parametrize("missing", ["packets", "frames", "processed", "wrong-source", "worker", "error", "exit-code"])
def test_pipeline_does_not_claim_success_without_full_evidence(missing):
    session, can, code, error = verified_session(), {"frames": 20, "error": None}, 0, None
    if missing == "packets":
        session["diagnostics"]["packets"] = 0
    elif missing == "frames":
        can["frames"] = 0
    elif missing == "processed":
        session["processed"] = 0
    elif missing == "wrong-source":
        session["source"] = "carla"
    elif missing == "worker":
        session["worker_stopped"] = False
    elif missing == "error":
        error = "cleanup failed"
    else:
        code = 1
    assert not ycc_avtp.evidence(session, {}, can, code, error)["pipeline_verified"]


def test_pipeline_distinguishes_transport_from_attack_comparison():
    result = ycc_avtp.evidence(verified_session(), {"clean": {"frames": 12}}, {"frames": 20}, 0, None)
    assert result["pipeline_verified"]
    assert not result["paired_clean_attack_recorded"]
    result = ycc_avtp.evidence(verified_session(), {"clean": {"frames": 3}, "attack": {"frames": 9}}, {"frames": 20}, 0, None)
    assert result["paired_clean_attack_recorded"]


def test_check_under_no_new_privileges_never_invokes_sudo(monkeypatch, capsys):
    monkeypatch.setattr(ycc_avtp, "preflight", lambda _: {"ready": False, "issues": ["NoNewPrivs=1"]})
    monkeypatch.setattr(ycc_avtp.subprocess, "run", lambda *a, **k: pytest.fail("check must be read-only"))
    code = ycc_avtp.run(Namespace(check=True, duration=0, auto_toggle=0, server_timeout=30))
    assert code == 2
    assert json.loads(capsys.readouterr().out)["issues"] == ["NoNewPrivs=1"]


def test_owned_children_are_stopped_in_reverse_order_and_waited(monkeypatch, tmp_path):
    calls = []
    class Child:
        def __init__(self, command, **kwargs):
            self.name, self.code = command[0], None
            calls.append((self.name, "start"))
        def poll(self):
            return self.code
        def send_signal(self, sig):
            assert sig == signal.SIGINT
            calls.append((self.name, "signal"))
        def wait(self, timeout):
            calls.append((self.name, "wait"))
            self.code = 0
    monkeypatch.setattr(ycc_avtp.subprocess, "Popen", Child)
    children = ycc_avtp.Children(tmp_path)
    children.start("receiver", ["receiver"])
    children.start("carla-client", ["client"])
    assert children.close() == []
    assert calls == [("receiver", "start"), ("client", "start"), ("client", "signal"),
                     ("client", "wait"), ("receiver", "signal"), ("receiver", "wait")]


def test_avtp_shutdown_collects_real_socket_statistics_once():
    class Socket:
        def __init__(self):
            self.calls = []
        def getsockopt(self, level, option, size):
            self.calls.append((level, option, size))
            return struct.pack("II", 100, 3)
        def close(self):
            self.calls.append("close")
    source = AvtpSource(LatestFrame(), "veth-r")
    sock = source.sock = Socket()
    source.close()
    source.close()
    assert sock.calls == [(263, 6, 8), "close"]
    assert source.diagnostics()["kernel_dropped_packets"] == 3


def test_original_camera_encoder_roundtrip_through_avtp():
    """Exercise the actual sender encoder (not a separately configured codec)."""
    import numpy as np
    from avtp_network.avtp import fragment_mpegts_stream
    from sensors.camera import MPEGTSStreamEncoder
    source = AvtpSource(LatestFrame(), "veth-r", stream_id=42)
    encoder = MPEGTSStreamEncoder(width=960, height=400, fps=30)
    sequence = 252
    try:
        for number in range(8):
            bgra = np.full((400, 960, 4), number * 20, dtype=np.uint8)
            packets = fragment_mpegts_stream(encoder.encode_frame_to_ts_blocks(bgra), 42, sequence,
                                            "00:11:22:33:44:55", blocks_per_pkt=2)
            sequence = (sequence + len(packets)) & 255
            for packet in packets:
                source.accept_packet(packet)
    finally:
        encoder.close()
    source.stream.finish()
    source.decode()
    assert source.received == 8
    assert source.diagnostics()["decoded_resolution"] == [960, 400]
    assert source.diagnostics()["missing_packets"] == 0
    assert source.diagnostics()["mpegts_bytes"] > 0


def test_can_observer_records_only_received_data_frames(monkeypatch):
    import can
    class Bus:
        def __init__(self, **kwargs):
            self.channel = kwargs["channel"]
            self.messages = iter([can.Message(arbitration_id=42, data=b"\x01"),
                                  can.Message(arbitration_id=43, is_error_frame=True),
                                  can.Message(arbitration_id=44, is_remote_frame=True)])
        def recv(self, timeout):
            try:
                return next(self.messages)
            except StopIteration:
                time.sleep(.001)
        def shutdown(self):
            pass
    monkeypatch.setattr(can, "ThreadSafeBus", Bus)
    monitor = ycc_avtp.CanMonitor()
    monitor.start()
    deadline = time.monotonic() + 1
    while not monitor.counts and time.monotonic() < deadline:
        time.sleep(.001)
    report = monitor.close()
    assert report == {"interface": "vcan0", "frames": 1, "arbitration_ids": {"0x2a": 1}, "error": None}


def test_synchronous_or_existing_world_is_never_taken_over(monkeypatch):
    monkeypatch.setattr(ycc_avtp, "world_probe", lambda: {"heroes": [42], "synchronous": False})
    with pytest.raises(RuntimeError, match="--attach"):
        ycc_avtp.wait_world(Namespace(attach=False, server_timeout=1), threading.Event())


@pytest.mark.parametrize("attach", [False, True])
@pytest.mark.parametrize("viewer_code", [0, 1])
def test_ycc_lifecycle_observes_ethernet_and_preserves_external_clients(monkeypatch, tmp_path, attach, viewer_code):
    calls = []
    from perception_latency import backend
    import torch
    monkeypatch.setattr(backend, "load_delta", lambda _: None)
    monkeypatch.setattr(backend, "load_model", lambda _: (object(), None))
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: None)
    monkeypatch.setattr(ycc_avtp, "preflight", lambda _: {"ready": True})
    monkeypatch.setattr(ycc_avtp, "wait_world", lambda *_: {"heroes": [42] if attach else [], "synchronous": False})
    monkeypatch.setattr(ycc_avtp, "ethernet_command", lambda command, namespace=None: command)
    monkeypatch.setattr(ycc_avtp.subprocess, "run", lambda command, **_: calls.append(("network", command)))
    class Server:
        process = None
        def ensure(self, args, *_):
            assert args.start_server is not attach
            calls.append("server-ready")
        def close(self):
            calls.append("server-close")
    class Monitor:
        error = None
        counts = {42: 30}
        def start(self):
            calls.append("monitor-start")
        def close(self):
            calls.append("monitor-close")
            return {"frames": 30, "error": None}
    class Children:
        entries = []
        def __init__(self, *_):
            pass
        def start(self, name, command):
            calls.append(("start", name))
            if name == "receiver":
                assert command[command.index("--source") + 1] == "avtp"
                out = Path(command[command.index("--output") + 1])
                (out / "session.json").write_text(json.dumps(verified_session()))
                (out / "summary.json").write_text(json.dumps({"clean": {"frames": 3}, "attack": {"frames": 9}}))
            return Namespace(poll=lambda: viewer_code if name == "receiver" else 0)
        def failure_message(self, name, code):
            return f"{name}: {code}"
        def close(self):
            calls.append("clients-close")
            return ["receiver: codigo de encerramento 1"] if viewer_code else []
    monkeypatch.setattr(ycc_avtp, "ServerHandle", Server)
    monkeypatch.setattr(ycc_avtp, "CanMonitor", Monitor)
    monkeypatch.setattr(ycc_avtp, "Children", Children)
    args = Namespace(check=False, attach=attach, output=str(tmp_path / "run"), delta="unused", duration=1,
                     auto_toggle=1, headless=True, server_timeout=30)
    if viewer_code:
        with pytest.raises(RuntimeError, match="receiver: 1"):
            ycc_avtp.run(args)
    else:
        assert ycc_avtp.run(args) == 0
    assert calls.index("clients-close") < calls.index("server-close")
    starts = [value for key, value in (item for item in calls if isinstance(item, tuple)) if key == "start"]
    assert starts == (["receiver"] if attach else ["receiver", "carla-client", "controls"])
    setup_command = [value for key, value in (item for item in calls if isinstance(item, tuple)) if key == "network"][1]
    assert ("--check" in setup_command) is attach
    assert json.loads((tmp_path / "run/pipeline.json").read_text())["pipeline_verified"] is (viewer_code == 0)


def test_children_keep_controlling_terminal_with_redirected_logs(tmp_path):
    """Reproduce the terminal boundary that sudo timestamps depend on, no root."""
    import pty
    import sys
    master, slave = pty.openpty()
    harness = '''
import fcntl, json, os, sys, termios
from pathlib import Path
from perception_latency.ycc_avtp import Children
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
out = Path(sys.argv[1])
(out / 'parent.json').write_text(json.dumps({'sid': os.getsid(0)}))
children = Children(out)
code = "import json, os; fd=os.open('/dev/tty', os.O_RDONLY); print(json.dumps({'sid':os.getsid(0), 'pgrp':os.tcgetpgrp(fd)})); os.close(fd)"
child = children.start('terminal-probe', [sys.executable, '-c', code])
assert child.wait(timeout=5) == 0
assert children.close() == []
'''
    try:
        result = subprocess.run([sys.executable, "-c", harness, str(tmp_path)], stdin=slave,
                                stdout=slave, stderr=slave, start_new_session=True, timeout=10)
        assert result.returncode == 0, (tmp_path / "terminal-probe.log").read_text()
        parent = json.loads((tmp_path / "parent.json").read_text())
        child = json.loads((tmp_path / "terminal-probe.log").read_text())
        assert child["sid"] == parent["sid"] == child["pgrp"]
    finally:
        os.close(master)
        os.close(slave)
