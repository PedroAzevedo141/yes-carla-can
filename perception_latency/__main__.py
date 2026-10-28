import argparse
import hashlib
import json
from pathlib import Path


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Deve ser maior que zero.")
    return number


def extract(args):
    """Prepare an explicitly marked pilot split from an existing recording."""
    import av
    import cv2

    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    selected = []
    decode_errors = 0
    index = 0
    with av.open(args.source) as video:
        for packet in video.demux(video=0):
            try:
                frames = packet.decode()
            except av.error.InvalidDataError:
                decode_errors += 1
                continue
            for frame in frames:
                if frame.is_corrupt:
                    decode_errors += 1
                    continue
                if index % args.stride == 0:
                    target = out / f"frame_{index:06d}.png"
                    if not cv2.imwrite(str(target), frame.to_ndarray(format="bgr24")):
                        raise OSError(f"Falha ao gravar {target}")
                    selected.append({"path": target.name, "decoded_index": index, "pts": frame.pts})
                index += 1
    if len(selected) < 5:
        raise ValueError("São necessários ao menos cinco frames amostrados para o piloto.")
    # First 60% training, middle 20% temporal gap, last 20% pilot test.
    train_end = int(len(selected) * 0.6)
    test_begin = int(len(selected) * 0.8)
    for i, record in enumerate(selected):
        split = "train" if i < train_end else "test" if i >= test_begin else "gap"
        (out / split).mkdir(exist_ok=True)
        (out / record["path"]).rename(out / split / record["path"])
        record["split"] = split
    (out / "manifest.json").write_text(json.dumps({
        "source": str(Path(args.source).resolve()),
        "source_sha256": hashlib.sha256(Path(args.source).read_bytes()).hexdigest(),
        "decode_errors_or_corrupt_frames": decode_errors,
        "decoded_frames": index, "stride": args.stride, "frames": selected,
        "scope": "Pilot only: same short recording, temporally correlated. Full evaluation requires independent drives and BDD100K.",
    }, indent=2))
    print(f"{len(selected)} frames extraídos em {out}; erros de decodificação: {decode_errors}")


def doctor(args):
    import platform
    import torch
    import torchvision
    from .backend import load_model
    info = {"python": platform.python_version(), "torch": torch.__version__,
            "torchvision": torchvision.__version__, "cuda": torch.cuda.is_available()}
    if info["cuda"]:
        info["gpu"] = torch.cuda.get_device_name(0)
    if args.load_model:
        model, device = load_model(args.device)
        with torch.no_grad():
            raw = model(torch.zeros(1, 3, 640, 640, device=device))[0]
        info["prediction_shape"] = list(raw.shape)
    print(json.dumps(info, indent=2))


def split_images(args):
    """Sample without replacement and save a reproducible, disjoint split."""
    import random
    import shutil
    from .backend import image_files

    files = image_files(args.images)
    count = args.train_count + args.test_count
    if count > len(files):
        raise ValueError(f"São necessárias {count} imagens; a pasta contém {len(files)}.")
    selected = random.Random(args.seed).sample(files, count)
    records = [{"source": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in selected]
    if len({r["sha256"] for r in records}) != count:
        raise ValueError("A amostra contém imagens idênticas; escolha outra semente ou remova duplicatas.")
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    for name in ["train", "test"]:
        (out / name).mkdir()
    for i, (source, record) in enumerate(zip(selected, records)):
        split = "train" if i < args.train_count else "test"
        filename = f"{i:05d}_{source.name}"
        shutil.copy2(source, out / split / filename)
        record.update({"split": split, "file": f"{split}/{filename}"})
    (out / "manifest.json").write_text(json.dumps({
        "seed": args.seed, "train_count": args.train_count, "test_count": args.test_count,
        "source": str(Path(args.images).resolve()), "files": records,
        "scope": "Exploratory subset; no duplicate file hashes; scene independence depends on source dataset.",
    }, indent=2))
    print(f"Amostra salva em {out}: {args.train_count} treino, {args.test_count} teste.")


def main():
    parser = argparse.ArgumentParser(description="Groundswell / Yes CARLA CAN — laboratório de percepção")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("doctor", help="Verificar ambiente e modelo")
    p.add_argument("--load-model", action="store_true")
    p.add_argument("--device", default="cuda:0")
    p.set_defaults(func=doctor)
    p = commands.add_parser("extract", help="Extrair um piloto de uma gravação MPEG-TS")
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--stride", type=positive, default=12)
    p.set_defaults(func=extract)
    p = commands.add_parser("split", help="Separar uma amostra de treino e teste sem repetir arquivos")
    p.add_argument("--images", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--train-count", type=positive, default=160)
    p.add_argument("--test-count", type=positive, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(func=split_images)
    for name in ["baseline", "train", "evaluate"]:
        p = commands.add_parser(name)
        p.add_argument("--images", required=True)
        p.add_argument("--output", required=True)
        p.add_argument("--device", default="cuda:0")
        p.add_argument("--size", type=int, choices=[640], default=640)
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--threads", type=positive, default=4)
        if name == "train":
            p.set_defaults(func="train")
            p.add_argument("--epochs", type=positive, default=100)
            p.add_argument("--batch-size", type=positive, default=8)
            p.add_argument("--epsilon", type=float, default=70)
            p.add_argument("--alpha", type=float, default=0.0008)
            p.add_argument("--area-weight", type=float, default=10)
            p.add_argument("--ratio", type=float, choices=[0.0, 0.4, 0.8], default=0.4)
        else:
            p.set_defaults(func="evaluate")
            if name == "evaluate":
                p.add_argument("--delta", required=True)
            p.add_argument("--previews", type=int, default=3)
            p.add_argument("--repeats", type=positive, default=5)
            p.add_argument("--warmup", type=positive, default=10)
            p.add_argument("--conf", type=float, default=0.25)
            p.add_argument("--iou", type=float, default=0.45)
            p.add_argument("--max-nms", type=positive, default=30000)
            p.add_argument("--budget-ms", type=float, default=33.3333)
    p = commands.add_parser("live", help="Painel visual com comparacao ao vivo do mesmo frame")
    p.add_argument("--source", choices=["carla", "avtp", "file"], default="carla")
    p.add_argument("--delta", default=str(Path(__file__).resolve().parents[1] / ".perception/runs/bdd-r04/delta.npy"))
    p.add_argument("--output", default=None)
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--mode", choices=["clean", "attack", "random"], default="clean")
    p.add_argument("--threads", type=positive, default=4)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--warmup", type=positive, default=3)
    p.add_argument("--conf", type=float, default=.25)
    p.add_argument("--iou", type=float, default=.45)
    p.add_argument("--max-nms", type=positive, default=30000)
    p.add_argument("--duration", type=float, default=0, help="Encerrar N segundos apos o primeiro resultado; 0 = interativo")
    p.add_argument("--auto-toggle", type=float, default=0, help="Alternar limpa/ataque a cada N segundos (demonstracao)")
    p.add_argument("--headless", action="store_true", help="Renderizar para arquivos sem abrir janela")
    p.add_argument("--snapshots", action="store_true", help="Salvar uma imagem de cada condicao e a ultima tela")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=positive, default=2000)
    p.add_argument("--rolename", default="hero")
    p.add_argument("--actor-id", type=int, default=None)
    p.add_argument("--spawn-demo", action="store_true", help="Criar veiculo proprio se nao houver ego do Yes Carla Can")
    p.add_argument("--traffic", type=int, default=8, help="Veiculos criados apenas na demonstracao independente")
    p.add_argument("--start-server", action="store_true", help="Iniciar CARLA local se a porta nao estiver em uso")
    p.add_argument("--server-timeout", type=float, default=120)
    p.add_argument("--carla-dir", default=str(Path(__file__).resolve().parents[1] / "carla-0-9-15"))
    p.add_argument("--fps", type=float, default=15)
    p.add_argument("--interface", default="veth-r")
    p.add_argument("--stream-id", type=lambda v: int(v, 0), default=0xAABBCCDDEEFF0001)
    p.add_argument("--video", default=None)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--prepared-output", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func="live")
    args = parser.parse_args()
    if args.command == "train" and (args.epsilon <= 0 or args.alpha <= 0 or args.area_weight < 0):
        parser.error("epsilon/alpha devem ser positivos; area-weight deve ser não negativo.")
    if args.command in {"baseline", "evaluate"} and not (0 < args.conf < 1 and 0 < args.iou < 1 and args.budget_ms > 0):
        parser.error("conf/iou devem estar entre 0 e 1; budget-ms deve ser positivo.")
    if args.func == "live":
        import math
        values = [args.duration, args.auto_toggle, args.fps, args.server_timeout, args.conf, args.iou]
        if not all(math.isfinite(v) for v in values) or args.duration < 0 or args.auto_toggle < 0 or args.fps <= 0 or args.server_timeout <= 0 or args.traffic < 0:
            parser.error("Tempos/taxa devem ser finitos e validos; traffic deve ser nao negativo.")
        if not 0 < args.conf < 1 or not 0 < args.iou < 1:
            parser.error("conf e iou devem estar entre 0 e 1.")
        if args.source == "file" and not args.video:
            parser.error("--source file exige --video.")
        if args.source != "carla" and (args.start_server or args.spawn_demo):
            parser.error("--start-server/--spawn-demo sao usados apenas com --source carla.")
        from .live import run
        run(args)
    elif args.func == "train":
        from .train import train
        train(args)
    elif args.func == "evaluate":
        if args.previews < 0:
            parser.error("previews deve ser zero ou positivo.")
        from .evaluate import evaluate
        evaluate(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
