"""Start an owned CARLA server only on explicit --start-server; reuse others."""
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class ServerHandle:
    def __init__(self):
        self.process = None
        self.log = None
        self.lock = threading.Lock()

    def ensure(self, args, stop, output):
        try:
            with socket.create_connection((args.host, args.port), timeout=.5):
                return
        except OSError:
            if not args.start_server:
                raise RuntimeError("Servidor CARLA indisponivel. Inicie o Yes Carla Can ou use --start-server --spawn-demo.")
        if args.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("--start-server inicia apenas um servidor local.")
        executable = Path(args.carla_dir).resolve() / "CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping"
        if not executable.is_file():
            raise FileNotFoundError(f"Simulador nao encontrado: {executable}")
        env = os.environ.copy()
        nvidia = Path("/usr/share/vulkan/icd.d/nvidia_icd.json")
        if nvidia.exists():
            env.setdefault("VK_ICD_FILENAMES", str(nvidia))
        with self.lock:
            if stop.is_set():
                return
            self.log = (output / "carla-server.log").open("w")
            self.process = subprocess.Popen(
                [str(executable), "CarlaUE4", "-RenderOffScreen", "-quality-level=Low", "-nosound", "-unattended", "-stdout", "-FullStdOutLogOutput", f"-carla-rpc-port={args.port}"],
                cwd=Path(args.carla_dir).resolve(), env=env, stdout=self.log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + args.server_timeout
        while not stop.wait(.5):
            process = self.process
            if process is None:
                return
            if process.poll() is not None:
                raise RuntimeError("O servidor CARLA encerrou ao iniciar. Consulte carla-server.log nesta execucao.")
            try:
                with socket.create_connection((args.host, args.port), timeout=.5):
                    return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("O servidor CARLA nao iniciou dentro do tempo configurado.")

    def close(self):
        with self.lock:
            process, self.process = self.process, None
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except ProcessLookupError:
                    pass
            if self.log is not None:
                self.log.close()
                self.log = None


def supervise(args):
    """Keep CARLA alive until the viewer process and its native TM have exited.

    CARLA's Traffic Manager owns C++ background threads. Stopping the server
    in the same process before Python teardown can abort those native threads.
    This parent never creates a CARLA client; only the child does.
    """
    from .backend import PROJECT, load_delta
    load_delta(args.delta)
    name = "live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6]
    output = Path(args.output or PROJECT / ".perception/runs" / name).resolve()
    output.mkdir(parents=True, exist_ok=False)
    server, stop = ServerHandle(), threading.Event()
    child = None
    previous = {}

    def interrupt(*_):
        stop.set()
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)

    for sig in [signal.SIGINT, signal.SIGTERM]:
        previous[sig] = signal.signal(sig, interrupt)
    code, started_server, server_pid = None, False, None
    try:
        print(f"Preparando servidor CARLA. Registros: {output}", flush=True)
        server.ensure(args, stop, output)
        started_server = server.process is not None
        server_pid = server.process.pid if server.process is not None else None
        if not stop.is_set():
            command = [sys.executable, "-m", "perception_latency", "live"]
            for key, value in vars(args).items():
                if key in {"command", "func", "start_server", "output", "prepared_output"} or value is None:
                    continue
                flag = "--" + key.replace("_", "-")
                if isinstance(value, bool):
                    if value:
                        command.append(flag)
                else:
                    command.extend([flag, str(value)])
            command.extend(["--output", str(output), "--prepared-output"])
            child = subprocess.Popen(command, cwd=PROJECT, start_new_session=True)
            cancelled_at = None
            while True:
                try:
                    code = child.wait(timeout=.5)
                    break
                except subprocess.TimeoutExpired:
                    if stop.is_set():
                        cancelled_at = cancelled_at or time.monotonic()
                        if time.monotonic() - cancelled_at > 30:
                            child.kill()
                            code = child.wait(timeout=5)
                            break
    finally:
        # The client process has finished before any owned server is stopped.
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                code = child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                code = child.wait(timeout=5)
        server.close()
        (output / "supervisor.json").write_text(json.dumps({"started_server": started_server,
            "server_pid": server_pid, "child_exit_code": code, "cancelled": stop.is_set()}, indent=2))
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if code not in (None, 0):
        raise RuntimeError(f"O painel encerrou com codigo {code}. Consulte {output}")
