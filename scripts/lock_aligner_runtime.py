#!/usr/bin/env python
"""Regenerate ``src/cplus_align/runtime_lock.json`` — the aligner's pinned runtime.

    python scripts/lock_aligner_runtime.py

Needs network access and pip. Resolves the aligner's dependencies for
CPython 3.12 on Linux x86_64 and aarch64 (binary wheels only), and records
every wheel by URL, SHA-256 and size, plus the model at a fixed Hugging Face
revision. The sidecar downloads exactly these files and nothing else, with no
index access, so an install is reproducible and every byte is checked.

To upgrade something, change the pins below, rerun, and commit the lock. Test
the result with a real install before shipping: a new torch or transformers
can change how the model loads.

torch comes from PyTorch's CPU-only index by direct URL. The torch wheel on PyPI
is the CUDA build, which pulls in several GB of NVIDIA libraries.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PYTHON = "3.12"
TORCH = "2.14.0"
REQUIREMENTS = [
    "transformers==5.17.0",
    "nltk==3.10.3",
    # Imported by ctc-forced-aligner's package __init__, so needed even though
    # nothing here romanizes text.
    "uroman==1.3.1.1",
    "imageio-ffmpeg==0.6.0",
    "numpy",
]
MODEL_REPO = "MahmoudAshraf/mms-300m-1130-forced-aligner"
MODEL_REVISION = "49402e9577b1158620820667c218cd494cc44486"
MODEL_FILES = [
    "config.json",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "vocab.json",
    # Only the safetensors copy: the repo holds the same weights twice.
    "model.safetensors",
]
ARCHES = ("x86_64", "aarch64")
#: The newest glibc the image may run on is Debian 12's 2.36. pip does not widen
#: a --platform to older manylinux tags on its own, so every one is listed.
GLIBC = 36


def platforms(arch: str) -> list[str]:
    tags = [f"manylinux_2_{minor}_{arch}" for minor in range(GLIBC, 16, -1)]
    tags.append(f"manylinux2014_{arch}")
    if arch == "x86_64":
        tags += ["manylinux2010_x86_64", "manylinux1_x86_64"]
    return [arg for tag in tags for arg in ("--platform", tag)]
TORCH_INDEX = "https://download.pytorch.org/whl/cpu/torch/"

OUT = Path(__file__).resolve().parent.parent / "src" / "cplus_align" / "runtime_lock.json"


def http(url: str, method: str = "GET") -> urllib.request.addinfourl:
    request = urllib.request.Request(url, method=method, headers={"User-Agent": "cplus-lock"})
    return urllib.request.urlopen(request, timeout=60)


def size_of(url: str) -> int:
    with http(url, "HEAD") as response:
        return int(response.headers["Content-Length"])


def torch_wheel(arch: str) -> dict:
    page = http(TORCH_INDEX).read().decode()
    name = f"torch-{TORCH}%2Bcpu-cp312-cp312-manylinux_2_28_{arch}.whl"
    match = re.search(re.escape(name) + r"#sha256=([0-9a-f]{64})", page)
    if not match:
        sys.exit(f"torch {TORCH} CPU wheel for {arch} not found on {TORCH_INDEX}")
    url = f"https://download.pytorch.org/whl/cpu/{name}"
    return {"url": url, "sha256": match.group(1)}


def resolve(arch: str) -> list[dict]:
    torch = torch_wheel(arch)
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
                "--quiet", "--report", str(report), "--only-binary=:all:",
                *platforms(arch), "--python-version", PYTHON,
                "--implementation", "cp", "--target", str(Path(tmp) / "t"),
                f"torch @ {torch['url']}", *REQUIREMENTS,
            ],
            check=True,
        )  # fmt: skip
        installs = json.loads(report.read_text())["install"]
    wheels = []
    for item in installs:
        info = item["download_info"]
        url = info["url"]
        sha = info.get("archive_info", {}).get("hashes", {}).get("sha256")
        if url == torch["url"]:
            sha = torch["sha256"]
        if not sha:
            sys.exit(f"no sha256 for {url}")
        wheels.append(
            {
                "name": item["metadata"]["name"],
                "version": item["metadata"]["version"],
                "filename": url.rsplit("/", 1)[-1].replace("%2B", "+"),
                "url": url,
                "sha256": sha,
                "size": size_of(url),
            }
        )
    return sorted(wheels, key=lambda w: w["name"].lower())


def model_files() -> list[dict]:
    tree = json.loads(
        http(f"https://huggingface.co/api/models/{MODEL_REPO}/tree/{MODEL_REVISION}").read()
    )
    by_path = {entry["path"]: entry for entry in tree}
    out = []
    for name in MODEL_FILES:
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
        entry = by_path[name]
        lfs = entry.get("lfs") or {}
        if lfs.get("oid"):
            sha, size = lfs["oid"], int(lfs["size"])
        else:
            body = http(url).read()
            sha, size = hashlib.sha256(body).hexdigest(), len(body)
        out.append({"name": name, "url": url, "sha256": sha, "size": size})
    return out


def main() -> None:
    lock = {
        "comment": "Generated by scripts/lock_aligner_runtime.py. Do not edit by hand.",
        "python": PYTHON,
        "model": {
            "repo": MODEL_REPO,
            "revision": MODEL_REVISION,
            "license": "CC-BY-NC-4.0",
            "files": model_files(),
        },
        "wheels": {arch: resolve(arch) for arch in ARCHES},
    }
    OUT.write_text(json.dumps(lock, indent=1) + "\n")
    for arch, wheels in lock["wheels"].items():
        total = sum(w["size"] for w in wheels)
        print(f"{arch}: {len(wheels)} wheels, {total / 1e6:.0f} MB")
    print(f"model: {sum(f['size'] for f in lock['model']['files']) / 1e6:.0f} MB")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
