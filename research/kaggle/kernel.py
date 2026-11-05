"""Thin Kaggle bootstrap: verify payload, install pinned runtime, call F4 code."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def prepare_runtime(
    requirements: Path, version: str, root: Path = Path("/tmp/bikeflow-runtime")
) -> Path:
    if tuple(map(int, version.split(".")[:2])) != (3, 12):
        raise ValueError("Unsupported project Python; expected frozen Python 3.12")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "uv==0.12.1"],
        check=True,
        timeout=180,
    )
    runtime_env = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_CACHE_DIR": str(root / "cache"),
    }
    uv = [sys.executable, "-m", "uv"]
    for command in (
        ["python", "install", version, "--no-bin"],
        ["venv", "--no-project", "--python", version, str(root / "venv")],
    ):
        subprocess.run(uv + command, check=True, env=runtime_env, timeout=240)
    python = root / "venv/bin/python"
    subprocess.run(
        uv + ["pip", "install", "--python", str(python), "-r", str(requirements)],
        check=True,
        env=runtime_env,
        timeout=300,
    )
    subprocess.run(
        [
            str(python),
            "-c",
            f"import platform; assert platform.python_version()=={version!r}",
        ],
        check=True,
        timeout=30,
    )
    frozen = subprocess.run(
        uv + ["pip", "freeze", "--python", str(python)],
        check=True,
        env=runtime_env,
        timeout=30,
        capture_output=True,
        text=True,
    )
    (requirements.parent.parent / "runtime-freeze.txt").write_text(
        frozen.stdout, encoding="utf-8"
    )
    return python


def main() -> None:
    bundles = list(Path("/kaggle/input").rglob("protocol.json"))
    if len(bundles) != 1:
        raise ValueError("Exactly one frozen BikeFlow payload required")
    received = bundles[0].parent
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
    # Kaggle expands .zip dataset files. Transport the same bytes as .bin and
    # restore the original frozen filename in an isolated working directory.
    bundle = Path("/kaggle/working/bikeflow-payload")
    bundle.mkdir()
    for name, expected in protocol["checksums"].items():
        remote_name = "source-archive.bin" if name == "source.zip" else name
        if (
            Path(name).name != name
            or hashlib.sha256((received / remote_name).read_bytes()).hexdigest()
            != expected
        ):
            raise ValueError("Payload checksum mismatch")
        shutil.copyfile(received / remote_name, bundle / name)
    shutil.copyfile(bundles[0], bundle / "protocol.json")
    python = prepare_runtime(
        bundle / "requirements.txt", protocol["environment"]["python"]
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
            str(python),
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
