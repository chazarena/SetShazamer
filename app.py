"""SetShazamer desktop GUI.

All widget access happens on the Tk main thread. The scan runs in
``scanner.ScanWorker`` (its own thread + asyncio loop) and Spotify calls run on
a one-thread executor; both report back through queues drained by ``_poll``.
"""

from __future__ import annotations

import csv
import logging
import logging.handlers
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

import scanner

# Must run before anything looks for ffmpeg: Finder-launched apps get a bare PATH.
scanner.ensure_tool_path()

import customtkinter as ctk  # noqa: E402

from scanner import (  # noqa: E402
    DEFAULT_STEP_SECONDS,
    MAX_STEP_SECONDS,
    MIN_STEP_SECONDS,
    SUPPORTED_EXTENSIONS,
    FinishedEvent,
    ProgressEvent,
    ScanConfig,
    ScanEvent,
    ScanWorker,
    StatusEvent,
    TrackEntry,
    TrackEvent,
    TrackRemovedEvent,
    TrackUpdatedEvent,
    format_duration,
)
from downloader import (  # noqa: E402
    DownloadCancelled,
    DownloadProgress,
    download_audio,
    is_url,
)
from spotify import (  # noqa: E402
    DASHBOARD_URL,
    SETUP_STEPS,
    Playlist,
    SpotifyAuthError,
    SpotifyClient,
    SpotifyError,
    SpotifyTrack,
)

APP_NAME = "SetShazamer"
NEW_PLAYLIST = "+ New playlist…"
NO_PLAYLISTS = "(connect Spotify)"
CHECKED, UNCHECKED, NO_CHECK = "☑", "☐", ""
CONFIDENCE_LABELS = {"High": "●●● High", "Medium": "●●○ Med", "Low": "●○○ Low"}

if sys.platform == "darwin":
    MONO_FAMILY = "Menlo"
elif sys.platform == "win32":
    MONO_FAMILY = "Consolas"
else:
    MONO_FAMILY = "DejaVu Sans Mono"

ACCENT_RED = ("#c0392b", "#a93226")
ACCENT_RED_HOVER = ("#a93226", "#922b21")
SPOTIFY_GREEN = ("#1db954", "#1aa34a")
SPOTIFY_GREEN_HOVER = ("#1aa34a", "#168d40")
MUTED_TEXT = ("gray40", "gray60")
WARN_TEXT = ("#b9770e", "#f5b041")

log = logging.getLogger("setshazamer")


def _setup_logging() -> Path:
    if sys.platform == "darwin":
        log_dir = Path.home() / "Library" / "Logs" / APP_NAME
    else:
        from spotify import config_dir

        log_dir = config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "setshazamer.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return path


class PromptDialog(ctk.CTkToplevel):
    """Modal text prompt with an optional explanation, initial value and help link."""

    def __init__(
        self,
        master: ctk.CTk,
        title: str,
        message: str,
        initial: str = "",
        ok_text: str = "OK",
        link: tuple[str, str] | None = None,
    ) -> None:
        super().__init__(master)
        self.result: str | None = None
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text=message, justify="left", anchor="w", wraplength=460).grid(
            row=0, column=0, columnspan=3, padx=20, pady=(20, 10), sticky="ew")
        self.entry = ctk.CTkEntry(self, width=460)
        self.entry.grid(row=1, column=0, columnspan=3, padx=20, pady=(0, 16), sticky="ew")
        if initial:
            self.entry.insert(0, initial)

        if link:
            ctk.CTkButton(self, text=link[0], width=120, fg_color="transparent", border_width=1,
                          text_color=("gray10", "gray90"),
                          command=lambda: webbrowser.open(link[1])).grid(
                row=2, column=0, padx=(20, 0), pady=(0, 20), sticky="w")
        ctk.CTkButton(self, text="Cancel", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self._cancel).grid(
            row=2, column=1, padx=8, pady=(0, 20), sticky="e")
        ctk.CTkButton(self, text=ok_text, width=90, command=self._ok).grid(
            row=2, column=2, padx=(0, 20), pady=(0, 20), sticky="e")

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(120, self._activate)

    def _activate(self) -> None:
        self.lift()
        self.focus_force()
        self.entry.focus_set()
        try:
            self.grab_set()
        except tk.TclError:
            pass  # Not viewable yet on some window managers; still usable.

    def _ok(self) -> None:
        value = self.entry.get().strip()
        if value:
            self.result = value
            self.destroy()

    def _cancel(self) -> None:
        self.destroy()

    @classmethod
    def ask(cls, master: ctk.CTk, **kwargs: Any) -> str | None:
        dialog = cls(master, **kwargs)
        master.wait_window(dialog)
        return dialog.result


class SetShazamerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x780")
        self.minsize(1000, 640)

        self.filepath: str | None = None
        self.worker: ScanWorker | None = None
        # Rows are keyed by TrackEntry.uid, which stays put while confidence changes.
        self.entries: list[TrackEntry] = []
        self.checked: set[int] = set()
        self._touched: set[int] = set()  # Rows the user ticked/unticked by hand.
        self.spotify_status: dict[int, str] = {}
        self.spotify_matches: dict[int, SpotifyTrack | None] = {}

        self.scan_events: queue.Queue[ScanEvent] = queue.Queue()
        self.ui_calls: queue.Queue[Callable[[], None]] = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spotify")
        self.spotify = SpotifyClient()
        self.playlists: dict[str, Playlist] = {}
        self._auth_cancel: threading.Event | None = None
        self._spotify_busy = False

        self._scan_started = 0.0
        self._last_remaining: float | None = None
        self._last_progress_at = 0.0
        self._ffmpeg_ok = True
        self._download_cancel: threading.Event | None = None

        self.step_var = tk.IntVar(value=DEFAULT_STEP_SECONDS)
        self.show_id_var = tk.BooleanVar(value=False)
        self.verify_var = tk.BooleanVar(value=True)
        self.skip_dupes_var = tk.BooleanVar(value=True)
        self.playlist_var = tk.StringVar(value=NO_PLAYLISTS)

        self._build_ui()
        self._refresh_spotify_header()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if sys.platform == "darwin":
            # Files dropped on the Dock icon / "Open With".
            self.createcommand("::tk::mac::OpenDocument", self._on_mac_open)

        self.after(50, self._poll)
        self.after(250, self._preflight)
        if self.spotify.is_connected:
            self.after(400, self._load_playlists)

    # ------------------------------------------------------------------ UI ---

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        mono = ctk.CTkFont(family=MONO_FAMILY, size=13)

        # 1. Header
        header = ctk.CTkFrame(self)
        header.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        self.open_btn = ctk.CTkButton(header, text="Open Mix…", width=120, command=self._choose_file)
        self.open_btn.grid(row=0, column=0, padx=12, pady=12)
        self.file_label = ctk.CTkLabel(
            header, anchor="w", text_color=MUTED_TEXT,
            text="No file selected  ·  " + " ".join(SUPPORTED_EXTENSIONS))
        self.file_label.grid(row=0, column=1, columnspan=2, padx=(0, 12), sticky="ew")
        self.url_entry = ctk.CTkEntry(header, placeholder_text="…or paste a YouTube / SoundCloud link")
        self.url_entry.grid(row=1, column=0, columnspan=2, padx=12, pady=(0, 12), sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._download_from_link())
        self.download_btn = ctk.CTkButton(header, text="Download & Scan", width=150,
                                          command=self._download_from_link)
        self.download_btn.grid(row=1, column=2, padx=(0, 12), pady=(0, 12))

        # 2. Configuration
        config = ctk.CTkFrame(self)
        config.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        config.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(config, text="Scan Interval").grid(row=0, column=0, padx=(12, 8), pady=12)
        self.step_slider = ctk.CTkSlider(
            config, from_=MIN_STEP_SECONDS, to=MAX_STEP_SECONDS,
            number_of_steps=(MAX_STEP_SECONDS - MIN_STEP_SECONDS) // 10,
            variable=self.step_var, command=self._on_step_change, width=200)
        self.step_slider.grid(row=0, column=1, pady=12)
        self.step_label = ctk.CTkLabel(config, text=f"{DEFAULT_STEP_SECONDS}s", width=48, anchor="w")
        self.step_label.grid(row=0, column=2, padx=8, sticky="w")
        self.verify_box = ctk.CTkCheckBox(config, text="Double-check new tracks",
                                          variable=self.verify_var)
        self.verify_box.grid(row=0, column=3, padx=(12, 6))
        ctk.CTkCheckBox(config, text="Show Unidentified [ID - ID]", variable=self.show_id_var,
                        command=self._rebuild_tree).grid(row=0, column=4, padx=(6, 12))
        self.scan_btn = ctk.CTkButton(config, text="Start Scan", width=140,
                                      font=ctk.CTkFont(weight="bold"), command=self._toggle_scan)
        self.scan_btn.grid(row=0, column=5, padx=12, pady=12)
        self._scan_btn_colors = (self.scan_btn.cget("fg_color"), self.scan_btn.cget("hover_color"))

        # 3. Telemetry
        telemetry = ctk.CTkFrame(self)
        telemetry.grid(row=2, column=0, padx=16, pady=8, sticky="ew")
        telemetry.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(telemetry, mode="determinate")
        self.progress.set(0)
        self.progress.grid(row=0, column=0, padx=(12, 8), pady=(12, 4), sticky="ew")
        self.pct_label = ctk.CTkLabel(telemetry, text="0%", width=48, anchor="e")
        self.pct_label.grid(row=0, column=1, padx=(0, 12), pady=(12, 4))
        self.status_label = ctk.CTkLabel(telemetry, text="Idle", anchor="w")
        self.status_label.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="ew")
        self.time_label = ctk.CTkLabel(telemetry, text="", anchor="e", text_color=MUTED_TEXT)
        self.time_label.grid(row=1, column=1, padx=12, pady=(0, 10), sticky="e")

        # 4. Results
        results = ctk.CTkFrame(self)
        results.grid(row=3, column=0, padx=16, pady=8, sticky="nsew")
        results.grid_columnconfigure(0, weight=1)
        results.grid_rowconfigure(0, weight=1)
        self._style_tree(mono)
        columns = ("check", "time", "artist", "title", "label", "confidence", "spotify")
        self.tree = ttk.Treeview(results, columns=columns, show="headings",
                                 selectmode="browse", style="Shazamer.Treeview")
        for col, text, width, stretch, anchor in (
            ("check", "", 34, False, "center"),
            ("time", "Time", 92, False, "w"),
            ("artist", "Artist", 230, True, "w"),
            ("title", "Title", 290, True, "w"),
            ("label", "Label", 170, True, "w"),
            ("confidence", "Confidence", 104, False, "w"),
            ("spotify", "Spotify", 130, False, "w"),
        ):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=width, minwidth=30, stretch=stretch, anchor=anchor)
        self.tree.heading("check", text=CHECKED, command=self._toggle_all)
        self.tree.tag_configure("unidentified", foreground="#7f8c8d")
        self.tree.tag_configure("low", foreground="#e59866")
        self.tree.tag_configure("odd", background="#202224")
        self.tree.grid(row=0, column=0, padx=(8, 0), pady=8, sticky="nsew")
        scroll = ctk.CTkScrollbar(results, command=self.tree.yview)
        scroll.grid(row=0, column=1, padx=(0, 4), pady=8, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_space)
        self.tree.bind("<Double-1>", self._on_tree_double)

        # 5. Spotify
        spot = ctk.CTkFrame(self)
        spot.grid(row=4, column=0, padx=16, pady=8, sticky="ew")
        spot.grid_columnconfigure(2, weight=1)
        self.spotify_btn = ctk.CTkButton(spot, text="Connect Spotify", width=150,
                                         fg_color=SPOTIFY_GREEN, hover_color=SPOTIFY_GREEN_HOVER,
                                         command=self._spotify_button)
        self.spotify_btn.grid(row=0, column=0, padx=12, pady=12)
        self.spotify_label = ctk.CTkLabel(spot, text="", anchor="w", text_color=MUTED_TEXT, width=150)
        self.spotify_label.grid(row=0, column=1, padx=(0, 8), sticky="w")
        self.playlist_menu = ctk.CTkOptionMenu(spot, variable=self.playlist_var, values=[NO_PLAYLISTS],
                                               command=self._on_playlist_pick, width=240,
                                               dynamic_resizing=False)
        self.playlist_menu.grid(row=0, column=2, padx=4, sticky="ew")
        self.refresh_btn = ctk.CTkButton(spot, text="↻", width=32, command=self._load_playlists)
        self.refresh_btn.grid(row=0, column=3, padx=4)
        ctk.CTkCheckBox(spot, text="Skip songs already in playlist",
                        variable=self.skip_dupes_var).grid(row=0, column=4, padx=10)
        self.add_btn = ctk.CTkButton(spot, text="Add Selected", width=150,
                                     fg_color=SPOTIFY_GREEN, hover_color=SPOTIFY_GREEN_HOVER,
                                     command=self._add_to_spotify)
        self.add_btn.grid(row=0, column=5, padx=12)

        # 6. Export
        export = ctk.CTkFrame(self)
        export.grid(row=5, column=0, padx=16, pady=(8, 16), sticky="ew")
        export.grid_columnconfigure(0, weight=1)
        self.count_label = ctk.CTkLabel(export, text="0 unique tracks found", anchor="w",
                                        font=ctk.CTkFont(weight="bold"))
        self.count_label.grid(row=0, column=0, padx=12, pady=12, sticky="ew")
        ctk.CTkButton(export, text="Copy to Clipboard", width=150,
                      command=self._copy_clipboard).grid(row=0, column=1, padx=6)
        ctk.CTkButton(export, text="Export .TXT", width=120,
                      command=self._export_txt).grid(row=0, column=2, padx=6)
        ctk.CTkButton(export, text="Export .CSV", width=120,
                      command=self._export_csv).grid(row=0, column=3, padx=(6, 12))

    def _style_tree(self, mono: ctk.CTkFont) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        bg, fg, head = "#1a1b1c", "#dce4ee", "#2b2d2f"
        style.configure("Shazamer.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                        rowheight=26, borderwidth=0, font=(MONO_FAMILY, 12))
        style.map("Shazamer.Treeview", background=[("selected", "#1f538d")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Shazamer.Treeview.Heading", background=head, foreground=fg,
                        relief="flat", font=(MONO_FAMILY, 12, "bold"))
        style.map("Shazamer.Treeview.Heading", background=[("active", "#34373a")])
        style.layout("Shazamer.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # ------------------------------------------------------------ helpers ---

    def _set_status(self, text: str, warn: bool = False) -> None:
        self.status_label.configure(text=text, text_color=WARN_TEXT if warn else ("gray10", "gray90"))

    def _call_ui(self, fn: Callable[[], None]) -> None:
        """Thread-safe: queue ``fn`` to run on the Tk thread."""
        self.ui_calls.put(fn)

    def _run_bg(
        self,
        job: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> Future:
        future = self.executor.submit(job)

        def finished(f: Future) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            self._call_ui((lambda: on_error(exc)) if exc else (lambda: on_done(f.result())))

        future.add_done_callback(finished)
        return future

    def _visible_entries(self) -> list[TrackEntry]:
        show_id = self.show_id_var.get()
        return [e for e in self.entries if show_id or not e.unidentified]

    def _identified_count(self) -> int:
        return sum(1 for e in self.entries if not e.unidentified)

    def _update_counts(self) -> None:
        n = self._identified_count()
        self.count_label.configure(text=f"{n} unique track{'s' if n != 1 else ''} found")
        selected = sum(1 for e in self.entries if e.uid in self.checked and not e.unidentified)
        self.add_btn.configure(text=f"Add Selected ({selected})" if selected else "Add Selected")

    # ---------------------------------------------------------- file/scan ---

    def _preflight(self) -> None:
        missing = scanner.missing_tools()
        if not missing:
            return
        self._ffmpeg_ok = False
        self.scan_btn.configure(state="disabled")
        self._set_status(f"Missing {' and '.join(missing)} — scanning disabled", warn=True)
        if sys.platform == "win32":
            how = "Open PowerShell and run:\n\n    winget install Gyan.FFmpeg\n\nthen restart SetShazamer."
        else:
            how = ("Install Homebrew from https://brew.sh if you don't have it, then run "
                   "this in Terminal:\n\n    brew install ffmpeg\n\nthen restart SetShazamer.")
        messagebox.showerror(
            "FFmpeg not found",
            f"SetShazamer needs FFmpeg ({', '.join(missing)}) to read audio.\n\n{how}",
            parent=self,
        )

    def _on_mac_open(self, *paths: str) -> None:
        if paths:
            self._set_file(paths[0])

    def _choose_file(self) -> None:
        pattern = " ".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS)
        path = filedialog.askopenfilename(
            parent=self, title="Choose a mix",
            filetypes=[("Audio / Video", pattern), ("All files", "*")])
        if path:
            self._set_file(path)

    def _set_file(self, path: str) -> None:
        if self.worker is not None:
            messagebox.showinfo(APP_NAME, "Stop the current scan before opening another file.",
                                parent=self)
            return
        self.filepath = path
        p = Path(path)
        suffix = "" if p.suffix.lower() in SUPPORTED_EXTENSIONS else "  (unrecognised type; will try)"
        self.file_label.configure(text=f"{p.name}{suffix}", text_color=("gray10", "gray90"))
        self._set_status("Ready to scan")

    def _on_step_change(self, value: float) -> None:
        self.step_label.configure(text=f"{int(round(value))}s")

    # ----------------------------------------------------------- download ---

    def _download_from_link(self) -> None:
        if self.worker is not None or self._download_cancel is not None:
            return
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showinfo(APP_NAME, "Paste a YouTube or SoundCloud link first.", parent=self)
            return
        if not is_url(url):
            messagebox.showerror(APP_NAME, "That doesn't look like a link. Paste the full URL, "
                                 "starting with https://", parent=self)
            return
        if not self._ffmpeg_ok:
            self._preflight()
            return

        cancel = threading.Event()
        self._download_cancel = cancel
        self._set_downloading(True)
        self.progress.set(0)
        self.pct_label.configure(text="")
        self.time_label.configure(text="")
        log.info("Download started: %s", url)

        def on_progress(progress: DownloadProgress) -> None:
            self._call_ui(lambda: self._show_download_progress(progress))

        def job() -> None:
            try:
                path = download_audio(url, on_progress=on_progress, cancel=cancel)
            except BaseException as exc:  # Reported on the Tk thread, never swallowed.
                self._call_ui(lambda e=exc: self._download_finished(None, e))
                return
            self._call_ui(lambda: self._download_finished(path, None))

        threading.Thread(target=job, name="download", daemon=True).start()

    def _set_downloading(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.open_btn, self.url_entry, self.download_btn, self.step_slider, self.verify_box):
            widget.configure(state=state)
        if busy:
            self.scan_btn.configure(state="normal", text="Cancel Download",
                                    fg_color=ACCENT_RED, hover_color=ACCENT_RED_HOVER)
        else:
            self.scan_btn.configure(state="normal", text="Start Scan",
                                    fg_color=self._scan_btn_colors[0],
                                    hover_color=self._scan_btn_colors[1])

    def _show_download_progress(self, progress: DownloadProgress) -> None:
        if self._download_cancel is None:
            return
        if progress.fraction is not None:
            self.progress.set(progress.fraction)
            self.pct_label.configure(text=f"{progress.fraction:.0%}")
        self._set_status(progress.message)

    def _download_finished(self, path: Path | None, error: BaseException | None) -> None:
        cancelled = self._download_cancel is not None and self._download_cancel.is_set()
        self._download_cancel = None
        self._set_downloading(False)
        if error is not None or cancelled or path is None:
            self.progress.set(0)
            self.pct_label.configure(text="0%")
            if cancelled or isinstance(error, DownloadCancelled):
                self._set_status("Download cancelled")
                return
            log.warning("Download failed: %s", error)
            self._set_status(f"Download failed: {error}", warn=True)
            messagebox.showerror("Download failed", str(error), parent=self)
            return
        self.url_entry.delete(0, "end")
        self._set_file(str(path))
        log.info("Download saved: %s", path)
        self._start_scan()
        self._set_status(f"Saved to {path.parent}  ·  starting scan…")

    def _toggle_scan(self) -> None:
        if self._download_cancel is not None:
            self._download_cancel.set()
            self.scan_btn.configure(state="disabled", text="Cancelling…")
            return
        if self.worker is None:
            self._start_scan()
        else:
            self.worker.stop()
            self.scan_btn.configure(state="disabled", text="Stopping…")
            self._set_status("Stopping…")

    def _start_scan(self) -> None:
        if not self._ffmpeg_ok:
            self._preflight()
            return
        if not self.filepath and self.url_entry.get().strip():
            self._download_from_link()
            return
        if not self.filepath:
            self._choose_file()
            if not self.filepath:
                return
        if not Path(self.filepath).is_file():
            messagebox.showerror(APP_NAME, f"File not found:\n{self.filepath}", parent=self)
            return

        self.entries.clear()
        self.checked.clear()
        self._touched.clear()
        self.spotify_status.clear()
        self.spotify_matches.clear()
        self._rebuild_tree()
        self.progress.set(0)
        self.pct_label.configure(text="0%")
        self._scan_started = time.monotonic()
        self._last_remaining = None

        config = ScanConfig(self.filepath, step_seconds=int(round(self.step_var.get())),
                            verify=self.verify_var.get())
        self.worker = ScanWorker(config, self.scan_events)
        self.worker.start()
        log.info("Scan started: %s (step %ss)", self.filepath, config.step_seconds)
        self.scan_btn.configure(text="Stop Scan", fg_color=ACCENT_RED, hover_color=ACCENT_RED_HOVER)
        for widget in (self.open_btn, self.url_entry, self.download_btn, self.step_slider, self.verify_box):
            widget.configure(state="disabled")

    def _handle_scan_event(self, event: ScanEvent) -> None:
        if isinstance(event, ProgressEvent):
            self.progress.set(event.fraction)
            self.pct_label.configure(text=f"{event.fraction:.0%}")
            self._last_remaining = event.remaining
            self._last_progress_at = time.monotonic()
            self._set_status(event.status)
        elif isinstance(event, StatusEvent):
            self._set_status(event.message, warn=True)
        elif isinstance(event, TrackEvent):
            self._append_entry(event.entry)
        elif isinstance(event, TrackUpdatedEvent):
            self._update_entry(event.entry)
        elif isinstance(event, TrackRemovedEvent):
            self._remove_entry(event.uid)
        elif isinstance(event, FinishedEvent):
            self.worker = None
            self.scan_btn.configure(state="normal", text="Start Scan",
                                    fg_color=self._scan_btn_colors[0],
                                    hover_color=self._scan_btn_colors[1])
            for widget in (self.open_btn, self.url_entry, self.download_btn, self.step_slider, self.verify_box):
                widget.configure(state="normal")
            if event.reason == "completed":
                self.progress.set(1)
                self.pct_label.configure(text="100%")
            self._set_status(event.message, warn=event.reason == "error")
            self.time_label.configure(text=f"Took {format_duration(event.elapsed)}")
            log.info("Scan finished (%s): %s", event.reason, event.message)
            if event.reason == "error":
                messagebox.showerror("Scan failed", event.message, parent=self)
            else:
                self.bell()

    def _tick_clock(self) -> None:
        if self.worker is None:
            return
        elapsed = time.monotonic() - self._scan_started
        text = f"Elapsed {format_duration(elapsed)}"
        if self._last_remaining is not None:
            left = max(0.0, self._last_remaining - (time.monotonic() - self._last_progress_at))
            text += f"  ·  ~{format_duration(left)} left"
        self.time_label.configure(text=text)

    # -------------------------------------------------------------- table ---

    def _entry(self, uid: int) -> TrackEntry | None:
        return next((e for e in self.entries if e.uid == uid), None)

    def _row_values(self, entry: TrackEntry) -> tuple[str, ...]:
        if entry.unidentified:
            return (NO_CHECK, entry.timestamp, "ID", "Unidentified", "", "", "")
        check = CHECKED if entry.uid in self.checked else UNCHECKED
        label = entry.label if entry.has_label else ""
        confidence = CONFIDENCE_LABELS.get(entry.confidence, entry.confidence)
        return (check, entry.timestamp, entry.artist, entry.title, label, confidence,
                self.spotify_status.get(entry.uid, ""))

    def _row_tags(self, entry: TrackEntry, odd: bool) -> list[str]:
        tags = ["odd"] if odd else []
        if entry.unidentified:
            tags.append("unidentified")
        elif entry.confidence == "Low":
            tags.append("low")
        return tags

    def _insert_row(self, entry: TrackEntry) -> None:
        odd = len(self.tree.get_children()) % 2 == 1
        self.tree.insert("", "end", iid=str(entry.uid), values=self._row_values(entry),
                         tags=self._row_tags(entry, odd))

    def _refresh_row(self, entry: TrackEntry) -> None:
        iid = str(entry.uid)
        if self.tree.exists(iid):
            odd = "odd" in self.tree.item(iid, "tags")
            self.tree.item(iid, values=self._row_values(entry), tags=self._row_tags(entry, odd))

    def _auto_check(self, entry: TrackEntry) -> None:
        """Tick likely-correct tracks; leave the ones the user set by hand alone."""
        if entry.unidentified or entry.uid in self._touched:
            return
        if entry.confidence == "Low":
            self.checked.discard(entry.uid)
        else:
            self.checked.add(entry.uid)

    def _append_entry(self, entry: TrackEntry) -> None:
        self.entries.append(entry)
        self._auto_check(entry)
        if self.show_id_var.get() or not entry.unidentified:
            at_bottom = self.tree.yview()[1] >= 0.999
            self._insert_row(entry)
            if at_bottom:
                self.tree.see(str(entry.uid))
        self._update_counts()

    def _update_entry(self, entry: TrackEntry) -> None:
        for i, existing in enumerate(self.entries):
            if existing.uid == entry.uid:
                self.entries[i] = entry
                break
        else:
            return
        self._auto_check(entry)
        self._refresh_row(entry)
        self._update_counts()

    def _remove_entry(self, uid: int) -> None:
        self.entries = [e for e in self.entries if e.uid != uid]
        for store in (self.checked, self._touched):
            store.discard(uid)
        self.spotify_status.pop(uid, None)
        self._rebuild_tree()  # Re-stripes the rows.

    def _rebuild_tree(self) -> None:
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        show_id = self.show_id_var.get()
        for entry in self.entries:
            if show_id or not entry.unidentified:
                self._insert_row(entry)
        self.tree.yview_moveto(top)
        self._update_counts()

    def _toggle_uid(self, uid: int) -> None:
        entry = self._entry(uid)
        if entry is None or entry.unidentified:
            return
        if uid in self.checked:
            self.checked.discard(uid)
        else:
            self.checked.add(uid)
        self._touched.add(uid)
        self._refresh_row(entry)
        self._update_counts()

    def _toggle_all(self) -> None:
        identified = [e for e in self.entries if not e.unidentified]
        tick = not all(e.uid in self.checked for e in identified)
        for entry in identified:
            (self.checked.add if tick else self.checked.discard)(entry.uid)
            self._touched.add(entry.uid)
            self._refresh_row(entry)
        self._update_counts()

    def _on_tree_click(self, event: tk.Event) -> None:
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        row = self.tree.identify_row(event.y)
        if row:
            self._toggle_uid(int(row))

    def _on_tree_space(self, _event: tk.Event) -> str:
        row = self.tree.focus()
        if row:
            self._toggle_uid(int(row))
        return "break"

    def _on_tree_double(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        entry = self._entry(int(row)) if row else None
        if entry is None:
            return
        # The two clicks of a double-click toggle the box twice, leaving it as it was.
        match = self.spotify_matches.get(entry.uid)
        url = (match.url if match else "") or entry.shazam_url or entry.apple_music_url
        if url:
            webbrowser.open(url)

    # ------------------------------------------------------------- export ---

    def _tracklist_lines(self) -> list[str]:
        lines = []
        for entry in self._visible_entries():
            if entry.unidentified:
                lines.append(f"{entry.timestamp} ID - ID")
            else:
                line = f"{entry.timestamp} {entry.artist} - {entry.title}"
                lines.append(f"{line} [{entry.label}]" if entry.has_label else line)
        return lines

    def _default_export_name(self, ext: str) -> str:
        stem = Path(self.filepath).stem if self.filepath else "Mix"
        return f"{stem} - Tracklist{ext}"

    def _ensure_results(self) -> bool:
        if self._visible_entries():
            return True
        messagebox.showinfo(APP_NAME, "No tracks to export yet.", parent=self)
        return False

    def _copy_clipboard(self) -> None:
        if not self._ensure_results():
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(e.display_line for e in self._visible_entries()))
        self.update_idletasks()
        self._set_status("Tracklist copied to clipboard")

    def _export_txt(self) -> None:
        if not self._ensure_results():
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export tracklist", defaultextension=".txt",
            initialfile=self._default_export_name(".txt"), filetypes=[("Text", "*.txt")])
        if not path:
            return
        name = Path(self.filepath).stem if self.filepath else "Mix"
        body = "\n".join([name, "", *self._tracklist_lines()]) + "\n"
        try:
            Path(path).write_text(body, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't save file:\n{exc}", parent=self)
            return
        self._set_status(f"Saved {Path(path).name}")

    def _export_csv(self) -> None:
        if not self._ensure_results():
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export CSV", defaultextension=".csv",
            initialfile=self._default_export_name(".csv"), filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["Timestamp", "Seconds", "Artist", "Title", "Label", "Confidence",
                                 "Times Heard", "Album", "ISRC", "Apple Music URL", "Spotify URI",
                                 "Shazam URL"])
                for entry in self._visible_entries():
                    match = self.spotify_matches.get(entry.uid)
                    writer.writerow([
                        entry.timestamp, int(entry.seconds),
                        entry.artist, entry.title, entry.label if not entry.unidentified else "",
                        entry.confidence, entry.hits if not entry.unidentified else "",
                        entry.album, entry.isrc, entry.apple_music_url,
                        match.uri if match else entry.spotify_uri, entry.shazam_url,
                    ])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't save file:\n{exc}", parent=self)
            return
        self._set_status(f"Saved {Path(path).name}")

    # ------------------------------------------------------------ spotify ---

    def _refresh_spotify_header(self) -> None:
        if self._auth_cancel is not None:
            self.spotify_btn.configure(text="Cancel Sign-in")
            self.spotify_label.configure(text="Waiting for browser…")
        elif self.spotify.is_connected:
            self.spotify_btn.configure(text="Disconnect")
            self.spotify_label.configure(text=f"● {self.spotify.display_name or 'Connected'}")
        else:
            self.spotify_btn.configure(text="Connect Spotify")
            self.spotify_label.configure(text="Not connected")
        busy = self._spotify_busy or not self.spotify.is_connected
        self.add_btn.configure(state="disabled" if busy else "normal")
        self.refresh_btn.configure(state="disabled" if busy else "normal")
        self.playlist_menu.configure(state="disabled" if busy else "normal")

    def _spotify_button(self) -> None:
        if self._auth_cancel is not None:
            self._auth_cancel.set()
            return
        if self.spotify.is_connected:
            if messagebox.askyesno(APP_NAME, "Disconnect from Spotify?", parent=self):
                self.spotify.disconnect()
                self.playlists.clear()
                self.playlist_menu.configure(values=[NO_PLAYLISTS])
                self.playlist_var.set(NO_PLAYLISTS)
                self._refresh_spotify_header()
            return
        self._connect_spotify()

    def _connect_spotify(self) -> None:
        client_id = PromptDialog.ask(
            self, title="Connect Spotify", ok_text="Connect",
            message="Paste the Client ID of your Spotify developer app.\n\n" + SETUP_STEPS,
            initial=self.spotify.client_id, link=("Open Dashboard", DASHBOARD_URL))
        if not client_id:
            return
        self.spotify.set_client_id(client_id)

        cancel = threading.Event()
        self._auth_cancel = cancel
        self._refresh_spotify_header()

        def done(name: str) -> None:
            self._auth_cancel = None
            self._refresh_spotify_header()
            self._set_status(f"Connected to Spotify as {name}")
            self.lift()
            self._load_playlists()

        def failed(exc: BaseException) -> None:
            self._auth_cancel = None
            self._refresh_spotify_header()
            if not cancel.is_set():
                messagebox.showerror("Spotify", str(exc), parent=self)

        self._run_bg(lambda: self.spotify.authorize(cancel=cancel), done, failed)

    def _spotify_failed(self, exc: BaseException) -> None:
        self._spotify_busy = False
        if isinstance(exc, SpotifyAuthError):
            self.spotify.disconnect()
        self._refresh_spotify_header()
        log.warning("Spotify: %s", exc)
        self._set_status(str(exc), warn=True)
        if not isinstance(exc, SpotifyError):
            exc = SpotifyError(f"Unexpected error: {exc}")
        messagebox.showerror("Spotify", str(exc), parent=self)

    def _load_playlists(self, select_id: str | None = None) -> None:
        if not self.spotify.is_connected or self._spotify_busy:
            return
        self._spotify_busy = True
        self._refresh_spotify_header()
        self.spotify_label.configure(text="Loading playlists…")

        def done(playlists: list[Playlist]) -> None:
            self._spotify_busy = False
            self.playlists.clear()
            for pl in playlists:
                label, n = pl.name, 2
                while label in self.playlists:
                    label, n = f"{pl.name} ({n})", n + 1
                self.playlists[label] = pl
            self.playlist_menu.configure(values=[NEW_PLAYLIST, *self.playlists])
            wanted = select_id or self.spotify.store.get("last_playlist_id")
            chosen = next((lbl for lbl, pl in self.playlists.items() if pl.id == wanted), None)
            self.playlist_var.set(chosen or (next(iter(self.playlists), NEW_PLAYLIST)))
            self._refresh_spotify_header()

        self._run_bg(self.spotify.playlists, done, self._spotify_failed)

    def _on_playlist_pick(self, value: str) -> None:
        if value != NEW_PLAYLIST:
            playlist = self.playlists.get(value)
            if playlist:
                self.spotify.store.update(last_playlist_id=playlist.id)
            return
        self._create_playlist()

    def _create_playlist(self) -> None:
        suggestion = Path(self.filepath).stem if self.filepath else "SetShazamer"
        name = PromptDialog.ask(self, title="New Spotify playlist", ok_text="Create",
                                message="Name for the new playlist (private):", initial=suggestion)
        if not name:
            # Back to whatever was selected before the "+ New" pick.
            last = self.spotify.store.get("last_playlist_id")
            label = next((lbl for lbl, pl in self.playlists.items() if pl.id == last), None)
            self.playlist_var.set(label or next(iter(self.playlists), NEW_PLAYLIST))
            return

        self._spotify_busy = True
        self._refresh_spotify_header()
        description = f"Tracklist from {Path(self.filepath).name}, found by SetShazamer" \
            if self.filepath else "Found by SetShazamer"

        def done(playlist: Playlist) -> None:
            self._spotify_busy = False
            self.spotify.store.update(last_playlist_id=playlist.id)
            self._set_status(f"Created playlist “{playlist.name}”")
            self._load_playlists(select_id=playlist.id)

        self._run_bg(lambda: self.spotify.create_playlist(name, description), done,
                     self._spotify_failed)

    def _set_spotify_status(self, entry: TrackEntry, text: str) -> None:
        self.spotify_status[entry.uid] = text
        iid = str(entry.uid)
        if self.tree.exists(iid):
            self.tree.set(iid, "spotify", text)

    def _add_to_spotify(self) -> None:
        if not self.spotify.is_connected or self._spotify_busy:
            return
        playlist = self.playlists.get(self.playlist_var.get())
        if playlist is None:
            if self.playlist_var.get() == NEW_PLAYLIST:
                self._create_playlist()
            else:
                messagebox.showinfo(APP_NAME, "Choose a playlist first.", parent=self)
            return
        picked = [e for e in self.entries if e.uid in self.checked and not e.unidentified]
        if not picked:
            messagebox.showinfo(APP_NAME, "Tick the tracks you want to add first.", parent=self)
            return

        self._spotify_busy = True
        self._refresh_spotify_header()
        self._set_status(f"Adding {len(picked)} track(s) to “{playlist.name}”…")
        skip_dupes = self.skip_dupes_var.get()
        matches = self.spotify_matches  # Only this executor thread writes it while busy.

        def job() -> dict[str, int]:
            existing = self.spotify.playlist_uris(playlist.id) if skip_dupes else set()
            to_add: list[tuple[TrackEntry, str]] = []
            counts = {"added": 0, "duplicate": 0, "missing": 0}
            for entry in picked:
                if entry.uid not in matches:
                    self._call_ui(lambda e=entry: self._set_spotify_status(e, "Searching…"))
                    matches[entry.uid] = self.spotify.find_track(entry)
                match = matches[entry.uid]
                if match is None:
                    counts["missing"] += 1
                    self._call_ui(lambda e=entry: self._set_spotify_status(e, "Not found"))
                elif match.uri in existing:
                    counts["duplicate"] += 1
                    self._call_ui(lambda e=entry: self._set_spotify_status(e, "Already there"))
                else:
                    existing.add(match.uri)  # Never add one Spotify track twice per batch.
                    to_add.append((entry, match.uri))
                    self._call_ui(lambda e=entry: self._set_spotify_status(e, "Matched"))
            if to_add:
                self.spotify.add_to_playlist(playlist.id, [uri for _, uri in to_add])
                for entry, _ in to_add:
                    self._call_ui(lambda e=entry: self._set_spotify_status(e, "✓ Added"))
            counts["added"] = len(to_add)
            return counts

        def done(counts: dict[str, int]) -> None:
            self._spotify_busy = False
            self._refresh_spotify_header()
            parts = [f"Added {counts['added']} to “{playlist.name}”"]
            if counts["duplicate"]:
                parts.append(f"{counts['duplicate']} already there")
            if counts["missing"]:
                parts.append(f"{counts['missing']} not on Spotify")
            self._set_status("  ·  ".join(parts), warn=bool(counts["missing"]))

        def failed(exc: BaseException) -> None:
            for entry in picked:
                if self.spotify_status.get(entry.uid) in ("Searching…", "Matched"):
                    self._set_spotify_status(entry, "Error")
            self._spotify_failed(exc)

        self._run_bg(job, done, failed)

    # --------------------------------------------------------------- loop ---

    def _poll(self) -> None:
        try:
            for _ in range(500):
                self._handle_scan_event(self.scan_events.get_nowait())
        except queue.Empty:
            pass
        try:
            for _ in range(500):
                self.ui_calls.get_nowait()()
        except queue.Empty:
            pass
        self._tick_clock()
        self.after(50, self._poll)

    def _on_close(self) -> None:
        if self.worker is not None:
            if not messagebox.askyesno(APP_NAME, "A scan is running. Stop it and quit?", parent=self):
                return
            self.worker.stop()
            self.worker.join(timeout=5)
        if self._auth_cancel is not None:
            self._auth_cancel.set()
        if self._download_cancel is not None:
            self._download_cancel.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()


def main() -> None:
    log_path = _setup_logging()
    log.info("Starting %s (log: %s)", APP_NAME, log_path)
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = SetShazamerApp()
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
        app._set_file(sys.argv[1])
    app.mainloop()


if __name__ == "__main__":
    main()
