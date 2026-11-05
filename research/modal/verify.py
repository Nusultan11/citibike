"""Ephemeral Modal verification of exact Kaggle v2 outputs; never trains.

Use the separately installed Modal CLI; it is not an application dependency:
python -m modal run research/modal/verify.py
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import modal

ARCHIVE_SHA256 = "6ea773c52d352d17420d6c968336f427cc14a0e1ed486ecb6367a6f96ec93995"
PROTOCOL_SHA256 = "5181eb295d2526b418cee13af6839bcf22e0700340787511723f1d616f093dcb"
app = modal.App("bikeflow-f4-kaggle-independent-verify")
image = None
if modal.is_local():
    ROOT = Path(__file__).resolve().parents[2]
    PAYLOAD = ROOT / ".cache/kaggle-research/payload"
    image = (
        modal.Image.debian_slim(python_version="3.12.13")
        .apt_install("libgomp1")
        .pip_install_from_requirements(str(PAYLOAD / "requirements.txt"))
        .add_local_file(
            ROOT / ".cache/kaggle-research/kaggle-2.zip", "/inputs/kaggle-2.zip"
        )
    )
    # Exact allowlist: no directory mounts, credentials, database or raw data.
    protocol = json.loads((PAYLOAD / "protocol.json").read_text())
    for filename in ["protocol.json", *protocol["checksums"]]:
        if Path(filename).name != filename:
            raise ValueError("Unexpected payload path")
        image = image.add_local_file(PAYLOAD / filename, f"/inputs/payload/{filename}")


def extract_checked(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise ValueError("Archive CRC failure")
        if sum(entry.file_size for entry in zipped.infolist()) > 1024**3:
            raise ValueError("Archive expansion exceeds budget")
        for entry in zipped.infolist():
            path = Path(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                raise ValueError("Unsafe archive path")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive symlink rejected")
        zipped.extractall(destination)


@app.function(
    image=image,
    cpu=(2.0, 2.0),
    memory=(4096, 4096),
    timeout=900,
    startup_timeout=300,
    retries=0,
    max_containers=1,
    scaledown_window=2,
)
def verify_remote() -> dict:
    import resource

    started = time.monotonic()
    payload = Path("/inputs/payload")
    archive = Path("/inputs/kaggle-2.zip")
    for path, expected in (
        (archive, ARCHIVE_SHA256),
        (payload / "protocol.json", PROTOCOL_SHA256),
    ):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen input checksum mismatch")
    if platform.python_version() != "3.12.13":
        raise ValueError("Unexpected verifier Python")
    with tempfile.TemporaryDirectory(prefix="bikeflow-verify-") as directory:
        root = Path(directory)
        extract_checked(archive, root / "output")
        extract_checked(payload / "source.zip", root / "source")
        output = root / "verified.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "citibike.kaggle_research",
                "verify",
                "--bundle",
                str(payload),
                "--result",
                str(root / "output/bikeflow-result"),
                "--output",
                str(output),
            ],
            check=True,
            timeout=840,
            env={
                **os.environ,
                "PYTHONPATH": str(root / "source"),
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
            },
        )
        result = output.read_text()
        parsed = json.loads(result)
        if parsed["status"] != "VERIFIED" or parsed["verification"]["mismatches"]:
            raise ValueError("Remote verification failed")
        return {
            "report": result,
            "report_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "verification": parsed["verification"],
            "elapsed_seconds": time.monotonic() - started,
            "child_peak_rss_bytes": resource.getrusage(
                resource.RUSAGE_CHILDREN
            ).ru_maxrss
            * 1024,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "function_call_id": modal.current_function_call_id(),
        }


@app.local_entrypoint()
def main() -> None:
    destination = ROOT / ".cache/kaggle-research/modal-verified.json"
    if destination.exists():
        raise ValueError("Preserve existing remote verification output")
    result = verify_remote.remote()
    report = result.pop("report")
    destination.write_bytes(report.encode("utf-8"))
    result["app_id"] = app.app_id
    result["archive_sha256"] = ARCHIVE_SHA256
    result["protocol_sha256"] = PROTOCOL_SHA256
    result["resources_requested"] = {
        "cpu_request_and_limit": 2,
        "memory_request_and_limit_mib": 4096,
        "timeout_seconds": 900,
        "gpu": None,
        "retries": 0,
        "persistent_volumes": False,
    }
    destination.with_name("modal-summary.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result))
