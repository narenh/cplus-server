"""The aligner sidecar's own logic, without the model.

What can be checked without torch: reading epubs, the shared-directory
protocol, the pinned runtime lock, the progress estimate, the supervisor's
crash handling, and (with numpy) anchoring and sampling. The model-driven
parts are exercised by a real run, not here.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cplus_align import install
from cplus_align.epub import inspect_epub, is_english, read_epub
from cplus_align.pipeline.progress import JobProgress, predicted_seconds, record_calibration
from cplus_align.protocol import (
    AlignPaths,
    has_work,
    needs_verify,
    read_json,
    ready_to_align,
    write_json,
)
from cplus_align.supervisor import MAX_CRASHES, Supervisor

from .audiobook_fixtures import CONTENT_ENCRYPTION, FONT_OBFUSCATION, make_epub

# --------------------------------------------------------------------------- #
# Epubs
# --------------------------------------------------------------------------- #


def test_text_is_split_on_block_tags_not_on_line_wraps() -> None:
    book = read_epub(io.BytesIO(make_epub(paragraphs=2)))
    [doc] = book.docs
    assert doc.blocks[0] == ("h1", "An Unexpected Party")
    # The source wraps mid-sentence after "ground"; the paragraph comes back whole.
    assert doc.blocks[1][1].startswith("In a hole in the ground there lived a hobbit.")
    assert len(doc.blocks) == 3
    assert book.title == "The Hobbit" and book.authors == ["J. R. R. Tolkien"]


def test_font_obfuscation_is_not_mistaken_for_drm() -> None:
    info = inspect_epub(io.BytesIO(make_epub(encryption=FONT_OBFUSCATION)))
    assert info.ok, info.problems


@pytest.mark.parametrize(
    "epub",
    [
        make_epub(encryption=CONTENT_ENCRYPTION),
        make_epub(files={"META-INF/rights.xml": "<rights/>"}),
        make_epub(files={"META-INF/sinf.xml": "<sinf/>"}),
    ],
)
def test_drm_is_recognised(epub: bytes) -> None:
    info = inspect_epub(io.BytesIO(epub))
    assert any("DRM" in problem for problem in info.problems)


@pytest.mark.parametrize(
    ("language", "english"),
    [("en", True), ("en-GB", True), ("eng", True), (None, True), ("fr", False), ("de-DE", False)],
)
def test_language_check(language: str | None, english: bool) -> None:
    assert is_english(language) is english


def test_a_declared_legacy_encoding_is_honoured() -> None:
    xhtml = (
        '<?xml version="1.0" encoding="windows-1252"?><html><body><p>caf\xe9 society</p>'
        "</body></html>"
    ).encode("latin-1")
    buf = io.BytesIO()
    import zipfile

    with (
        zipfile.ZipFile(io.BytesIO(make_epub(paragraphs=0))) as src,
        zipfile.ZipFile(buf, "w") as dst,
    ):
        for item in src.infolist():
            data = xhtml if item.filename == "OEBPS/ch1.xhtml" else src.read(item)
            dst.writestr(item, data)
    [doc] = read_epub(io.BytesIO(buf.getvalue())).docs
    assert doc.blocks == [("p", "café society")]


# --------------------------------------------------------------------------- #
# The shared directory
# --------------------------------------------------------------------------- #


def test_job_states_follow_the_files(tmp_path: Path) -> None:
    paths = AlignPaths(tmp_path)
    job = paths.job(1)
    job.mkdir(parents=True)
    assert not needs_verify(job)  # no job.json yet: still being written
    write_json(job / "job.json", {})
    assert needs_verify(job) and has_work(paths)
    write_json(job / "verify.json", {"ok": True})
    assert not needs_verify(job) and ready_to_align(job)
    (job / "cancel").write_text("x")
    assert not ready_to_align(job) and not has_work(paths)


def test_a_half_written_file_reads_as_nothing(tmp_path: Path) -> None:
    (tmp_path / "status.json").write_text('{"pct": 4')
    assert read_json(tmp_path / "status.json") is None
    assert read_json(tmp_path / "missing.json") is None


# --------------------------------------------------------------------------- #
# The pinned runtime
# --------------------------------------------------------------------------- #


def test_the_lock_pins_every_file_by_hash_for_both_architectures() -> None:
    lock = install.load_lock()
    for arch in ("x86_64", "aarch64"):
        wheels = lock["wheels"][arch]
        names = {w["name"].lower() for w in wheels}
        assert {"torch", "transformers", "numpy", "nltk", "uroman", "imageio-ffmpeg"} <= names
        torch = next(w for w in wheels if w["name"] == "torch")
        assert "+cpu" in torch["version"]
        assert torch["url"].startswith("https://download.pytorch.org/whl/cpu/")
        assert not any(name.startswith("nvidia") for name in names)
        for wheel in wheels:
            assert len(wheel["sha256"]) == 64 and wheel["size"] > 0
    files = {f["name"] for f in lock["model"]["files"]}
    assert "model.safetensors" in files and "pytorch_model.bin" not in files
    assert lock["model"]["revision"] in lock["model"]["files"][0]["url"]


def test_download_size_counts_only_what_is_missing(tmp_path: Path) -> None:
    paths = AlignPaths(tmp_path)
    everything = install.download_size()
    assert everything == install.download_size(paths=paths) > 1_000_000_000
    config = next(f for f in install.load_lock()["model"]["files"] if f["name"] == "config.json")
    dest = paths.model_dir / "config.json"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"x" * config["size"])
    write_json(paths.runtime / "verified.json", {str(dest): config["sha256"]})
    assert install.download_size(paths=paths) == everything - config["size"]


def test_the_runtime_id_ignores_a_rebuilt_but_identical_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(install.LOCAL_WHEELS_ENV, str(tmp_path))
    (tmp_path / "SOURCE").write_text("abc123\n")
    wheel = tmp_path / "ctc_forced_aligner-0.3.0-cp312-cp312-linux_x86_64.whl"
    wheel.write_bytes(b"one build")
    first = install.runtime_id()
    wheel.write_bytes(b"another build of the same commit")
    assert install.runtime_id() == first
    (tmp_path / "SOURCE").write_text("def456\n")
    assert install.runtime_id() != first


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_estimate_follows_the_live_rate_and_never_goes_backwards(tmp_path: Path) -> None:
    clock = Clock()
    predicted = predicted_seconds({}, duration=36_000, size=300_000_000, threads=3)
    progress = JobProgress(tmp_path / "status.json", predicted, clock=clock, wall=clock)
    progress.start("download")
    clock.now = 60
    progress.finish_stage()
    progress.start("decode")
    clock.now = 120
    progress.finish_stage()
    progress.start("emissions")
    readings = []
    # Twice as slow as the prior: the ETA has to grow to match.
    for step in range(1, 21):
        clock.now = 120 + step * 0.05 * predicted["emissions"] * 2
        progress.update(step * 0.05)
        progress.write(force=True)
        readings.append(read_json(tmp_path / "status.json"))
    pcts = [r["pct"] for r in readings]
    assert pcts == sorted(pcts)
    halfway = readings[9]
    assert halfway["eta"] > 0.9 * predicted["emissions"]  # ~half of a doubled stage left
    assert halfway["label"] == "Listening to the audio"


def test_a_resumed_job_keeps_its_place_on_the_bar(tmp_path: Path) -> None:
    clock = Clock()
    predicted = predicted_seconds({}, duration=6300, size=50_000_000, threads=3)
    progress = JobProgress(tmp_path / "status.json", predicted, clock=clock, wall=clock)
    progress.skip("download")
    progress.skip("decode")
    progress.start("emissions", frac=0.7)
    pct, eta = progress.estimate()
    assert 55 < pct < 75
    assert eta < 0.5 * sum(predicted.values())


def test_calibration_learns_this_machines_speed(tmp_path: Path) -> None:
    clock = Clock()
    predicted = predicted_seconds({}, duration=1000, size=10_000_000, threads=2)
    progress = JobProgress(tmp_path / "status.json", predicted, clock=clock, wall=clock)
    for stage, seconds in (("download", 5), ("decode", 4), ("emissions", 400), ("anchors", 2),
                           ("align", 20), ("save", 1)):  # fmt: skip
        progress.start(stage)
        clock.now += seconds
        progress.finish_stage()
    path = tmp_path / "calibration.json"
    record_calibration(path, progress, duration=1000, size=10_000_000, threads=2, resumed=set())
    calibration = read_json(path)
    assert calibration["emissions@2"] == pytest.approx(0.4)
    assert calibration["download_bps"] == pytest.approx(2_000_000)
    again = predicted_seconds(calibration, duration=1000, size=0, threads=2)
    assert again["emissions"] == pytest.approx(400)


def test_default_speed_scales_with_threads() -> None:
    two = predicted_seconds({}, duration=1000, size=0, threads=2)["emissions"]
    three = predicted_seconds({}, duration=1000, size=0, threads=3)["emissions"]
    four = predicted_seconds({}, duration=1000, size=0, threads=4)["emissions"]
    assert two > three > four
    assert three == pytest.approx(200)


# --------------------------------------------------------------------------- #
# The supervisor
# --------------------------------------------------------------------------- #


def _crash(supervisor: Supervisor, code: int) -> None:
    supervisor.engine = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
    supervisor.engine.wait()
    supervisor.check_engine()


def test_a_job_that_keeps_crashing_the_engine_is_failed_not_retried_forever(
    tmp_path: Path,
) -> None:
    paths = AlignPaths(tmp_path)
    supervisor = Supervisor(paths)
    job = paths.job(3)
    job.mkdir(parents=True)
    write_json(job / "job.json", {})
    write_json(paths.engine_file, {"job": 3, "t": time.time()})
    for _ in range(MAX_CRASHES - 1):
        _crash(supervisor, 1)
        assert not (job / "error.json").exists()
    _crash(supervisor, 1)
    error = read_json(job / "error.json")
    assert error["code"] == "crashed" and "crashed 3 times" in error["message"]


def test_a_clean_engine_exit_is_not_a_crash(tmp_path: Path) -> None:
    paths = AlignPaths(tmp_path)
    supervisor = Supervisor(paths)
    job = paths.job(4)
    job.mkdir(parents=True)
    write_json(paths.engine_file, {"job": 4, "t": time.time()})
    _crash(supervisor, 0)
    assert not (job / "crashes.json").exists()


def test_an_install_interrupted_by_a_restart_is_reported(tmp_path: Path) -> None:
    paths = AlignPaths(tmp_path)
    write_json(paths.state_file, {"status": "installing"})
    Supervisor(paths).recover()
    state = read_json(paths.state_file)
    assert state["status"] == "failed" and "restart" in state["error"]


def test_an_interrupted_install_with_its_request_still_pending_resumes(tmp_path: Path) -> None:
    paths = AlignPaths(tmp_path)
    write_json(paths.state_file, {"status": "installing"})
    write_json(paths.request_file, {"action": "install"})
    Supervisor(paths).recover()
    assert read_json(paths.state_file)["status"] == "installing"


def test_the_engine_is_given_only_this_package_to_import(tmp_path: Path) -> None:
    folder = Path(Supervisor(AlignPaths(tmp_path))._pythonpath)
    assert [p.name for p in folder.iterdir()] == ["cplus_align"]
    assert (folder / "cplus_align" / "pipeline" / "worker.py").exists()


# --------------------------------------------------------------------------- #
# Anchoring and sampling (numpy only)
# --------------------------------------------------------------------------- #


def test_the_longest_in_order_chain_of_seed_hits_wins() -> None:
    pytest.importorskip("numpy")
    from cplus_align.pipeline.anchors import NoSharedText, find_chain

    pairs = [(0, 100), (5, 900), (10, 110), (20, 120), (25, 50), (30, 130)]
    assert find_chain(pairs) == [(0, 100), (10, 110), (20, 120), (30, 130)]
    with pytest.raises(NoSharedText):
        find_chain([])


def test_unique_seeds_and_hits() -> None:
    pytest.importorskip("numpy")
    from cplus_align.pipeline.anchors import seed_hits
    from cplus_align.pipeline.book import unique_seeds

    book = "inaholeinthegroundtherelivedahobbit" + "notanastydirtywethole" * 2
    seeds = unique_seeds(book)
    assert seeds["inaholeinthe"] == 0
    assert seeds["notanastydir"] == -1  # appears twice: useless as an anchor
    heard = "xxxxinaholeinthegroundyyyy"
    hits = seed_hits(heard, seeds)
    assert hits[0] == (4, 0) and len(hits) == len("inaholeintheground") - 11


def test_greedy_decoding_collapses_repeats_and_blanks_in_blocks() -> None:
    np = pytest.importorskip("numpy")
    from cplus_align.pipeline.anchors import greedy_decode

    vocab = {"<blank>": 0, "a": 1, "b": 2, "'": 3}
    path = [0, 1, 1, 0, 1, 2, 2, 0, 3, 0]
    em = np.full((len(path), 4), -10.0, dtype=np.float32)
    em[np.arange(len(path)), path] = 0.0
    letters, frames = greedy_decode(em, vocab, 0, block=3)
    assert letters == "aab'"
    assert list(frames) == [1, 4, 5, 8]


def test_samples_spread_across_a_many_file_book() -> None:
    pytest.importorskip("numpy")
    from cplus_align.pipeline.verify import SAMPLE_SECONDS, Track, sample_points

    tracks = [Track("a", 600.0), Track("b", 600.0), Track("c", 600.0)]
    points = sample_points(tracks, 8)
    assert len(points) == 8
    assert points[0] == (0, pytest.approx(180.0))  # 10% of 1800 s
    assert points[-1][0] == 2
    assert all(offset <= 600 - SAMPLE_SECONDS for _, offset in points)
    assert [i for i, _ in points] == sorted(i for i, _ in points)


def test_calibration_file_is_json(tmp_path: Path) -> None:
    write_json(tmp_path / "c.json", {"a": 1})
    assert json.loads((tmp_path / "c.json").read_text()) == {"a": 1}
