"""Run the original YCC clients and consume their Ethernet video, with evidence.

The supervisor never creates a CARLA client or runs as root. Privileged entry
into the receiver namespace is followed by dropping back to the invoking user;
only CAP_NET_RAW is retained for the Ethernet sockets.
"""
import argparse
import importlib.util
import json
import math
import os
import pwd
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .live_server import ServerHandle

PROJECT = Path(__file__).resolve().parents[1]


def register_parser(commands):
    parser = commands.add_parser("ycc-avtp", help="Iniciar o YCC e comparar o video recebido por Ethernet AVTP")
    parser.add_argument("--check", action="store_true", help="Verificar requisitos sem criar interfaces ou iniciar processos")
    parser.add_argument("--attach", action="store_true", help="Usar o YCC ja iniciado; abrir somente o painel e o monitor CAN")
    parser.add_argument("--output")
    parser.add_argument("--delta", default=str(PROJECT / ".perception/runs/bdd-r04/delta.npy"))
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--auto-toggle", type=float, default=0)
    parser.add_argument("--headless", action="store_true", help="Ocultar apenas o painel; os clientes originais continuam graficos")
    parser.add_argument("--server-timeout", type=float, default=120)
    parser.set_defaults(func="ycc-avtp")


def privilege_status():
    status = Path("/proc/self/status").read_text()
    return any(line.split() == ["NoNewPrivs:", "1"] for line in status.splitlines())


def preflight(args):
    missing_modules = [name for name in ["torch", "torchvision", "carla", "can", "cantools", "pygame", "av", "scapy", "tkinter"]
                       if importlib.util.find_spec(name) is None]
    missing_tools = [name for name in ["sudo", "ip", "setpriv", "bash", "modprobe"] if shutil.which(name) is None]
    issues = []
    if os.geteuid() == 0:
        issues.append("Inicie como seu usuario normal; o CARLA nao pode ser executado como root.")
    if privilege_status():
        issues.append("Esta sessao tem NoNewPrivs=1 e impede sudo. Execute o launcher no seu terminal local.")
    if missing_modules or missing_tools:
        issues.append(f"Dependencias ausentes: {missing_modules + missing_tools}")
    if os.environ.get("YES_CARLA_DELTA"):
        issues.append("Desative YES_CARLA_DELTA: a referencia recebida precisa estar limpa.")
    if not os.environ.get("DISPLAY"):
        issues.append("DISPLAY ausente; os clientes graficos originais do YCC precisam da sessao desktop.")
    if not Path(args.delta).is_file():
        issues.append(f"Perturbacao ausente: {args.delta}")
    artifact = Path(os.environ.get("GROUNDSWELL_ARTIFACT", PROJECT / ".perception/artifacts/RPBAttack"))
    if not (artifact / "local_yolos/yolov5/weights/yolov5s.pt").is_file():
        issues.append("Checkpoint YOLOv5s ausente. Consulte docs/perception-latency.md.")
    if not args.attach and not (PROJECT / "carla-0-9-15/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping").is_file():
        issues.append("Instalacao local do CARLA ausente.")
    gpu = None
    if "torch" not in missing_modules:
        import torch
        if torch.cuda.is_available():
            gpu = torch.cuda.get_device_name(0)
        else:
            issues.append("CUDA indisponivel; este launcher mede o detector na GPU.")
    return {"ready": not issues, "issues": issues, "gpu": gpu, "python": sys.executable,
            "transport": "Ethernet AVTP: host/veth-s -> switch/br0 -> receiver/veth-r",
            "can_interface": "vcan0", "network_creation": "sudo; existing topology is preserved"}


def child_environment():
    # Pass desktop and experiment settings explicitly through sudo; keep all files
    # and graphics processes owned by the original desktop user.
    names = ["DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
             "PYTHONPATH", "MPLCONFIGDIR", "YOLOV5_CONFIG_DIR", "MPLBACKEND", "GROUNDSWELL_ARTIFACT",
             "CUDA_VISIBLE_DEVICES", "LD_LIBRARY_PATH", "SDL_VIDEODRIVER"]
    user = pwd.getpwuid(os.getuid())
    values = {name: os.environ[name] for name in names if name in os.environ}
    values.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name,
                  PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", YOLOv5_AUTOINSTALL="false")
    return [f"{key}={value}" for key, value in values.items()]


def ethernet_command(command, namespace=None):
    prefix = ["sudo", "-n", "--"]
    if namespace:
        prefix += ["ip", "netns", "exec", namespace]
    return prefix + ["setpriv", f"--reuid={os.getuid()}", f"--regid={os.getgid()}", "--init-groups",
                     "--inh-caps=+net_raw", "--ambient-caps=+net_raw", "--no-new-privs",
                     "env", *child_environment(), *command]


class Children:
    def __init__(self, output):
        self.output, self.entries = output, []

    def start(self, name, command):
        log = (self.output / f"{name}.log").open("w")
        try:
            # Keep the controlling terminal used by sudo -v. A detached session
            # cannot reuse sudo's per-terminal authentication timestamp.
            child = subprocess.Popen(command, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, start_new_session=False)
        except BaseException:
            log.close()
            raise
        self.entries.append((name, child, log))
        return child

    def check(self, except_process=None):
        for name, child, _ in self.entries:
            if child is not except_process and child.poll() is not None:
                raise RuntimeError(self.failure_message(name, child.returncode))

    def failure_message(self, name, code):
        path = self.output / f"{name}.log"
        try:
            detail = path.read_text(errors="replace")[-2000:].strip()
        except OSError:
            detail = ""
        return f"{name} encerrou com codigo {code}. Consulte {path}." + (f"\n{detail}" if detail else "")

    def close(self):
        # Keep CARLA running until every client (including native TM threads) exits.
        errors = []
        for name, child, log in reversed(self.entries):
            try:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                    try:
                        child.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        child.terminate()
                        child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{name}: {exc}")
            finally:
                log.close()
            if child.poll() not in (None, 0):
                errors.append(f"{name}: codigo de encerramento {child.poll()}")
        return errors


class CanMonitor:
    def __init__(self):
        self.bus, self.thread = None, None
        self.stop = threading.Event()
        self.counts, self.error = Counter(), None

    def start(self):
        import can
        self.bus = can.ThreadSafeBus(interface="socketcan", channel="vcan0", receive_own_messages=False)
        def receive():
            try:
                while not self.stop.is_set():
                    message = self.bus.recv(timeout=.2)
                    if message is not None and not message.is_error_frame and not message.is_remote_frame:
                        self.counts[hex(message.arbitration_id)] += 1
            except Exception as exc:
                self.error = str(exc)
        self.thread = threading.Thread(target=receive, name="can-evidence", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.bus is not None:
            self.bus.shutdown()
        return {"interface": "vcan0", "frames": sum(self.counts.values()), "arbitration_ids": dict(self.counts), "error": self.error}


def world_probe():
    # A separate process owns all native CARLA client state, as in live_server.
    code = """import carla, json
client = carla.Client('127.0.0.1', 2000)
client.set_timeout(5)
world = client.get_world()
print(json.dumps({'map': world.get_map().name, 'synchronous': world.get_settings().synchronous_mode,
 'heroes': [a.id for a in world.get_actors().filter('vehicle.*') if a.attributes.get('role_name') == 'hero']}))
"""
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode:
        return None
    return json.loads(result.stdout.strip().splitlines()[-1])


def wait_world(args, stop):
    deadline = time.monotonic() + args.server_timeout
    while not stop.is_set():
        world = world_probe()
        if world is not None:
            if not args.attach and (world["heroes"] or world["synchronous"]):
                raise RuntimeError("O CARLA ja tem um hero ou esta sincrono. Use --attach com o YCC existente.")
            if args.attach and not world["heroes"]:
                raise RuntimeError("--attach exige o veiculo hero do YCC ja em execucao.")
            return world
        if time.monotonic() > deadline:
            raise TimeoutError("O CARLA nao respondeu. Consulte carla-server.log.")
        stop.wait(1)
    raise InterruptedError("Inicializacao cancelada.")


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def evidence(session, summary, can, viewer_code, error):
    diagnostics = session.get("diagnostics", {})
    verified = (session.get("source") == "avtp" and diagnostics.get("transport") == "ethernet"
                and diagnostics.get("interface") == "veth-r"
                and viewer_code == 0 and error is None and session.get("error") is None
                and session.get("worker_stopped") is True and diagnostics.get("packets", 0) > 0
                and session.get("processed", 0) > 0 and can.get("frames", 0) > 0 and can.get("error") is None)
    return {"pipeline_verified": bool(verified), "paired_clean_attack_recorded": all(summary.get(mode, {}).get("frames", 0) > 0 for mode in ["clean", "attack"]),
            "transport": "ethernet_namespace", "attack_stage": "after AVTP decode and resize, before YOLOv5s",
            "viewer_exit_code": viewer_code, "avtp": diagnostics, "processed": session.get("processed", 0),
            "can": can, "error": error or session.get("error")}


def run(args):
    if any(not math.isfinite(v) or v < 0 for v in [args.duration, args.auto_toggle]) or not math.isfinite(args.server_timeout) or args.server_timeout <= 0:
        raise ValueError("Duracao e alternancia devem ser nao negativas; timeout deve ser positivo e finito.")
    report = preflight(args)
    if args.check:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ready"] else 2
    if not report["ready"]:
        raise RuntimeError("\n".join(report["issues"]))
    from .backend import load_delta, load_model
    load_delta(args.delta)
    model, device = load_model("cuda:0")
    del model
    import torch
    torch.cuda.empty_cache()
    name = "live-avtp-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6]
    output = Path(args.output or PROJECT / ".perception/runs" / name).resolve()
    # Validate an explicit output location before touching the network.
    if output.exists():
        raise FileExistsError(f"Pasta de resultados ja existe: {output}")
    # Authenticate in the user's terminal before starting background children.
    subprocess.run(["sudo", "-v"], check=True)
    setup = ["sudo", "-n", "--", "bash", str(PROJECT / "scripts/setup_perception_network.sh")]
    subprocess.run(setup + (["--check"] if args.attach else []), check=True)
    output.mkdir(parents=True, exist_ok=False)
    viewer_output = output / "perception"
    viewer_output.mkdir()
    children, monitor, server, stop = Children(output), CanMonitor(), ServerHandle(), threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in [signal.SIGINT, signal.SIGTERM]}
    viewer_code, failure, world = None, None, None
    started_server, server_pid = False, None
    try:
        print(f"Iniciando YCC com receptor AVTP. Registros: {output}", flush=True)
        server_args = argparse.Namespace(host="127.0.0.1", port=2000, start_server=not args.attach,
            carla_dir=str(PROJECT / "carla-0-9-15"), server_timeout=args.server_timeout)
        server.ensure(server_args, stop, output)
        started_server = server.process is not None
        server_pid = server.process.pid if started_server else None
        world = wait_world(args, stop)
        monitor.start()
        command = [sys.executable, "-m", "perception_latency", "live", "--source", "avtp", "--interface", "veth-r",
                   "--delta", str(Path(args.delta).resolve()), "--output", str(viewer_output), "--prepared-output", "--snapshots",
                   "--duration", str(args.duration), "--auto-toggle", str(args.auto_toggle), "--server-timeout", str(args.server_timeout)]
        if args.headless:
            command.append("--headless")
        viewer = children.start("receiver", ethernet_command(command, "receiver"))
        if not args.attach:
            children.start("carla-client", ethernet_command([sys.executable, "-u", str(PROJECT / "CARLA_client_module.py"), "--vcan", "vcan0"]))
            # CAN sockets need no Ethernet capability and remain in the host namespace.
            children.start("controls", [sys.executable, "-u", str(PROJECT / "vehicle_controls_module.py"), "--vcan", "vcan0"])
        last_progress = 0
        while not stop.wait(.5):
            viewer_code = viewer.poll()
            if viewer_code is not None:
                if viewer_code:
                    raise RuntimeError(children.failure_message("receiver", viewer_code))
                break
            children.check(except_process=viewer)
            if monitor.error:
                raise RuntimeError(f"Falha no monitor CAN: {monitor.error}")
            if time.monotonic() - last_progress > 10:
                status = read_json(viewer_output / "status.json")
                print(f"AVTP: {status.get('diagnostics', {}).get('packets', 0)} pacotes | "
                      f"comparacoes: {status.get('processed', 0)} | CAN: {sum(monitor.counts.values())} frames", flush=True)
                last_progress = time.monotonic()
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup_errors = children.close()
        can = monitor.close()
        # A nonzero exit code is an error, but the process has still exited.
        # Preserve the server only when a client really remains alive.
        alive_children = [name for name, child, _ in children.entries if child.poll() is None]
        if not alive_children:
            server.close()
        if cleanup_errors:
            failure = failure or "; ".join(cleanup_errors)
        if viewer_code is None:
            viewer_code = next((child.poll() for name, child, _ in children.entries if name == "receiver"), None)
        result = evidence(read_json(viewer_output / "session.json"), read_json(viewer_output / "summary.json"), can, viewer_code, failure)
        result.update(started_server=started_server, server_pid=server_pid, world=world,
                      attach=args.attach, cancelled=stop.is_set(), cleanup_errors=cleanup_errors,
                      alive_children=alive_children,
                      children={name: child.poll() for name, child, _ in children.entries}, network_preserved=True)
        (output / "pipeline.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"Evidencia do fluxo completo: {output / 'pipeline.json'}", flush=True)
    if failure:
        raise RuntimeError(f"{failure} | Registros: {output}")
    if not result["pipeline_verified"] and not stop.is_set():
        raise RuntimeError("O fluxo completo ainda nao foi comprovado: verifique pipeline.json.")
    return 0
