"""Getting audio: short samples for verification, whole tracks for alignment.

Everything comes from cplus-service's ``/internal/aligner/jobs/{id}/tracks/{n}``,
which relays Plex with the admin's token so the token never reaches this
container. Samples are read by seeking over HTTP (ffmpeg issues range
requests), so verification needs a few MB, not the whole book.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np

SR = 16000

Poll = Callable[[], None]


def ffmpeg_exe() -> str:
    override = os.environ.get("CPLUS_ALIGN_FFMPEG")
    if override:
        return override
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - the runtime always ships it
        found = shutil.which("ffmpeg")
        if not found:
            raise RuntimeError("ffmpeg is not available in the aligner runtime") from None
        return found


def _header_arg(headers: dict[str, str]) -> list[str]:
    if not headers:
        return []
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]


def _resolve(url: str, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Swap the URL's hostname for its IP, keeping the original in a Host header.

    The static ffmpeg in the runtime can segfault (exit -11) inside its own
    hostname lookup, so name resolution happens here, in Python, instead.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host:
        return url, headers
    try:
        ip = socket.getaddrinfo(host, parts.port or 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        return url, headers
    if ip == host:
        return url, headers
    netloc = f"{ip}:{parts.port}" if parts.port else ip
    return (
        urllib.parse.urlunsplit(parts._replace(netloc=netloc)),
        {**headers, "Host": parts.netloc.rpartition("@")[2]},
    )


def read_sample(url: str, headers: dict[str, str], offset: float, seconds: float) -> np.ndarray:
    """``seconds`` of 16 kHz mono int16 audio starting near ``offset`` in one track."""
    url, headers = _resolve(url, headers)
    cmd = [
        ffmpeg_exe(), "-nostdin", "-loglevel", "error",
        *_header_arg(headers),
        "-rw_timeout", "30000000",
        "-ss", f"{max(offset, 0.0):.2f}", "-t", f"{seconds:.2f}",
        "-i", url, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR),
        "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"ffmpeg exited {proc.returncode}")
    return np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], dtype=np.int16)


class DownloadError(RuntimeError):
    pass


def download(
    url: str,
    headers: dict[str, str],
    dest: Path,
    *,
    expected_size: int | None,
    progress: Callable[[int], None],
    poll: Poll,
    attempts: int = 8,
) -> None:
    """Fetch one track to ``dest``, resuming a partial ``.part`` file across failures.

    A network blip hours into a book costs one retry from where it stopped, not
    the book.
    """
    part = dest.with_suffix(dest.suffix + ".part")
    failures = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        if expected_size and have >= expected_size:
            break
        request = urllib.request.Request(url, headers=dict(headers))
        if have:
            request.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if have and response.status != 206:
                    have = 0  # the server ignored the range: start over
                    part.unlink(missing_ok=True)
                with open(part, "ab") as handle:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
                        have += len(chunk)
                        progress(have)
                        poll()
            if expected_size is None or have >= expected_size:
                break
            raise DownloadError(f"connection closed at {have} of {expected_size} bytes")
        except (urllib.error.URLError, OSError, DownloadError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 404):
                raise DownloadError(f"cplus-service refused the audio ({exc.code})") from exc
            failures += 1
            if failures >= attempts:
                raise DownloadError(f"download kept failing: {exc}") from exc
            time.sleep(min(60, 2**failures))
            poll()
    os.replace(part, dest)


def decode(
    inputs: list[Path],
    raw_path: Path,
    *,
    progress: Callable[[int], None],
    poll: Poll,
) -> list[int]:
    """Decode every track, in order, into one 16 kHz mono int16 file.

    Returns each track's length in samples as actually decoded — the offsets a
    player needs come from these, not from container metadata, which leaves out
    encoder padding and would drift across a many-file book.
    """
    counts: list[int] = []
    total = 0
    with open(raw_path, "wb") as out:
        for path in inputs:
            cmd = [
                ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", str(path),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR),
                "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
            ]  # fmt: skip
            # stderr to a file, not a pipe: nothing reads it until ffmpeg exits,
            # and a full pipe would stall the decode.
            errors = tempfile.TemporaryFile()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)
            assert proc.stdout is not None
            written = 0
            try:
                while True:
                    chunk = proc.stdout.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    progress(total + written)
                    poll()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            code = proc.wait()
            errors.seek(0)
            err = errors.read()
            errors.close()
            if code != 0:
                detail = err.decode("utf-8", "replace").strip().splitlines()
                raise RuntimeError(
                    f"could not decode {path.name}: {detail[-1] if detail else 'ffmpeg failed'}"
                )
            if written % 2:  # never happens with s16le, but keep samples aligned
                out.write(b"\0")
                written += 1
            counts.append(written // 2)
            total += written
    return counts
