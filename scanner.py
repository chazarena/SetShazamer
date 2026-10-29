"""SetShazamer scanning engine.

Slices a long mix through ffmpeg pipes (no temp files, no full-file decode),
fingerprints each slice with shazamio, and folds the matches into a
deduplicated, timestamped tracklist. Nothing in here touches the GUI: progress
and results leave through an ``emit`` callback, and ``ScanWorker`` runs the
whole thing on its own thread with its own asyncio event loop.

Run ``python scanner.py <file>`` for a headless scan.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterator

import aiohttp
from aiohttp_retry import ExponentialRetry
from shazamio import (
    BadResponseStatus,
    FailedDecodeJson,
    HTTPClient,
    RateLimited,
    Shazam,
    SignatureError,
)

log = logging.getLogger("setshazamer.scanner")

SUPPORTED_EXTENSIONS = (".mp3", ".mp4", ".wav", ".m4a", ".flac", ".mkv", ".webm", ".ogg", ".opus", ".aac")

SLICE_SECONDS = 12  # Shazam's own window; 15 s and up returns no matches.
MIN_STEP_SECONDS = 30
MAX_STEP_SECONDS = 180
DEFAULT_STEP_SECONDS = 90
MIN_TAIL_SECONDS = 4.0  # A final slice shorter than this can't be fingerprinted.
GAP_THRESHOLD = 2  # Consecutive empty slices before an "ID" row is logged.

JITTER_RANGE = (1.2, 1.8)
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 5.0
MAX_COOLDOWN_SECONDS = 20.0  # Extra spacing added after a 429, decays on success.
COOLDOWN_DECAY_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 25.0
FFMPEG_TIMEOUT_SECONDS = 90.0
FFPROBE_TIMEOUT_SECONDS = 60.0

UNIX_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")

RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    RateLimited,
    BadResponseStatus,
    FailedDecodeJson,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
)

# Keeps ffmpeg from flashing a console window from a --windowed Windows build.
_SUBPROCESS_KWARGS: dict[str, Any] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)


class MediaError(Exception):
    """ffmpeg/ffprobe could not read the source file."""


class ScanCancelled(Exception):
    """The stop signal was raised while a scan was running."""


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


def ensure_tool_path() -> None:
    """Prepend the usual ffmpeg install dirs to PATH.

    Apps launched from Finder/Spotlight get launchd's bare PATH, which has
    neither Homebrew prefix, so ``ffmpeg`` would raise FileNotFoundError.
    """
    dirs: list[str] = []
    if getattr(sys, "frozen", False):
        # Lets a build ship its own ffmpeg next to the executable.
        exe_dir = Path(sys.executable).resolve().parent
        dirs += [str(exe_dir), str(exe_dir / "bin")]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs += [meipass, os.path.join(meipass, "bin")]

    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        dirs += [
            r"C:\ffmpeg\bin",
            os.path.join(program_files, "ffmpeg", "bin"),
            r"C:\ProgramData\chocolatey\bin",
        ]
        if local_app_data:
            dirs.append(os.path.join(local_app_data, "Microsoft", "WinGet", "Links"))
    else:
        dirs += UNIX_TOOL_DIRS

    merged: list[str] = []
    for entry in dirs + os.environ.get("PATH", "").split(os.pathsep):
        if entry and entry not in merged:
            merged.append(entry)
    os.environ["PATH"] = os.pathsep.join(merged)


def missing_tools() -> list[str]:
    return [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MediaError(f"{name} was not found on PATH. Install FFmpeg (brew install ffmpeg).")
    return path


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_duration(seconds: float | None) -> str:
    """Compact human duration for the telemetry bar: 1h 04m, 3m 12s, 9s."""
    if seconds is None:
        return "--"
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


@dataclass(frozen=True)
class TrackEntry:
    seconds: float
    artist: str
    title: str
    label: str = "N/A"
    album: str = ""
    isrc: str = ""
    apple_music_url: str = ""
    spotify_uri: str = ""
    shazam_url: str = ""
    unidentified: bool = False

    @property
    def timestamp(self) -> str:
        return format_timestamp(self.seconds)

    @property
    def has_label(self) -> bool:
        return bool(self.label) and self.label != "N/A"

    @property
    def display_line(self) -> str:
        if self.unidentified:
            return f"[{self.timestamp}] | ID - Unidentified"
        line = f"[{self.timestamp}] | {self.artist} - {self.title}"
        return f"{line} ({self.label})" if self.has_label else line


@dataclass(frozen=True)
class ScanConfig:
    filepath: str
    step_seconds: int = DEFAULT_STEP_SECONDS
    slice_seconds: int = SLICE_SECONDS
    gap_threshold: int = GAP_THRESHOLD

    def __post_init__(self) -> None:
        if not MIN_STEP_SECONDS <= self.step_seconds <= MAX_STEP_SECONDS:
            raise ValueError(
                f"step_seconds must be between {MIN_STEP_SECONDS} and {MAX_STEP_SECONDS}"
            )


@dataclass(frozen=True)
class ProgressEvent:
    done: int
    total: int
    position: float
    duration: float
    elapsed: float
    remaining: float | None
    status: str

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0


@dataclass(frozen=True)
class TrackEvent:
    entry: TrackEntry


@dataclass(frozen=True)
class StatusEvent:
    message: str


@dataclass(frozen=True)
class FinishedEvent:
    reason: str  # "completed" | "cancelled" | "error"
    message: str
    entries: tuple[TrackEntry, ...]
    elapsed: float


ScanEvent = ProgressEvent | TrackEvent | StatusEvent | FinishedEvent


# --------------------------------------------------------------------------- #
# Shazam response parsing
# --------------------------------------------------------------------------- #


def _section_metadata(track: dict[str, Any], key: str) -> str:
    sections = track.get("sections") or []
    # The spec's source of truth is sections[0]; later sections are a fallback.
    for section in sections:
        for item in section.get("metadata") or []:
            if (item.get("title") or "").strip().lower() == key.lower():
                text = (item.get("text") or "").strip()
                if text:
                    return text
    return ""


def _walk_uris(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        uri = node.get("uri")
        if isinstance(uri, str) and uri:
            yield uri
        for value in node.values():
            yield from _walk_uris(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_uris(value)


def parse_track(response: dict[str, Any], seconds: float) -> TrackEntry | None:
    track = response.get("track")
    if not response.get("matches") or not isinstance(track, dict):
        return None
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title:
        return None

    hub = track.get("hub") or {}
    apple_music_url = ""
    spotify_uri = ""
    for uri in _walk_uris(hub):
        if not apple_music_url and uri.startswith("http") and "music.apple.com" in uri:
            apple_music_url = uri
        elif not spotify_uri and uri.startswith("spotify:"):
            spotify_uri = uri

    return TrackEntry(
        seconds=seconds,
        artist=artist or "Unknown Artist",
        title=title,
        label=_section_metadata(track, "Label") or "N/A",
        album=_section_metadata(track, "Album"),
        isrc=(track.get("isrc") or "").strip(),
        apple_music_url=apple_music_url,
        spotify_uri=spotify_uri,
        shazam_url=(track.get("url") or "").strip(),
    )


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #

_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_DASH_SUFFIX = re.compile(r"\s+[-\u2013\u2014]\s+.*$")
_FEAT_INLINE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&|\+|/|;|\bx\b|\bvs\.?|\band\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b)\s*",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^\w\s]+")
_SPACES = re.compile(r"\s+")


def _clean(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = _NON_WORD.sub(" ", stripped.lower().replace("&", " and "))
    return _SPACES.sub(" ", lowered).strip()


def normalize_title(title: str) -> str:
    """"Song (feat. X) [Extended Mix] - Remastered 2011" -> "song"."""
    base = _FEAT_INLINE.sub("", title)
    base = _BRACKETED.sub(" ", base)
    base = _DASH_SUFFIX.sub("", base)
    return _clean(base) or _clean(title)


def artist_names(artist: str) -> frozenset[str]:
    return frozenset(name for part in _ARTIST_SPLIT.split(artist) if (name := _clean(part)))


@dataclass(frozen=True)
class TrackKey:
    raw: str
    title: str
    artists: frozenset[str]

    @classmethod
    def of(cls, artist: str, title: str) -> "TrackKey":
        return cls(
            raw=f"{artist.lower().strip()} - {title.lower().strip()}",
            title=normalize_title(title),
            artists=artist_names(artist),
        )

    def same_track(self, other: "TrackKey") -> bool:
        if self.raw == other.raw:
            return True
        if self.title != other.title and SequenceMatcher(None, self.title, other.title).ratio() < 0.88:
            return False
        if not self.artists or not other.artists or self.artists & other.artists:
            return True
        mine = " ".join(sorted(self.artists))
        theirs = " ".join(sorted(other.artists))
        return SequenceMatcher(None, mine, theirs).ratio() >= 0.9


class TracklistBuilder:
    """Turns a stream of per-slice results into transition-only entries."""

    def __init__(self, gap_threshold: int = GAP_THRESHOLD) -> None:
        self.gap_threshold = max(1, gap_threshold)
        self.entries: list[TrackEntry] = []
        self._last_key: TrackKey | None = None
        self._miss_run = 0
        self._miss_run_start = 0.0
        self._gap_logged = False

    @property
    def identified_count(self) -> int:
        return sum(1 for entry in self.entries if not entry.unidentified)

    def add_match(self, entry: TrackEntry) -> TrackEntry | None:
        self._miss_run = 0
        self._gap_logged = False
        key = TrackKey.of(entry.artist, entry.title)
        if self._last_key is not None and self._last_key.same_track(key):
            return None
        self._last_key = key
        self.entries.append(entry)
        return entry

    def add_miss(self, seconds: float) -> TrackEntry | None:
        if self._miss_run == 0:
            self._miss_run_start = seconds
        self._miss_run += 1
        if self._miss_run < self.gap_threshold or self._gap_logged:
            return None
        self._gap_logged = True
        gap = TrackEntry(
            seconds=self._miss_run_start, artist="ID", title="Unidentified", unidentified=True
        )
        self.entries.append(gap)
        return gap


# --------------------------------------------------------------------------- #
# ffmpeg / ffprobe pipes
# --------------------------------------------------------------------------- #


async def _reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.shield(proc.wait())


async def _run_pipe(cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_SUBPROCESS_KWARGS,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        # Covers cancellation and timeouts: no ffmpeg outlives its slice.
        await _reap(proc)
    return proc.returncode or 0, stdout, stderr


async def probe_duration(filepath: str) -> float:
    cmd = [
        _tool("ffprobe"), "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,duration",
        "-of", "json", filepath,
    ]
    try:
        code, stdout, stderr = await _run_pipe(cmd, FFPROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise MediaError("ffprobe timed out reading the file.") from exc
    if code != 0:
        detail = stderr.decode(errors="replace").strip().splitlines()
        raise MediaError(f"ffprobe could not read the file: {detail[-1] if detail else code}")

    info = json.loads(stdout or b"{}")
    audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise MediaError("The file has no audio stream.")

    candidates = [info.get("format", {}).get("duration")]
    candidates += [s.get("duration") for s in audio_streams]
    for value in candidates:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    raise MediaError("Could not determine the media duration.")


def _patch_wav_sizes(buf: bytearray) -> bytearray:
    """ffmpeg can't seek back on a pipe, so the RIFF/data sizes it writes are
    placeholders. Rewrite them from the real buffer length."""
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return buf
    struct.pack_into("<I", buf, 4, len(buf) - 8)
    offset = 12
    while offset + 8 <= len(buf):
        chunk_id = bytes(buf[offset:offset + 4])
        if chunk_id == b"data":
            struct.pack_into("<I", buf, offset + 4, len(buf) - offset - 8)
            break
        (size,) = struct.unpack_from("<I", buf, offset + 4)
        offset += 8 + size + (size & 1)
    return buf


async def extract_slice(filepath: str, start: float, length: int = SLICE_SECONDS) -> bytes:
    """Decode ``length`` seconds at ``start`` to 16 kHz mono PCM WAV, in memory."""
    cmd = [
        _tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", str(length), "-i", filepath,
        "-vn", "-sn", "-dn",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-f", "wav", "pipe:1",
    ]
    try:
        code, stdout, stderr = await _run_pipe(cmd, FFMPEG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise MediaError(f"ffmpeg timed out at {format_timestamp(start)}") from exc
    # 44-byte header + under a second of 16 kHz 16-bit audio isn't worth sending.
    if code != 0 or len(stdout) < 44 + 32000:
        detail = stderr.decode(errors="replace").strip().splitlines()
        raise MediaError(
            f"ffmpeg returned no audio at {format_timestamp(start)}"
            + (f": {detail[-1]}" if detail else "")
        )
    return bytes(_patch_wav_sizes(bytearray(stdout)))


def slice_offsets(duration: float, step: int, slice_len: int = SLICE_SECONDS) -> list[float]:
    if duration <= slice_len:
        return [0.0]
    offsets: list[float] = []
    t = 0.0
    while t < duration:
        if duration - t >= min(MIN_TAIL_SECONDS, slice_len):
            offsets.append(t)
        t += step
    return offsets


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #


class TracklistScanner:
    def __init__(
        self,
        config: ScanConfig,
        emit: Callable[[ScanEvent], None],
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.emit = emit
        self.stop_event = stop_event or threading.Event()
        self.builder = TracklistBuilder(config.gap_threshold)
        self.started = time.monotonic()
        self.failed_slices = 0
        self.cooldown = 0.0

    @property
    def entries(self) -> tuple[TrackEntry, ...]:
        return tuple(self.builder.entries)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def _check_cancel(self) -> None:
        if self.stop_event.is_set():
            raise ScanCancelled()

    def _progress(self, done: int, total: int, position: float, duration: float, status: str) -> None:
        elapsed = self.elapsed
        remaining = (elapsed / done) * (total - done) if done else None
        self.emit(ProgressEvent(done, total, position, duration, elapsed, remaining, status))

    async def _extract_or_none(self, start: float) -> bytes | None:
        try:
            return await extract_slice(self.config.filepath, start, self.config.slice_seconds)
        except MediaError as exc:
            log.warning("%s", exc)
            self.emit(StatusEvent(str(exc)))
            return None

    async def _recognize(self, shazam: Shazam, audio: bytes, position: float) -> dict[str, Any] | None:
        """Shazam's answer, or None when the slice failed for good (not a miss)."""
        stamp = format_timestamp(position)
        for attempt in range(MAX_RETRIES + 1):
            self._check_cancel()
            try:
                response = await asyncio.wait_for(shazam.recognize(audio), REQUEST_TIMEOUT_SECONDS)
                self.cooldown = max(0.0, self.cooldown - COOLDOWN_DECAY_SECONDS)
                return response
            except SignatureError as exc:
                log.warning("Fingerprint failed at %s: %s", stamp, exc)
                self.emit(StatusEvent(f"Could not fingerprint {stamp}; skipping"))
                return None
            except RETRYABLE_ERRORS as exc:
                kind = "Network error"
                if isinstance(exc, RateLimited):
                    kind = "Rate limited"
                    self.cooldown = min(MAX_COOLDOWN_SECONDS, max(2.0, self.cooldown * 2))
                if attempt == MAX_RETRIES:
                    log.warning("Giving up on %s after %d retries: %r", stamp, MAX_RETRIES, exc)
                    self.emit(StatusEvent(f"{kind} at {stamp}; skipped after {MAX_RETRIES} retries"))
                    return None
                delay = BACKOFF_BASE_SECONDS * (2 ** attempt) + random.uniform(0.0, 1.0)
                log.info("%s at %s (%r); retry %d in %.1fs", kind, stamp, exc, attempt + 1, delay)
                self.emit(StatusEvent(
                    f"{kind} at {stamp}; retry {attempt + 1}/{MAX_RETRIES} in {delay:.0f}s"
                ))
                await asyncio.sleep(delay)
        return None

    async def run(self) -> tuple[TrackEntry, ...]:
        self.started = time.monotonic()
        self._check_cancel()
        name = Path(self.config.filepath).name
        self.emit(StatusEvent(f"Probing {name}..."))
        duration = await probe_duration(self.config.filepath)
        offsets = slice_offsets(duration, self.config.step_seconds, self.config.slice_seconds)
        total = len(offsets)
        self._progress(0, total, 0.0, duration, f"{format_duration(duration)} mix, {total} slices")

        # One attempt per call: retries and backoff are ours, not aiohttp_retry's
        # (its default is 20 attempts, which would stall a scan for minutes).
        http_client = HTTPClient(retry_options=ExponentialRetry(attempts=1))
        shazam = Shazam(http_client=http_client)
        try:
            audio = await self._extract_or_none(offsets[0])
            for index, position in enumerate(offsets):
                self._check_cancel()
                stamp = format_timestamp(position)
                self._progress(index, total, position, duration, f"Recognizing {stamp}")

                response = await self._recognize(shazam, audio, position) if audio else None
                if response is None:
                    self.failed_slices += 1
                else:
                    match = parse_track(response, position)
                    added = self.builder.add_match(match) if match else self.builder.add_miss(position)
                    if added is not None:
                        self.emit(TrackEvent(added))

                status = f"Scanned {stamp}"
                if index + 1 < total:
                    # Decode the next slice while we wait out the rate-limit delay.
                    audio, _ = await asyncio.gather(
                        self._extract_or_none(offsets[index + 1]),
                        asyncio.sleep(random.uniform(*JITTER_RANGE) + self.cooldown),
                    )
                self._progress(index + 1, total, position, duration, status)
        finally:
            # Shazam only closes clients it built itself; this one is ours.
            await http_client.close()
        return self.entries


class ScanWorker(threading.Thread):
    """Hosts a TracklistScanner on a private asyncio loop.

    Every event lands on ``events``; the GUI drains that queue on its own
    thread, so no widget is ever touched from here.
    """

    def __init__(self, config: ScanConfig, events: "queue.Queue[ScanEvent]") -> None:
        super().__init__(name="SetShazamerScan", daemon=True)
        self.events = events
        self.stop_event = threading.Event()
        self.scanner = TracklistScanner(config, events.put, self.stop_event)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[tuple[TrackEntry, ...]] | None = None
        self._lock = threading.Lock()

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            loop, task = self._loop, self._task
            if loop is not None and task is not None and not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reason, message = "completed", ""
        try:
            with self._lock:
                self._loop = loop
                self._task = loop.create_task(self.scanner.run())
            if self.stop_event.is_set():
                self._task.cancel()
            loop.run_until_complete(self._task)
            found = self.scanner.builder.identified_count
            message = f"Scan complete: {found} unique track{'s' if found != 1 else ''}"
            if self.scanner.failed_slices:
                message += f" ({self.scanner.failed_slices} slices failed)"
        except (asyncio.CancelledError, ScanCancelled):
            reason, message = "cancelled", "Scan stopped"
        except MediaError as exc:
            reason, message = "error", str(exc)
        except Exception as exc:  # Surface anything else instead of dying silently.
            log.exception("Scan crashed")
            reason, message = "error", f"Unexpected error: {exc}"
        finally:
            with self._lock:
                self._task = None
                _close_loop(loop)
            self.events.put(FinishedEvent(reason, message, self.scanner.entries, self.scanner.elapsed))


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    try:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


# --------------------------------------------------------------------------- #
# Headless CLI
# --------------------------------------------------------------------------- #


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Extract a timestamped tracklist from a mix.")
    parser.add_argument("file", help="audio/video file, or a YouTube / SoundCloud link")
    parser.add_argument("--step", type=int, default=DEFAULT_STEP_SECONDS,
                        help=f"seconds between slices ({MIN_STEP_SECONDS}-{MAX_STEP_SECONDS})")
    parser.add_argument("--show-id", action="store_true", help="print unidentified gaps")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ensure_tool_path()
    if missing := missing_tools():
        print(f"Missing {', '.join(missing)}. Install FFmpeg: brew install ffmpeg", file=sys.stderr)
        return 2

    source = args.file
    if source.startswith(("http://", "https://")):
        from downloader import DownloadCancelled, DownloadError, download_audio

        try:
            source = str(download_audio(
                source, on_progress=lambda p: print(f"  .. {p.message}", file=sys.stderr, flush=True)))
        except (DownloadError, DownloadCancelled) as exc:
            print(f"Download failed: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
        print(f"  .. saved to {source}", file=sys.stderr)

    events: queue.Queue[ScanEvent] = queue.Queue()
    worker = ScanWorker(ScanConfig(source, step_seconds=args.step), events)
    worker.start()
    try:
        while True:
            event = events.get()
            if isinstance(event, TrackEvent):
                if args.show_id or not event.entry.unidentified:
                    print(event.entry.display_line, flush=True)
            elif isinstance(event, StatusEvent):
                print(f"  .. {event.message}", file=sys.stderr, flush=True)
            elif isinstance(event, ProgressEvent):
                print(f"  {event.fraction:6.1%}  {event.status}  (eta {format_duration(event.remaining)})",
                      file=sys.stderr, flush=True)
            elif isinstance(event, FinishedEvent):
                print(event.message, file=sys.stderr)
                return 0 if event.reason != "error" else 1
    except KeyboardInterrupt:
        worker.stop()
        worker.join()
        return 130


if __name__ == "__main__":
    sys.exit(_cli())
