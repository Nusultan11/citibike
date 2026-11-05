"""Thin Kaggle bootstrap: verify payload, install pinned runtime, call F4 code."""

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path


def main() -> None:
    bundles = list(Path("/kaggle/input").rglob("protocol.json"))
    if len(bundles) != 1:
        raise ValueError("Exactly one frozen BikeFlow payload required")
    bundle = bundles[0].parent
    protocol = json.loads(bundles[0].read_text())
    identity = hashlib.sha256(
        json.dumps(
            {k: v for k, v in protocol.items() if k != "id"},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()
    if identity != protocol["id"]:
        raise ValueError("Protocol checksum mismatch")
    for name, expected in protocol["checksums"].items():
        if (
            Path(name).name != name
            or hashlib.sha256((bundle / name).read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Payload checksum mismatch")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "-r",
            str(bundle / "requirements.txt"),
        ],
        check=True,
    )
    source = Path("/kaggle/working/bikeflow-source")
    source.mkdir()
    with zipfile.ZipFile(bundle / "source.zip") as archive:
        for name in archive.namelist():
            if not name.startswith("citibike/") or ".." in Path(name).parts:
                raise ValueError("Unexpected source archive path")
        archive.extractall(source)
    destination = Path("/kaggle/working/bikeflow-result")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "citibike.kaggle_research",
            "run",
            "--bundle",
            str(bundle),
            "--output",
            str(destination),
        ],
        check=True,
        env={
            **os.environ,
            "PYTHONPATH": str(source),
            "OMP_NUM_THREADS": "2",
            "OPENBLAS_NUM_THREADS": "2",
            "MKL_NUM_THREADS": "2",
        },
    )


if __name__ == "__main__":
    main()
