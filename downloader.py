"""Fetch a mix from YouTube, SoundCloud (or any site yt-dlp supports) as MP3.

Blocking; the GUI runs it on a worker thread. Progress goes out through a
callback and a ``threading.Event`` cancels it between chunks.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("setshazamer.downloader")

MP3_QUALITY = "192"  # kbps; plenty for fingerprinting and casual listening.
JS_RUNTIMES = ("deno", "node", "bun")  # YouTube needs one for full format access.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class DownloadError(Exception):
    pass


class DownloadCancelled(Exception):
    pass


@dataclass(frozen=True)
class DownloadProgress:
    stage: str  # "fetching" | "downloading" | "converting"
    fraction: float | None
    message: str


def is_url(text: str) -> bool:
    parsed = urllib.parse.urlparse(text.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def default_download_dir() -> Path:
    music = Path.home() / "Music"
    return (music if music.is_dir() else Path.home()) / "SetShazamer"


def _js_runtimes() -> dict[str, dict[str, str]]:
    found = {name: {"path": path} for name in JS_RUNTIMES if (path := shutil.which(name))}
    # yt-dlp's own default when nothing is installed.
    return found or {"deno": {}}


def _mb(n: float | None) -> str:
    return f"{n / 1_048_576:.1f} MB" if n else "?"


class _Logger:
    """Routes yt-dlp's chatter to logging instead of stdout (there is none in a .app)."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[download]"):
            log.debug(_ANSI.sub("", msg))

    def info(self, msg: str) -> None:
        log.info(_ANSI.sub("", msg))

    def warning(self, msg: str) -> None:
        log.warning(_ANSI.sub("", msg))

    def error(self, msg: str) -> None:
        log.error(_ANSI.sub("", msg))


def _clean_error(exc: BaseException) -> str:
    text = _ANSI.sub("", str(exc)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"^\[[\w:]+\]\s*[\w-]+:\s*", "", text)  # "[youtube] abc123: ..."
    return text or exc.__class__.__name__


def download_audio(
    url: str,
    dest_dir: Path | None = None,
    on_progress: Callable[[DownloadProgress], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Download ``url`` and return the path of the resulting MP3."""
    import yt_dlp  # Heavy import; only pay for it when a link is used.
    from yt_dlp.utils import DownloadCancelled as YtCancelled

    if not is_url(url):
        raise DownloadError("That doesn't look like a link. Paste a YouTube or SoundCloud URL.")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise DownloadError("FFmpeg is required to convert downloads to MP3 (brew install ffmpeg).")

    dest = dest_dir or default_download_dir()
    dest.mkdir(parents=True, exist_ok=True)
    cancel = cancel or threading.Event()
    report = on_progress or (lambda _p: None)

    def progress_hook(d: dict[str, Any]) -> None:
        if cancel.is_set():
            raise YtCancelled("Download cancelled")
        if d.get("status") == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            fraction = min(done / total, 1.0) if total else None
            if fraction is None and d.get("fragment_count"):
                # HLS streams (SoundCloud) report fragments, not a byte total.
                fraction = min((d.get("fragment_index") or 0) / d["fragment_count"], 1.0)
            speed = d.get("speed")
            parts = [f"Downloading… {fraction:.0%}" if fraction is not None else "Downloading…",
                     f"{_mb(done)} of {_mb(total)}" if total else _mb(done)]
            if speed:
                parts.append(f"{speed / 1_048_576:.1f} MB/s")
            report(DownloadProgress("downloading", fraction, "  ·  ".join(parts)))
        elif d.get("status") == "finished":
            report(DownloadProgress("converting", None, "Converting to MP3…"))

    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(dest / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "windowsfilenames": True,
        "overwrites": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Logger(),
        "progress_hooks": [progress_hook],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": MP3_QUALITY,
        }],
        "ffmpeg_location": str(Path(ffmpeg).parent),
        "js_runtimes": _js_runtimes(),
    }

    report(DownloadProgress("fetching", None, "Looking up link…"))
    info: dict[str, Any] = {}
    expected = Path()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
            if info.get("_type") in ("playlist", "multi_video"):
                raise DownloadError("That link is a playlist or set. Paste the link to a single mix.")
            if cancel.is_set():
                raise DownloadCancelled()

            expected = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
            if expected.is_file() and expected.stat().st_size > 0:
                report(DownloadProgress("converting", 1.0, f"Already downloaded: {expected.name}"))
                return expected

            title = info.get("title") or "mix"
            report(DownloadProgress("downloading", 0.0, f"Downloading “{title}”…"))
            info = ydl.process_ie_result(info, download=True) or info
    except (YtCancelled, DownloadCancelled) as exc:
        _remove_partials(dest, info.get("id"))
        raise DownloadCancelled() from exc
    except DownloadError:
        raise
    except yt_dlp.utils.YoutubeDLError as exc:
        _remove_partials(dest, info.get("id"))
        if cancel.is_set():
            raise DownloadCancelled() from exc
        raise DownloadError(_clean_error(exc)) from exc

    for candidate in (_final_path(info), str(expected)):
        if candidate and candidate.endswith(".mp3") and Path(candidate).is_file():
            log.info("Downloaded %s -> %s", url, candidate)
            return Path(candidate)
    raise DownloadError("The download finished but the MP3 wasn't found.")


def _final_path(info: dict[str, Any]) -> str:
    for item in info.get("requested_downloads") or []:
        if item.get("filepath"):
            return item["filepath"]
    return info.get("filepath", "")


def _remove_partials(dest: Path, video_id: str | None) -> None:
    if not video_id:
        return
    for leftover in dest.glob(f"*[[]{video_id}[]]*"):
        name = leftover.name
        if ".part" in name or name.endswith((".ytdl", ".temp")):
            try:
                leftover.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    if len(sys.argv) != 2:
        sys.exit("usage: python downloader.py <url>")
    import scanner

    scanner.ensure_tool_path()
    try:
        out = download_audio(sys.argv[1], on_progress=lambda p: print(p.message, file=sys.stderr))
    except (DownloadError, DownloadCancelled) as error:
        sys.exit(f"error: {error}")
    print(out)
