#!/usr/bin/env python3
"""Fetch a pinned research artifact, never execute its installer on download."""
import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

RECORD = "20397573"
URL = f"https://zenodo.org/records/{RECORD}/files/RPBAttack.zip"
SHA256 = "ea6411ef3d462ac733c8cc1897e5c3c30a4659958278baba8d49c5db8b10bf0d"


def main():
    parser = argparse.ArgumentParser(description="Baixar o artefato Groundswell 20397573 usado pelo adaptador.")
    parser.add_argument("--archive", type=Path, help="ZIP já baixado; verificação de hash continua obrigatória.")
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / ".perception/artifacts"
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "RPBAttack").exists():
        raise FileExistsError("Artefato já existe; nenhum arquivo foi substituído.")
    with tempfile.TemporaryDirectory(prefix="groundswell-", dir=destination) as temporary:
        staging = Path(temporary)
        archive = args.archive
        if archive is None:
            archive = staging / "RPBAttack.zip"
            print(f"Baixando {URL} (aproximadamente 683 MB)...", flush=True)
            with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise ValueError("Hash do ZIP diferente da versão fixada.")
        unpack = staging / "unpack"
        unpack.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            if sum(x.file_size for x in bundle.infolist()) > 2 * 1024**3:
                raise ValueError("Tamanho inesperado do artefato.")
            for entry in bundle.infolist():
                target = (unpack / entry.filename).resolve()
                if unpack.resolve() not in target.parents or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Caminho inválido no ZIP: {entry.filename}")
            for entry in bundle.infolist():
                if ".git" not in Path(entry.filename).parts and "__pycache__" not in Path(entry.filename).parts:
                    bundle.extract(entry, unpack)
        (unpack / "RPBAttack").rename(destination / "RPBAttack")
    (destination / "provenance.json").write_text(json.dumps({"record": RECORD, "url": URL, "archive_sha256": SHA256}, indent=2))
    print(f"Artefato verificado: {destination / 'RPBAttack'}")


if __name__ == "__main__":
    main()
