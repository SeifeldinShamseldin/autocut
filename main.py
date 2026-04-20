import threading
import subprocess
import tempfile
import os
import re
import json
import platform
import time as _time
import queue as _queue
import shutil
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
import tkinter as tk

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

import customtkinter as ctk
import numpy as np

from pydub import AudioSegment

from PIL import Image, ImageTk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# ── Optional drag-and-drop ────────────────────────────────────────────────────
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND = True
    _DND_IMPORT_ERROR = None
except ImportError as exc:
    _DND = False
    _DND_IMPORT_ERROR = exc

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# Light palette — B2B Professional
BG       = "#F1F5F9"
PANEL    = "#FFFFFF"
BORDER   = "#E2E8F0"
TEXT     = "#0F172A"
MUTED    = "#64748B"
PRIMARY  = "#0F172A"
ACCENT   = "#0369A1"
KEEP     = "#0369A1"
REMOVE   = "#F87171"
WAVE     = "#0F172A"
WAVE_BG  = "#F8FAFC"

PRESETS_FILE = Path.home() / ".autocut_presets.json"
SUPPORT_HANDLE = "seifeldin.shamseldin"
SUPPORT_INSTAGRAM_URL = "https://www.instagram.com/seifeldin.shamseldin/"

# Preview playback settings
_PREVIEW_W   = 480
_PREVIEW_H   = 270
_PREVIEW_FPS = 25


def _dot(parent, color: str, label: str) -> ctk.CTkFrame:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    ctk.CTkFrame(row, width=10, height=10, corner_radius=5, fg_color=color).pack(side="left", padx=(0, 5))
    ctk.CTkLabel(row, text=label, font=("", 11), text_color=MUTED).pack(side="left")
    return row


def _open_with_system(path: str):
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", path])
    elif system == "Windows":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


def _fmt_time(seconds: float) -> str:
    """Format seconds as M:SS."""
    s = int(seconds)
    m = s // 60
    s = s % 60
    return f"{m}:{s:02d}"


def _parse_drop_paths(data: str) -> list:
    """Parse tkinterdnd2 drop data handling {path with spaces} format."""
    paths = []
    data = data.strip()
    i = 0
    while i < len(data):
        if data[i] == "{":
            end = data.find("}", i)
            if end == -1:
                paths.append(data[i + 1:].strip())
                break
            paths.append(data[i + 1:end])
            i = end + 1
        else:
            # find next space or end
            j = i
            while j < len(data) and data[j] != " ":
                j += 1
            token = data[i:j].strip()
            if token:
                paths.append(token)
            i = j
        # skip whitespace between tokens
        while i < len(data) and data[i] == " ":
            i += 1
    return [p for p in paths if p]


def _video_info_text(path: str) -> str:
    try:
        r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
        return " ".join(line.strip() for line in r.stderr.splitlines() if "Video:" in line).lower()
    except Exception:
        return ""


def _is_hdr_video(path: str) -> bool:
    text = _video_info_text(path)
    return any(token in text for token in ("bt2020", "smpte2084", "arib-std-b67", "hlg"))


def _hdr_to_sdr_filter() -> str:
    return (
        "zscale=t=linear:npl=100,format=gbrpf32le,"
        "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
    )


# ── VideoPlayer widget ─────────────────────────────────────────────────────────

class VideoPlayer(ctk.CTkFrame):
    """Embedded video player widget using ffmpeg raw frame piping + PIL."""

    def __init__(self, master, segments_getter=None, timeline_getter=None,
                 position_callback=None, loading_callback=None, **kwargs):
        super().__init__(master, fg_color=PRIMARY, corner_radius=12, **kwargs)

        self._path: str | None = None
        self._duration: float = 0.0
        self._current_pos: float = 0.0
        self._playing: bool = False
        self._stop_event = threading.Event()
        self._photo = None  # keep reference to prevent GC
        self._segments_getter = segments_getter
        self._timeline_getter = timeline_getter
        self._position_callback = position_callback
        self._audio_proc = None
        self._audio_path = None
        self._preview_cache_path = None
        self._preview_cache_key = None
        self._loading_callback = loading_callback
        # Producer-consumer playback state
        self._frame_q: _queue.Queue | None = None
        self._display_job = None
        self._play_photo = None   # reused PhotoImage for paste() updates
        self._hdr_cache: dict[str, bool] = {}  # path → is_hdr

        # ── Canvas (video display) ─────────────────────────────────────────
        self._canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)

        # Placeholder text
        self._placeholder_id = self._canvas.create_text(
            160, 100,
            text="Drop video here\nor click Import",
            fill="#64748B",
            font=("", 13),
            justify="center",
        )

        # ── Controls bar ──────────────────────────────────────────────────
        controls = ctk.CTkFrame(self, fg_color="#0F172A", height=40)
        controls.pack(fill="x", side="bottom")
        controls.pack_propagate(False)

        self._play_btn = ctk.CTkButton(
            controls, text="▶", width=32, height=28,
            corner_radius=6, font=("", 14),
            fg_color="transparent", hover_color="#1e293b",
            text_color="#F1F5F9",
            command=self.toggle_play,
        )
        self._play_btn.pack(side="left", padx=(6, 2), pady=4)

        self._scrubber = ctk.CTkSlider(
            controls, from_=0, to=1,
            button_color="#0369A1", button_hover_color="#075985",
            progress_color="#0369A1", fg_color="#334155",
            command=self._on_scrub,
        )
        self._scrubber.set(0)
        self._scrubber.pack(side="left", fill="x", expand=True, padx=6, pady=8)

        self._time_label = ctk.CTkLabel(
            controls, text="0:00 / 0:00",
            font=("", 10), text_color="#94A3B8",
        )
        self._time_label.pack(side="left", padx=(2, 8))

    # ── Public API ────────────────────────────────────────────────────────

    def load(self, path: str):
        """Load a video file and show its first frame."""
        self.unload()
        self._path = path
        self._duration = self._get_duration(path)
        self._current_pos = 0.0
        self._scrubber.set(0)
        self._time_label.configure(text=f"0:00 / {_fmt_time(self._duration)}")
        # Show first frame in background
        threading.Thread(target=lambda: self._show_frame(0), daemon=True).start()

    def refresh_timeline(self, reset_position: bool = False):
        """Refresh preview duration after cut settings change."""
        if not self._path:
            return

        timeline = self._get_timeline()
        w = max(self._canvas.winfo_width(), 320)
        h = max(self._canvas.winfo_height(), 200)
        cache_key = self._make_preview_cache_key(timeline, w, h)
        if cache_key != self._preview_cache_key:
            self._clear_preview_cache()

        preview_duration = self._timeline_duration(timeline)
        if reset_position:
            self._current_pos = 0.0
        else:
            self._current_pos = min(self._current_pos, preview_duration)

        frac = self._current_pos / preview_duration if preview_duration > 0 else 0
        self._update_ui(frac, self._current_pos, preview_duration)

        if not self._playing:
            source_pos = self._source_pos_for_output(self._current_pos, timeline)
            threading.Thread(target=lambda: self._show_frame(source_pos), daemon=True).start()

    def unload(self):
        """Stop playback, clear canvas, show placeholder."""
        self._stop_playback()
        self._clear_preview_cache()
        self._path = None
        self._duration = 0.0
        self._current_pos = 0.0
        self._photo = None
        self._play_photo = None
        self._canvas.delete("all")
        w = max(self._canvas.winfo_width(), 320)
        h = max(self._canvas.winfo_height(), 200)
        self._placeholder_id = self._canvas.create_text(
            w // 2, h // 2,
            text="Drop video here\nor click Import",
            fill="#64748B",
            font=("", 13),
            justify="center",
        )
        self._play_btn.configure(text="▶")
        self._scrubber.set(0)
        self._time_label.configure(text="0:00 / 0:00")

    def toggle_play(self):
        if not self._path:
            return
        if self._playing:
            self._stop_playback()
        else:
            self._start_playback()

    # ── Internal ──────────────────────────────────────────────────────────

    def _get_duration(self, path: str) -> float:
        try:
            r = subprocess.run(
                [FFMPEG, "-i", path],
                capture_output=True, text=True,
            )
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
            if m:
                h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                return h * 3600 + mn * 60 + s
        except Exception:
            pass
        return 0.0

    def _get_timeline(self) -> list[tuple[int, int, float]]:
        """Return edited preview timeline as (source_start_ms, source_end_ms, speed)."""
        raw = []
        if self._timeline_getter:
            raw = self._timeline_getter() or []
        elif self._segments_getter:
            raw = [(s, e, 1.0) for s, e in (self._segments_getter() or [])]

        timeline: list[tuple[int, int, float]] = []
        for item in raw:
            if len(item) == 2:
                s_ms, e_ms = item
                speed = 1.0
            else:
                s_ms, e_ms, speed = item
            if e_ms > s_ms and speed > 0:
                timeline.append((int(s_ms), int(e_ms), float(speed)))

        if not timeline and self._duration > 0:
            timeline = [(0, int(self._duration * 1000), 1.0)]
        return timeline

    @staticmethod
    def _timeline_duration(timeline: list[tuple[int, int, float]]) -> float:
        return sum((e_ms - s_ms) / 1000 / speed for s_ms, e_ms, speed in timeline)

    def _source_pos_for_output(self, output_pos_s: float,
                               timeline: list[tuple[int, int, float]]) -> float:
        if not timeline:
            return 0.0

        cursor = 0.0
        for s_ms, e_ms, speed in timeline:
            out_dur = (e_ms - s_ms) / 1000 / speed
            if output_pos_s <= cursor + out_dur:
                local_out = max(0.0, output_pos_s - cursor)
                return min(e_ms / 1000, s_ms / 1000 + local_out * speed)
            cursor += out_dur

        return timeline[-1][1] / 1000

    def _locate_output_position(self, output_pos_s: float,
                                timeline: list[tuple[int, int, float]]):
        cursor = 0.0
        for idx, (s_ms, e_ms, speed) in enumerate(timeline):
            out_dur = (e_ms - s_ms) / 1000 / speed
            if output_pos_s <= cursor + out_dur or idx == len(timeline) - 1:
                local_out = max(0.0, output_pos_s - cursor)
                source_start_s = min(e_ms / 1000, s_ms / 1000 + local_out * speed)
                return idx, source_start_s, cursor + local_out
            cursor += out_dur
        return 0, 0.0, 0.0

    def _show_frame(self, pos_s: float):
        """Render a single frame at pos_s (background thread)."""
        if not self._path:
            return
        try:
            w = max(self._canvas.winfo_width(), 320)
            h = max(self._canvas.winfo_height(), 200)
            cmd = [
                FFMPEG,
                "-ss", f"{pos_s:.3f}",
                "-i", self._path,
                "-vframes", "1",
                "-vf", self._still_frame_filter(w, h),
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "pipe:1",
            ]
            r = subprocess.run(cmd, capture_output=True)
            if len(r.stdout) >= w * h * 3:
                img = Image.frombytes("RGB", (w, h), r.stdout[:w * h * 3])
                photo = ImageTk.PhotoImage(img)
                self._photo = photo
                self.after(0, lambda: self._blit(photo, w, h))
        except Exception:
            pass

    def _blit(self, photo, w: int, h: int):
        self._canvas.delete("all")
        self._canvas.create_image(w // 2, h // 2, image=photo, anchor="center")

    def _start_playback(self):
        self._stop_event.clear()
        self._playing = True
        self._play_btn.configure(text="⏸")
        self._frame_q = _queue.Queue(maxsize=12)
        self._play_photo = None
        threading.Thread(target=self._decode_frames, daemon=True).start()
        self._display_job = self.after(int(1000 / _PREVIEW_FPS), self._display_frame)

    def _stop_playback(self):
        self._stop_event.set()
        self._playing = False
        self._play_btn.configure(text="▶")
        self._stop_audio()
        if self._display_job is not None:
            self.after_cancel(self._display_job)
            self._display_job = None
        self._play_photo = None

    def _decode_frames(self):
        """Background thread: stream all segments via a SINGLE ffmpeg process.
        Uses concat demuxer with inpoint/outpoint — zero per-segment startup cost."""
        W, H = _PREVIEW_W, _PREVIEW_H
        frame_bytes = W * H * 3
        fi = 1.0 / _PREVIEW_FPS

        timeline = self._get_timeline()
        preview_duration = self._timeline_duration(timeline)
        if not timeline or preview_duration <= 0:
            self._frame_q.put(None)
            return

        start_pos_s = min(self._current_pos, preview_duration)
        seg_idx, source_start_s, output_cursor_s = self._locate_output_position(
            start_pos_s, timeline
        )

        hdr_prefix = f"{_hdr_to_sdr_filter()}," if self._cached_is_hdr() else ""
        scale_f = (
            f"{hdr_prefix}"
            f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black"
        )

        # Extract audio in parallel — video starts immediately
        threading.Thread(
            target=self._build_audio_and_play,
            args=(timeline, start_pos_s),
            daemon=True,
        ).start()

        self.after(0, lambda: self._play_btn.configure(text="⏸"))

        all_normal = all(spd == 1.0 for _, _, spd in timeline[seg_idx:])

        if all_normal:
            self._stream_concat(
                timeline, seg_idx, source_start_s, output_cursor_s,
                preview_duration, W, H, frame_bytes, scale_f, fi,
            )
        else:
            self._stream_per_segment(
                timeline, seg_idx, source_start_s, output_cursor_s,
                preview_duration, W, H, frame_bytes, scale_f, fi,
            )

        try:
            self._frame_q.put(None, timeout=1.0)
        except _queue.Full:
            pass

    def _stream_concat(self, timeline, seg_idx, source_start_s, output_cursor_s,
                       preview_duration, W, H, frame_bytes, scale_f, fi):
        """One ffmpeg via concat demuxer — handles all segments with no startup gap."""
        concat_f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        try:
            for idx in range(seg_idx, len(timeline)):
                s_ms, e_ms, _ = timeline[idx]
                seg_start = source_start_s if idx == seg_idx else s_ms / 1000
                seg_end = e_ms / 1000
                if seg_end <= seg_start:
                    continue
                safe = self._path.replace("'", "\\'")
                concat_f.write(f"file '{safe}'\n")
                concat_f.write(f"inpoint {seg_start:.6f}\n")
                concat_f.write(f"outpoint {seg_end:.6f}\n")
            concat_f.close()

            cmd = [
                FFMPEG,
                "-hwaccel", "auto",
                "-f", "concat", "-safe", "0", "-i", concat_f.name,
                "-vf", f"{scale_f},fps={_PREVIEW_FPS}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-an",
                "pipe:1",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            frame_count = 0
            loop_start = _time.monotonic()

            try:
                while not self._stop_event.is_set():
                    raw = proc.stdout.read(frame_bytes)
                    if len(raw) < frame_bytes:
                        break

                    frame_count += 1
                    elapsed = _time.monotonic() - loop_start
                    expected = (frame_count - 1) * fi

                    if self._frame_q.full() and elapsed > expected + fi:
                        continue

                    img = Image.frombytes("RGB", (W, H), raw)
                    pos = min(output_cursor_s + frame_count * fi, preview_duration)
                    source_pos = self._source_pos_for_output(pos, timeline)
                    frac = pos / preview_duration if preview_duration > 0 else 0

                    try:
                        self._frame_q.put(
                            (img, frac, pos, preview_duration, source_pos),
                            block=True, timeout=0.15,
                        )
                    except _queue.Full:
                        pass

                    sleep_dur = expected - (_time.monotonic() - loop_start)
                    if sleep_dur > 0.003:
                        _time.sleep(sleep_dur)
            finally:
                try:
                    proc.terminate()
                    proc.wait()
                except Exception:
                    pass
        finally:
            try:
                os.unlink(concat_f.name)
            except Exception:
                pass

    def _stream_per_segment(self, timeline, seg_idx, source_start_s, output_cursor_s,
                             preview_duration, W, H, frame_bytes, scale_f, fi):
        """Per-segment ffmpeg for speed-changed segments (2×/4×/8×)."""
        output_pos_s = output_cursor_s

        for idx in range(seg_idx, len(timeline)):
            if self._stop_event.is_set():
                break

            s_ms, e_ms, speed = timeline[idx]
            seg_start = source_start_s if idx == seg_idx else s_ms / 1000
            seg_dur = e_ms / 1000 - seg_start
            if seg_dur <= 0:
                continue

            if speed == 1.0:
                vf = f"{scale_f},fps={_PREVIEW_FPS}"
            else:
                vf = f"setpts={1.0/speed:.6f}*PTS,{scale_f},fps={_PREVIEW_FPS}"

            cmd = [
                FFMPEG,
                "-ss", f"{seg_start:.3f}",
                "-i", self._path,
                "-t", f"{seg_dur:.3f}",
                "-vf", vf,
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-an",
                "pipe:1",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            loop_start = _time.monotonic()
            frame_count = 0

            try:
                while not self._stop_event.is_set():
                    raw = proc.stdout.read(frame_bytes)
                    if len(raw) < frame_bytes:
                        break

                    frame_count += 1
                    elapsed = _time.monotonic() - loop_start
                    expected = (frame_count - 1) * fi

                    if self._frame_q.full() and elapsed > expected + fi:
                        continue

                    img = Image.frombytes("RGB", (W, H), raw)
                    pos = min(output_pos_s + frame_count * fi, preview_duration)
                    source_pos = self._source_pos_for_output(pos, timeline)
                    frac = pos / preview_duration if preview_duration > 0 else 0

                    try:
                        self._frame_q.put(
                            (img, frac, pos, preview_duration, source_pos),
                            block=True, timeout=0.15,
                        )
                    except _queue.Full:
                        pass

                    sleep_dur = expected - (_time.monotonic() - loop_start)
                    if sleep_dur > 0.003:
                        _time.sleep(sleep_dur)
            finally:
                try:
                    proc.terminate()
                    proc.wait()
                except Exception:
                    pass

            output_pos_s += seg_dur / speed

    def _build_audio_and_play(self, timeline, start_pos_s: float):
        """Background: build combined-cut audio file then play with afplay.
        Runs in parallel to video decode — audio starts a moment after video."""
        if platform.system() != "Darwin":
            return
        if self._stop_event.is_set():
            return
        try:
            audio_filter = self._build_audio_preview_filter(
                timeline, 0, timeline[0][0] / 1000
            )
            if not audio_filter:
                return

            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            cmd = [
                FFMPEG, "-y",
                "-i", self._path,
                "-filter_complex", audio_filter,
                "-map", "[outa]",
                "-vn", "-ar", "44100", "-ac", "2",
                tmp.name,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 or self._stop_event.is_set():
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
                return

            audio_path = tmp.name

            # Trim to start_pos_s if needed
            if start_pos_s > 0.1:
                tmp2 = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp2.close()
                r2 = subprocess.run(
                    [FFMPEG, "-y", "-ss", f"{start_pos_s:.3f}",
                     "-i", audio_path, tmp2.name],
                    capture_output=True, text=True,
                )
                try:
                    os.unlink(audio_path)
                except Exception:
                    pass
                if r2.returncode != 0:
                    try:
                        os.unlink(tmp2.name)
                    except Exception:
                        pass
                    return
                audio_path = tmp2.name

            if self._stop_event.is_set():
                try:
                    os.unlink(audio_path)
                except Exception:
                    pass
                return

            self._audio_path = audio_path
            self._audio_proc = subprocess.Popen(
                ["afplay", audio_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Race condition guard: close may have happened during extraction
            if self._stop_event.is_set():
                self._stop_audio()
        except Exception:
            pass

    def _display_frame(self):
        """Main thread: pull one frame from queue and blit it."""
        self._display_job = None
        if self._stop_event.is_set():
            return

        try:
            item = self._frame_q.get_nowait()
        except (_queue.Empty, AttributeError):
            # Queue empty — check back soon without blocking
            self._display_job = self.after(12, self._display_frame)
            return

        if item is None:
            # Decoder finished naturally
            self._playing = False
            self._play_photo = None
            self._current_pos = 0.0
            self._on_ended()
            return

        img, frac, pos, preview_duration, source_pos = item
        self._current_pos = pos

        cw = max(self._canvas.winfo_width(), _PREVIEW_W)
        ch = max(self._canvas.winfo_height(), _PREVIEW_H)

        if self._play_photo is None:
            # First frame: create PhotoImage and canvas item
            self._play_photo = ImageTk.PhotoImage(img)
            self._photo = self._play_photo
            self._canvas.delete("all")
            self._canvas.create_image(cw // 2, ch // 2,
                                       image=self._play_photo, anchor="center")
        else:
            # Subsequent frames: paste in-place (no Tk object allocation)
            self._play_photo.paste(img)

        self._update_ui(frac, pos, preview_duration, source_pos)

        self._display_job = self.after(int(1000 / _PREVIEW_FPS), self._display_frame)

    def _update_ui(self, frac: float, pos_s: float, duration_s: float | None = None,
                   source_pos_s: float | None = None):
        if duration_s is None:
            duration_s = self._timeline_duration(self._get_timeline())
        self._scrubber.set(max(0.0, min(1.0, frac)))
        self._time_label.configure(
            text=f"{_fmt_time(pos_s)} / {_fmt_time(duration_s)}"
        )
        if self._position_callback:
            if source_pos_s is None:
                source_pos_s = self._source_pos_for_output(pos_s, self._get_timeline())
            self._position_callback(pos_s, source_pos_s)

    def _on_ended(self):
        self._play_btn.configure(text="▶")
        self._scrubber.set(0)
        preview_duration = self._timeline_duration(self._get_timeline())
        self._time_label.configure(text=f"0:00 / {_fmt_time(preview_duration)}")
        # Show first frame again
        if self._path:
            source_pos = self._source_pos_for_output(0.0, self._get_timeline())
            threading.Thread(target=lambda: self._show_frame(source_pos), daemon=True).start()

    def _on_scrub(self, val):
        if not self._path or self._duration <= 0:
            return
        timeline = self._get_timeline()
        preview_duration = self._timeline_duration(timeline)
        pos = float(val) * preview_duration
        self._current_pos = pos
        self._time_label.configure(text=f"{_fmt_time(pos)} / {_fmt_time(preview_duration)}")
        source_pos = self._source_pos_for_output(pos, timeline)
        if self._position_callback:
            self._position_callback(pos, source_pos)
        if not self._playing:
            threading.Thread(target=lambda: self._show_frame(source_pos), daemon=True).start()

    @staticmethod
    def _atempo_chain(speed: float) -> str:
        filters = []
        remaining = speed
        while remaining > 2.0:
            filters.append("atempo=2.0")
            remaining /= 2.0
        filters.append(f"atempo={remaining:.6f}")
        return ",".join(filters)

    def _build_video_preview_filters(self, timeline, seg_start_idx, first_source_start_s, w, h):
        filters = []
        labels = []
        for out_idx, seg_idx in enumerate(range(seg_start_idx, len(timeline))):
            s_ms, e_ms, speed = timeline[seg_idx]
            seg_start_s = first_source_start_s if out_idx == 0 else s_ms / 1000
            seg_end_s = e_ms / 1000
            input_dur_s = seg_end_s - seg_start_s
            if input_dur_s <= 0:
                continue

            pts_factor = 1.0 / speed
            label = f"v{out_idx}"
            filters.append(
                f"[0:v]trim=start={seg_start_s:.6f}:end={seg_end_s:.6f},"
                f"setpts={pts_factor:.6f}*(PTS-STARTPTS)[{label}]"
            )
            labels.append(f"[{label}]")

        if not labels:
            return [], []

        if len(labels) == 1:
            filters.append(f"{labels[0]}{self._preview_frame_filter(w, h)}[outv]")
        else:
            filters.append(
                f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[vcat];"
                f"[vcat]{self._preview_frame_filter(w, h)}[outv]"
            )
        return filters, labels

    def _cached_is_hdr(self) -> bool:
        if not self._path:
            return False
        if self._path not in self._hdr_cache:
            self._hdr_cache[self._path] = _is_hdr_video(self._path)
        return self._hdr_cache[self._path]

    def _preview_frame_filter(self, w: int, h: int) -> str:
        chain = []
        if self._cached_is_hdr():
            chain.append(_hdr_to_sdr_filter())
        chain.extend([
            f"fps={_PREVIEW_FPS}",
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
        ])
        return ",".join(chain)

    def _still_frame_filter(self, w: int, h: int, apply_hdr: bool = True) -> str:
        chain = []
        if apply_hdr and self._cached_is_hdr():
            chain.append(_hdr_to_sdr_filter())
        chain.extend([
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
        ])
        return ",".join(chain)

    def _make_preview_cache_key(self, timeline, w: int | None = None, h: int | None = None):
        try:
            mtime = os.path.getmtime(self._path) if self._path else 0
        except Exception:
            mtime = 0
        return (self._path, mtime, w, h, tuple(timeline))

    def _clear_preview_cache(self):
        self._stop_audio()
        if self._preview_cache_path:
            try:
                os.unlink(self._preview_cache_path)
            except Exception:
                pass
        self._preview_cache_path = None
        self._preview_cache_key = None

    def _notify_loading(self, text: str):
        if self._loading_callback:
            self.after(0, lambda t=text: self._loading_callback(t))

    def _ensure_preview_cache(self, w: int, h: int, timeline):
        cache_key = self._make_preview_cache_key(timeline, w, h)
        if (
            self._preview_cache_path
            and self._preview_cache_key == cache_key
            and os.path.exists(self._preview_cache_path)
        ):
            return self._preview_cache_path

        self._clear_preview_cache()
        if not timeline:
            raise RuntimeError("No preview timeline")

        # Build scale filter once using cached HDR check
        hdr_prefix = f"{_hdr_to_sdr_filter()}," if self._cached_is_hdr() else ""
        scale_f = (
            f"{hdr_prefix}"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
        )

        tmp_dir = tempfile.mkdtemp()
        segment_files: list[str] = []
        out_path: str | None = None
        n = len(timeline)

        try:
            for i, (s_ms, e_ms, speed) in enumerate(timeline):
                if self._stop_event.is_set():
                    raise RuntimeError("Stopped")

                self._notify_loading(f"Building preview… {i+1}/{n}")

                seg_start = s_ms / 1000
                seg_dur = max((e_ms - s_ms) / 1000, 0.001)
                seg_path = os.path.join(tmp_dir, f"seg_{i:04d}.mp4")

                if speed == 1.0:
                    cmd = [
                        FFMPEG, "-y",
                        "-ss", f"{seg_start:.3f}",
                        "-i", self._path,
                        "-t", f"{seg_dur:.3f}",
                        "-vf", scale_f,
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "96k",
                        "-movflags", "+faststart",
                        seg_path,
                    ]
                else:
                    atempo = self._atempo_chain(speed)
                    cmd = [
                        FFMPEG, "-y",
                        "-ss", f"{seg_start:.3f}",
                        "-i", self._path,
                        "-t", f"{seg_dur:.3f}",
                        "-filter_complex",
                        f"[0:v]setpts={1.0/speed:.6f}*PTS,{scale_f}[v];[0:a]{atempo}[a]",
                        "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "96k",
                        "-movflags", "+faststart",
                        seg_path,
                    ]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr[-400:])
                segment_files.append(seg_path)

            self._notify_loading("Building preview… joining")

            tmp_out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            tmp_out.close()
            out_path = tmp_out.name

            if len(segment_files) == 1:
                r = subprocess.run(
                    [FFMPEG, "-y", "-i", segment_files[0], "-c", "copy",
                     "-movflags", "+faststart", out_path],
                    capture_output=True, text=True,
                )
            else:
                concat_txt = os.path.join(tmp_dir, "concat.txt")
                with open(concat_txt, "w") as f:
                    for seg in segment_files:
                        f.write(f"file '{seg}'\n")
                r = subprocess.run(
                    [FFMPEG, "-y", "-f", "concat", "-safe", "0",
                     "-i", concat_txt, "-c", "copy",
                     "-movflags", "+faststart", out_path],
                    capture_output=True, text=True,
                )

            if r.returncode != 0:
                try:
                    os.unlink(out_path)
                except Exception:
                    pass
                raise RuntimeError(r.stderr[-400:])

            self._preview_cache_path = out_path
            self._preview_cache_key = cache_key
            self._notify_loading("")
            return out_path

        finally:
            for seg in segment_files:
                try:
                    os.unlink(seg)
                except Exception:
                    pass
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _build_audio_preview_filter(self, timeline, seg_start_idx, first_source_start_s):
        filters = []
        labels = []
        for out_idx, seg_idx in enumerate(range(seg_start_idx, len(timeline))):
            s_ms, e_ms, speed = timeline[seg_idx]
            seg_start_s = first_source_start_s if out_idx == 0 else s_ms / 1000
            seg_end_s = e_ms / 1000
            if seg_end_s <= seg_start_s:
                continue

            label = f"a{out_idx}"
            chain = (
                f"[0:a]atrim=start={seg_start_s:.6f}:end={seg_end_s:.6f},"
                f"asetpts=PTS-STARTPTS"
            )
            if speed != 1.0:
                chain += f",{self._atempo_chain(speed)}"
            chain += f"[{label}]"
            filters.append(chain)
            labels.append(f"[{label}]")

        if not labels:
            return None
        if len(labels) == 1:
            filters.append(f"{labels[0]}anull[outa]")
        else:
            filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[outa]")
        return ";".join(filters)

    def _start_audio_preview(self, cache_path: str, start_pos_s: float):
        self._stop_audio()
        if platform.system() != "Darwin":
            return

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        cmd = [
            FFMPEG, "-y",
            "-ss", f"{start_pos_s:.3f}",
            "-i", cache_path,
            "-vn",
            "-ac", "2",
            "-ar", "44100",
            tmp.name,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            return

        self._audio_path = tmp.name
        self._audio_proc = subprocess.Popen(
            ["afplay", tmp.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop_audio(self):
        if self._audio_proc is not None:
            try:
                self._audio_proc.terminate()
                self._audio_proc.wait(timeout=1)
            except Exception:
                try:
                    self._audio_proc.kill()
                except Exception:
                    pass
            self._audio_proc = None

        if self._audio_path:
            try:
                os.unlink(self._audio_path)
            except Exception:
                pass
            self._audio_path = None


# ── Base class: CTk with optional tkinterdnd2 support ────────────────────────
if _DND:
    class _BaseClass(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    _BaseClass = ctk.CTk


class AutoCutApp(_BaseClass):
    def __init__(self):
        super().__init__()

        self.title("AutoCut")
        self.geometry("1200x800")
        self.minsize(950, 650)
        self.configure(fg_color=BG)

        # Core state
        self.video_path = None
        self.audio: AudioSegment | None = None
        self.duration_ms = 0
        self.segments: list[tuple[int, int]] = []
        self._debounce_id = None
        self.energy_db: np.ndarray | None = None
        self.chunk_ms = 20
        self.wave_xs: np.ndarray | None = None
        self.wave_env: np.ndarray | None = None
        self._wave_playhead = None
        self._wave_playhead_label = None

        # Audio-only preview state
        self._aud_path: str | None = None     # pre-built cut audio WAV
        self._aud_proc = None                  # afplay subprocess
        self._aud_playing = False
        self._aud_start_mono: float = 0        # monotonic when play started
        self._aud_start_pos: float = 0         # audio offset when play started
        self._aud_duration: float = 0
        self._aud_tick_job = None
        self._aud_paused_pos: float = 0.0      # position saved on pause

        # Undo/redo history: list of (threshold, min_silence, padding)
        self._history: list[tuple[float, float, float]] = []
        self._history_idx: int = -1
        self._history_updating: bool = False

        # Presets
        self._presets: dict[str, dict] = {}
        self._load_presets_from_disk()

        self._build_ui()
        self._bind_undo_redo()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Register drag-and-drop on entire window
        if _DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        elif _DND_IMPORT_ERROR:
            self.stats_label.configure(
                text="Import a video to begin (drag-and-drop unavailable: tkinterdnd2 not installed)"
            )

    # ── UI BUILD ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                              border_width=1, border_color=BORDER)
        header.pack(fill="x", padx=20, pady=(18, 0))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(side="left", padx=20, pady=14)
        ctk.CTkLabel(title_row, text="✂", font=("", 22), text_color=ACCENT).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(title_row, text="AutoCut", font=("", 20, "bold"), text_color=TEXT).pack(side="left")

        self.import_btn = ctk.CTkButton(
            header, text="+ Import Video", width=145, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color=PRIMARY, hover_color="#1e293b",
            command=self.import_video
        )
        self.import_btn.pack(side="right", padx=(0, 16), pady=14)

        self.remove_video_btn = ctk.CTkButton(
            header, text="Remove Video", width=120, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color=BORDER, text_color=TEXT, hover_color="#CBD5E1",
            command=self.remove_video, state="disabled"
        )
        self.remove_video_btn.pack(side="right", padx=(0, 8), pady=14)

        self.file_label = ctk.CTkLabel(header, text="No file selected",
                                       font=("", 12), text_color=MUTED)
        self.file_label.pack(side="right", padx=4)

        self.tab_view = ctk.CTkTabview(
            self,
            fg_color="transparent",
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color="#075985",
            segmented_button_unselected_color=BORDER,
            segmented_button_unselected_hover_color="#CBD5E1",
            text_color=TEXT,
        )
        self.tab_view.pack(fill="both", expand=True, padx=20, pady=(12, 18))
        self.editor_tab = self.tab_view.add("Editor")
        self.support_tab = self.tab_view.add("Support")
        self.tab_view.set("Editor")
        self.editor_tab.configure(fg_color="transparent")
        self.support_tab.configure(fg_color="transparent")

        self._build_support_tab(self.support_tab)

        # ── Content row: waveform (full width) ────────────────────────────
        self.content_row = ctk.CTkFrame(self.editor_tab, fg_color="transparent")
        content_row = self.content_row
        content_row.pack(fill="both", expand=True, padx=0, pady=14)

        self.wave_card = ctk.CTkFrame(content_row, fg_color=PANEL, corner_radius=14,
                                      border_width=1, border_color=BORDER)
        self.wave_card.pack(fill="both", expand=True)

        wave_header = ctk.CTkFrame(self.wave_card, fg_color="transparent")
        wave_header.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(wave_header, text="Audio Waveform", font=("", 12, "bold"),
                     text_color=TEXT).pack(side="left")

        # Playback position label
        self._pos_label = ctk.CTkLabel(wave_header, text="0:00 / 0:00",
                                       font=("", 11), text_color=MUTED)
        self._pos_label.pack(side="left", padx=16)

        legend = ctk.CTkFrame(wave_header, fg_color="transparent")
        legend.pack(side="right")
        _dot(legend, KEEP, "Keep").pack(side="left", padx=8)
        _dot(legend, REMOVE, "Remove").pack(side="left", padx=(0, 4))

        self.fig = Figure(figsize=(9, 4.0), dpi=100, facecolor=WAVE_BG)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor(WAVE_BG)
        self.ax.tick_params(colors=MUTED, labelsize=7)
        for sp in self.ax.spines.values():
            sp.set_edgecolor(BORDER)
        self.fig.tight_layout(pad=1.4)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.wave_card)
        self.wave_widget = self.canvas.get_tk_widget()
        self.wave_widget.pack(fill="both", expand=True, padx=12, pady=(6, 12))

        self._draw_empty_waveform()
        self.fig.canvas.mpl_connect("button_press_event", self._on_waveform_click)

        # Drag-and-drop bindings on waveform
        if _DND:
            self.wave_card.drop_target_register(DND_FILES)
            self.wave_card.dnd_bind("<<Drop>>", self._on_drop)
            self.wave_widget.drop_target_register(DND_FILES)
            self.wave_widget.dnd_bind("<<Drop>>", self._on_drop)

        # ── Controls ──────────────────────────────────────────────────────────
        controls_card = ctk.CTkFrame(self.editor_tab, fg_color=PANEL, corner_radius=14,
                                     border_width=1, border_color=BORDER)
        controls_card.pack(fill="x", padx=0, pady=(0, 6))

        inner = ctk.CTkFrame(controls_card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=(16, 8))
        inner.columnconfigure((0, 1, 2, 3), weight=1)

        self.threshold_var   = ctk.DoubleVar(value=-40)
        self.min_silence_var = ctk.DoubleVar(value=500)
        self.padding_var     = ctk.DoubleVar(value=100)

        self._slider_col(inner, 0, "Silence Threshold",  self.threshold_var,  -70, -10, "{:.0f} dB")
        self._slider_col(inner, 1, "Min Silence Length", self.min_silence_var, 100, 3000, "{:.0f} ms")
        self._slider_col(inner, 2, "Padding",            self.padding_var,    0,   600,  "{:.0f} ms")

        # Silence mode column
        mode_frame = ctk.CTkFrame(inner, fg_color="transparent")
        mode_frame.grid(row=0, column=3, sticky="ew", padx=(0, 0))
        ctk.CTkLabel(mode_frame, text="Silence Mode", font=("", 11, "bold"),
                     text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(mode_frame, text=" ", font=("", 12), text_color=ACCENT).pack(anchor="w", pady=(1, 4))
        self.silence_mode_var = ctk.StringVar(value="Cut")
        self.silence_seg_btn = ctk.CTkSegmentedButton(
            mode_frame,
            values=["Cut", "2×", "4×", "8×"],
            variable=self.silence_mode_var,
            command=self._on_silence_mode_change,
            font=("", 12),
            selected_color=ACCENT,
            selected_hover_color="#075985",
            unselected_color=BORDER,
            unselected_hover_color="#CBD5E1",
            text_color=TEXT,
            text_color_disabled=MUTED,
        )
        self.silence_seg_btn.pack(fill="x")

        # ── Settings row ──────────────────────────────────────────────────────
        settings_row = ctk.CTkFrame(controls_card, fg_color="transparent")
        settings_row.pack(fill="x", padx=20, pady=(0, 14))

        ctk.CTkLabel(settings_row, text="Format:", font=("", 11), text_color=MUTED).pack(side="left")
        self.format_var = ctk.StringVar(value="MP4")
        ctk.CTkOptionMenu(
            settings_row, variable=self.format_var,
            values=["MP4", "MOV"],
            width=80, height=28, font=("", 11),
            fg_color=BORDER, text_color=TEXT,
            button_color=BORDER, button_hover_color="#CBD5E1",
            dropdown_fg_color=PANEL, dropdown_text_color=TEXT,
        ).pack(side="left", padx=(4, 14))

        ctk.CTkLabel(settings_row, text="Quality:", font=("", 11), text_color=MUTED).pack(side="left")
        self.quality_var = ctk.StringVar(value="High CRF18")
        ctk.CTkOptionMenu(
            settings_row, variable=self.quality_var,
            values=["High CRF18", "Medium CRF23", "Low CRF28"],
            width=130, height=28, font=("", 11),
            fg_color=BORDER, text_color=TEXT,
            button_color=BORDER, button_hover_color="#CBD5E1",
            dropdown_fg_color=PANEL, dropdown_text_color=TEXT,
        ).pack(side="left", padx=(4, 20))

        ctk.CTkLabel(settings_row, text="Preset:", font=("", 11), text_color=MUTED).pack(side="left")
        self._preset_names_var = ctk.StringVar(value="(none)")
        self.preset_menu = ctk.CTkOptionMenu(
            settings_row, variable=self._preset_names_var,
            values=self._preset_menu_values(),
            width=130, height=28, font=("", 11),
            fg_color=BORDER, text_color=TEXT,
            button_color=BORDER, button_hover_color="#CBD5E1",
            dropdown_fg_color=PANEL, dropdown_text_color=TEXT,
            command=self._load_preset,
        )
        self.preset_menu.pack(side="left", padx=(4, 6))

        ctk.CTkButton(
            settings_row, text="Save", width=60, height=28,
            corner_radius=6, font=("", 11),
            fg_color=ACCENT, hover_color="#075985",
            command=self._save_preset
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            settings_row, text="Delete", width=65, height=28,
            corner_radius=6, font=("", 11),
            fg_color=BORDER, text_color=TEXT, hover_color="#CBD5E1",
            command=self._delete_preset
        ).pack(side="left")

        # ── Footer ────────────────────────────────────────────────────────────
        footer = ctk.CTkFrame(self.editor_tab, fg_color=PANEL, corner_radius=14,
                              border_width=1, border_color=BORDER)
        footer.pack(fill="x", padx=0, pady=(0, 0))

        self.stats_label = ctk.CTkLabel(footer, text="Import a video to begin",
                                        font=("", 12), text_color=MUTED)
        self.stats_label.pack(side="left", padx=20, pady=14)

        # Progress bar (right side)
        self.progress = ctk.CTkProgressBar(footer, width=160, height=6,
                                           corner_radius=3, fg_color=BORDER,
                                           progress_color=ACCENT)
        self.progress.set(0)
        self.progress_shown = False

        # Export as One (rightmost)
        self.export_btn = ctk.CTkButton(
            footer, text="Export as One", width=145, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color=PRIMARY, hover_color="#1e293b",
            command=self.export_video, state="disabled"
        )
        self.export_btn.pack(side="right", padx=(0, 16), pady=14)

        # Export as Clips
        self.export_clips_btn = ctk.CTkButton(
            footer, text="Export as Clips", width=145, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color=ACCENT, hover_color="#075985",
            command=self.export_clips, state="disabled"
        )
        self.export_clips_btn.pack(side="right", padx=(0, 8), pady=14)

        self.preview_btn = ctk.CTkButton(
            footer, text="▶  Play", width=115, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color=ACCENT, hover_color="#075985",
            command=self.preview_cut, state="disabled"
        )
        self.preview_btn.pack(side="right", padx=(0, 8), pady=14)

        # Undo/redo buttons
        self.redo_btn = ctk.CTkButton(
            footer, text="↪", width=36, height=36,
            corner_radius=8, font=("", 16),
            fg_color=BORDER, text_color=MUTED, hover_color="#CBD5E1",
            command=self._redo, state="disabled"
        )
        self.redo_btn.pack(side="right", padx=(0, 4), pady=14)

        self.undo_btn = ctk.CTkButton(
            footer, text="↩", width=36, height=36,
            corner_radius=8, font=("", 16),
            fg_color=BORDER, text_color=MUTED, hover_color="#CBD5E1",
            command=self._undo, state="disabled"
        )
        self.undo_btn.pack(side="right", padx=(0, 4), pady=14)

    def _build_support_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        support_card = ctk.CTkFrame(
            parent, fg_color=PANEL, corner_radius=14,
            border_width=1, border_color=BORDER
        )
        support_card.grid(row=0, column=0, sticky="nsew", padx=0, pady=14)

        inner = ctk.CTkFrame(support_card, fg_color="transparent")
        inner.pack(expand=True, padx=28, pady=28)

        ctk.CTkLabel(
            inner, text="Support", font=("", 20, "bold"), text_color=TEXT
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            inner, text=SUPPORT_HANDLE, font=("", 15, "bold"), text_color=ACCENT
        ).pack(pady=(0, 14))

        ctk.CTkButton(
            inner,
            text="◎",
            width=56,
            height=48,
            corner_radius=8,
            font=("", 24, "bold"),
            fg_color=PRIMARY,
            hover_color="#1e293b",
            command=self._open_support_instagram,
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            inner,
            text="Instagram",
            font=("", 12),
            text_color=MUTED,
        ).pack(pady=(0, 24))

        ctk.CTkLabel(
            inner,
            text=(
                "For inquiries: I am a robotics, automation, AI, and software "
                "engineer. This project is just for fun."
            ),
            font=("", 13),
            text_color=TEXT,
            wraplength=560,
            justify="center",
        ).pack()

    def _open_support_instagram(self):
        _open_with_system(SUPPORT_INSTAGRAM_URL)

    def _slider_col(self, parent, col, title, var, lo, hi, fmt):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=0, column=col, sticky="ew", padx=(0, 20))

        ctk.CTkLabel(frame, text=title, font=("", 11, "bold"),
                     text_color=TEXT).pack(anchor="w")
        val_label = ctk.CTkLabel(frame, text=fmt.format(var.get()),
                                 font=("", 12), text_color=ACCENT)
        val_label.pack(anchor="w", pady=(1, 4))

        def on_change(_=None):
            val_label.configure(text=fmt.format(var.get()))
            self._debounced_update()

        var.trace_add("write", lambda *_: val_label.configure(text=fmt.format(var.get())))

        ctk.CTkSlider(frame, from_=lo, to=hi, variable=var, command=on_change,
                      button_color=ACCENT, button_hover_color="#075985",
                      progress_color=ACCENT, fg_color=BORDER).pack(fill="x")

    # ── WAVEFORM ──────────────────────────────────────────────────────────────

    def _draw_empty_waveform(self):
        self.ax.clear()
        self.ax.set_facecolor(WAVE_BG)
        self.fig.patch.set_facecolor(WAVE_BG)
        self.ax.text(0.5, 0.5, "Import a video to see waveform",
                     transform=self.ax.transAxes, ha="center", va="center",
                     color=MUTED, fontsize=11)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        for sp in self.ax.spines.values():
            sp.set_edgecolor(BORDER)
        self.canvas.draw()
        self._wave_playhead = None
        self._wave_playhead_label = None

    def _draw_waveform(self):
        if self.wave_xs is None:
            return
        self.ax.clear()
        self.ax.set_facecolor(WAVE_BG)
        self.fig.patch.set_facecolor(WAVE_BG)

        xs      = self.wave_xs
        env     = self.wave_env
        total_s = self.duration_ms / 1000

        self.ax.axvspan(0, total_s, color=REMOVE, alpha=0.10, linewidth=0)
        for s_ms, e_ms in self.segments:
            self.ax.axvspan(s_ms / 1000, e_ms / 1000, color=KEEP, alpha=0.10, linewidth=0)

        self.ax.fill_between(xs, -env, env, color="#CBD5E1", alpha=1.0, linewidth=0)
        for s_ms, e_ms in self.segments:
            mask = (xs >= s_ms / 1000) & (xs <= e_ms / 1000)
            if mask.any():
                self.ax.fill_between(xs, -env, env, where=mask,
                                     color=KEEP, alpha=0.85, linewidth=0)

        self.ax.yaxis.grid(True, color=BORDER, linewidth=0.5, alpha=0.6)
        self.ax.set_axisbelow(True)
        self.ax.set_xlim(0, total_s)
        self.ax.set_ylim(-1.15, 1.15)
        self.ax.set_xlabel("seconds", color=MUTED, fontsize=8)
        self.ax.tick_params(colors=MUTED, labelsize=7)
        for sp in self.ax.spines.values():
            sp.set_edgecolor(BORDER)
        self.fig.tight_layout(pad=1.4)
        self.canvas.draw()
        self._wave_playhead = None
        self._wave_playhead_label = None

    def _on_preview_position(self, output_pos_s: float, source_pos_s: float):
        now = _time.monotonic()
        if now - self._last_playhead_draw < 0.08:
            return
        self._last_playhead_draw = now
        self._set_waveform_playhead(source_pos_s, output_pos_s)

    def _on_preview_loading(self, text: str):
        """Called by VideoPlayer while building the preview cache."""
        if text:
            self.stats_label.configure(text=text)
            if not self.progress_shown:
                self._show_progress(True)
            self.progress.configure(mode="indeterminate")
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._show_progress(False)

    def _set_waveform_playhead(self, source_pos_s: float, output_pos_s: float | None = None):
        if self.wave_xs is None:
            return

        total_s = self.duration_ms / 1000 if self.duration_ms else 0.0
        source_pos_s = max(0.0, min(source_pos_s, total_s))
        label = _fmt_time(output_pos_s if output_pos_s is not None else source_pos_s)

        if self._wave_playhead is None:
            self._wave_playhead = self.ax.axvline(
                source_pos_s, color="#111827", linewidth=1.4, alpha=0.95, zorder=10
            )
            self._wave_playhead_label = self.ax.text(
                source_pos_s, 1.08, label,
                ha="center", va="bottom", color="#111827", fontsize=8,
                bbox={"boxstyle": "round,pad=0.2", "facecolor": PANEL, "edgecolor": BORDER},
                zorder=11,
            )
        else:
            self._wave_playhead.set_xdata([source_pos_s, source_pos_s])
            if self._wave_playhead_label is not None:
                self._wave_playhead_label.set_position((source_pos_s, 1.08))
                self._wave_playhead_label.set_text(label)
        self.canvas.draw_idle()

    # ── ANALYSIS ──────────────────────────────────────────────────────────────

    def _debounced_update(self):
        if self._debounce_id:
            self.after_cancel(self._debounce_id)
        self._debounce_id = self.after(80, self._on_params_changed)

    def _on_params_changed(self):
        """Called when sliders change. Push undo history then analyze."""
        if not self._history_updating:
            self._push_history()
        self._analyze_and_draw()

    def _compute_energy(self):
        samples = np.array(self.audio.get_array_of_samples(), dtype=np.float32)
        if self.audio.channels == 2:
            samples = samples.reshape(-1, 2).mean(axis=1)

        sr = self.audio.frame_rate
        chunk = max(1, int(sr * self.chunk_ms / 1000))
        n = len(samples)
        n_chunks = n // chunk
        trimmed = samples[: n_chunks * chunk].reshape(n_chunks, chunk)

        rms = np.sqrt((trimmed ** 2).mean(axis=1))
        rms = np.maximum(rms, 1e-10)
        self.energy_db = 20 * np.log10(rms / 32768.0)

        peak = np.abs(samples).max() or 1
        samples /= peak
        step = max(1, n // 3000)
        t = samples[: n - n % step].reshape(-1, step)
        self.wave_env = np.abs(t).max(axis=1)
        self.wave_xs  = np.linspace(0, self.duration_ms / 1000, len(self.wave_env))

    def _compute_segments_from_energy(self,
                                      energy_db: np.ndarray,
                                      duration_ms: int,
                                      threshold: float,
                                      min_sil_ms: int,
                                      pad_ms: int) -> list[tuple[int, int]]:
        """Pure computation, no UI updates. Returns list of (start_ms, end_ms)."""
        is_speech = energy_db > threshold

        raw: list[list[int]] = []
        in_speech = False
        start = 0
        for i, sp in enumerate(is_speech):
            if sp and not in_speech:
                start = i
                in_speech = True
            elif not sp and in_speech:
                raw.append([start * self.chunk_ms, i * self.chunk_ms])
                in_speech = False
        if in_speech:
            raw.append([start * self.chunk_ms, len(is_speech) * self.chunk_ms])

        merged: list[list[int]] = []
        for s, e in raw:
            if merged and (s - merged[-1][1]) < min_sil_ms:
                merged[-1][1] = e
            else:
                merged.append([s, e])

        result: list[tuple[int, int]] = []
        for s, e in merged:
            s = max(0, s - pad_ms)
            e = min(duration_ms, e + pad_ms)
            if result and s <= result[-1][1]:
                result[-1] = (result[-1][0], max(result[-1][1], e))
            else:
                result.append((s, e))

        return result

    def _analyze_and_draw(self):
        if self.energy_db is None:
            return

        threshold  = self.threshold_var.get()
        min_sil_ms = int(self.min_silence_var.get())
        pad_ms     = int(self.padding_var.get())

        self.segments = self._compute_segments_from_energy(
            self.energy_db, self.duration_ms, threshold, min_sil_ms, pad_ms
        )

        kept_ms    = sum(e - s for s, e in self.segments)
        removed_ms = self.duration_ms - kept_ms
        kept_pct   = kept_ms / self.duration_ms * 100 if self.duration_ms else 0

        self.stats_label.configure(
            text=f"Keep {kept_ms/1000:.1f}s ({kept_pct:.0f}%)  ·  "
                 f"Preview {self._preview_duration_ms()/1000:.1f}s  ·  "
                 f"Remove {removed_ms/1000:.1f}s  ·  {len(self.segments)} clips"
        )
        state = "normal" if self.segments else "disabled"
        self.export_btn.configure(state=state)
        self.export_clips_btn.configure(state=state)
        self.preview_btn.configure(state=state)
        self._draw_waveform()
        self._aud_path = None
        self._aud_paused_pos = 0.0

    # ── SILENCE MODE ──────────────────────────────────────────────────────────

    def _on_silence_mode_change(self, value):
        if self.energy_db is not None:
            self._analyze_and_draw()

    def _get_silence_speed(self) -> float | None:
        """Returns None for Cut mode, or the speed multiplier."""
        val = self.silence_mode_var.get()
        if val == "Cut":
            return None
        return float(val.replace("×", ""))

    def _build_all_segments(self) -> list[tuple[int, int, float]]:
        """Returns list of (start_ms, end_ms, speed).
        Cut mode: only speech segments at 1.0.
        Speed mode: speech at 1.0, silence at speed.
        """
        speed = self._get_silence_speed()
        if speed is None:
            # Cut: only speech
            return [(s, e, 1.0) for s, e in self.segments]

        # Speed mode: interleave silence between speech segments
        result: list[tuple[int, int, float]] = []
        prev_end = 0
        for s_ms, e_ms in self.segments:
            if s_ms > prev_end:
                # Silence gap before this speech segment
                result.append((prev_end, s_ms, speed))
            result.append((s_ms, e_ms, 1.0))
            prev_end = e_ms
        # Trailing silence
        if prev_end < self.duration_ms:
            result.append((prev_end, self.duration_ms, speed))
        return result

    def _preview_duration_ms(self) -> float:
        return sum((e - s) / speed for s, e, speed in self._build_all_segments())

    @staticmethod
    def _atempo_chain(speed: float) -> str:
        """Build chained atempo filter string for speeds > 2x."""
        filters = []
        remaining = speed
        while remaining > 2.0:
            filters.append("atempo=2.0")
            remaining /= 2.0
        filters.append(f"atempo={remaining:.6f}")
        return ",".join(filters)

    # ── IMPORT ────────────────────────────────────────────────────────────────

    def import_video(self):
        path = filedialog.askopenfilename(
            title="Select Video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.m4v *.webm"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._load_single(path)

    def _load_single(self, path: str):
        self.video_path = path
        self.file_label.configure(text=Path(path).name, text_color=TEXT)
        self.remove_video_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self.stats_label.configure(text="Loading audio…")
        self._aud_stop()
        self._aud_path = None
        self._aud_paused_pos = 0.0
        threading.Thread(target=self._load_audio, daemon=True).start()

    def remove_video(self):
        self.video_path = None
        self.audio = None
        self.duration_ms = 0
        self.segments = []
        self.energy_db = None
        self.wave_xs = None
        self.wave_env = None
        self._aud_path = None
        self._aud_paused_pos = 0.0
        self._aud_duration = 0
        self._aud_stop()

        tmp_audio = getattr(self, "_tmp_audio", None)
        if tmp_audio:
            try:
                os.unlink(tmp_audio)
            except Exception:
                pass
            self._tmp_audio = None

        self.file_label.configure(text="No file selected", text_color=MUTED)
        self.remove_video_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self.preview_btn.configure(text="▶  Play", state="disabled")
        self._pos_label.configure(text="0:00 / 0:00")
        self.stats_label.configure(text="Import a video to begin")
        self._show_progress(False)
        self._draw_empty_waveform()

    def _load_audio(self):
        source_path = self.video_path
        if not source_path:
            return

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            tmp_audio_path = tmp.name
            self._tmp_audio = tmp_audio_path

            subprocess.run(
                [
                    FFMPEG, "-y", "-i", source_path,
                    "-vn", "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
                    tmp_audio_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            audio = AudioSegment.from_wav(tmp_audio_path)
            if self.video_path != source_path:
                try:
                    os.unlink(tmp_audio_path)
                except Exception:
                    pass
                if getattr(self, "_tmp_audio", None) == tmp_audio_path:
                    self._tmp_audio = None
                return

            self.audio = audio
            self.duration_ms = len(audio)
            self._compute_energy()
            self.after(0, self._analyze_and_draw)
        except Exception as exc:
            if self.video_path == source_path:
                self.after(0, lambda: self.stats_label.configure(text=f"Error: {exc}"))

    # ── DRAG & DROP ───────────────────────────────────────────────────────────

    def _on_drop(self, event):
        paths = _parse_drop_paths(event.data)
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}
        for p in paths:
            if Path(p).suffix.lower() in video_exts:
                self._load_single(p)
                return

    # ── UNDO / REDO ───────────────────────────────────────────────────────────

    def _bind_undo_redo(self):
        self.bind("<Control-z>", lambda e: self._undo())
        self.bind("<Control-y>", lambda e: self._redo())
        self.bind("<Command-z>", lambda e: self._undo())
        self.bind("<Command-Z>", lambda e: self._redo())  # Cmd+Shift+Z

    def _on_close(self):
        try:
            self._aud_stop()
        except Exception:
            pass
        self.destroy()

    def _push_history(self):
        state = (
            self.threshold_var.get(),
            self.min_silence_var.get(),
            self.padding_var.get(),
        )
        # Truncate forward history
        self._history = self._history[: self._history_idx + 1]
        self._history.append(state)
        self._history_idx = len(self._history) - 1
        self._update_undo_redo_buttons()

    def _apply_history(self, idx: int):
        self._history_updating = True
        t, m, p = self._history[idx]
        self.threshold_var.set(t)
        self.min_silence_var.set(m)
        self.padding_var.set(p)
        self._history_updating = False
        self._analyze_and_draw()

    def _undo(self):
        if self._history_idx > 0:
            self._history_idx -= 1
            self._apply_history(self._history_idx)
        self._update_undo_redo_buttons()

    def _redo(self):
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            self._apply_history(self._history_idx)
        self._update_undo_redo_buttons()

    def _update_undo_redo_buttons(self):
        self.undo_btn.configure(state="normal" if self._history_idx > 0 else "disabled")
        self.redo_btn.configure(state="normal" if self._history_idx < len(self._history) - 1 else "disabled")

    # ── PRESETS ───────────────────────────────────────────────────────────────

    def _load_presets_from_disk(self):
        try:
            if PRESETS_FILE.exists():
                self._presets = json.loads(PRESETS_FILE.read_text())
        except Exception:
            self._presets = {}

    def _save_presets_to_disk(self):
        try:
            PRESETS_FILE.write_text(json.dumps(self._presets, indent=2))
        except Exception as e:
            messagebox.showerror("Presets", f"Could not save presets: {e}")

    def _preset_menu_values(self) -> list[str]:
        names = list(self._presets.keys())
        return names if names else ["(none)"]

    def _refresh_preset_menu(self):
        vals = self._preset_menu_values()
        self.preset_menu.configure(values=vals)
        self._preset_names_var.set(vals[0])

    def _save_preset(self):
        name = simpledialog.askstring("Save Preset", "Preset name:", parent=self)
        if not name:
            return
        self._presets[name] = {
            "threshold":   self.threshold_var.get(),
            "min_silence": self.min_silence_var.get(),
            "padding":     self.padding_var.get(),
        }
        self._save_presets_to_disk()
        self._refresh_preset_menu()
        self._preset_names_var.set(name)

    def _load_preset(self, name: str):
        if name not in self._presets:
            return
        p = self._presets[name]
        self._history_updating = True
        self.threshold_var.set(p.get("threshold", -40))
        self.min_silence_var.set(p.get("min_silence", 500))
        self.padding_var.set(p.get("padding", 100))
        self._history_updating = False
        self._push_history()
        self._analyze_and_draw()

    def _delete_preset(self):
        name = self._preset_names_var.get()
        if name not in self._presets:
            return
        del self._presets[name]
        self._save_presets_to_disk()
        self._refresh_preset_menu()

    # ── EXPORT ────────────────────────────────────────────────────────────────

    def _get_crf(self) -> int:
        q = self.quality_var.get()
        if "18" in q:
            return 18
        elif "23" in q:
            return 23
        else:
            return 28

    def _get_extension(self) -> str:
        return self.format_var.get().lower()

    def _get_video_encoder(self) -> list[str]:
        crf = self._get_crf()
        return self._get_video_encoder_with_crf(crf)

    def _get_video_encoder_with_crf(self, crf: int, out_path: str = "") -> list[str]:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p"]

    def _get_video_filter_args(self, path: str) -> list[str]:
        if _is_hdr_video(path):
            return ["-vf", _hdr_to_sdr_filter()]
        return []

    def _get_segment_video_filter(self, speed: float, path: str) -> str:
        filters = [f"setpts={1.0 / speed:.6f}*PTS"]
        if _is_hdr_video(path):
            filters.append(_hdr_to_sdr_filter())
        return ",".join(filters)

    def _get_color_metadata_args(self, path: str) -> list[str]:
        if _is_hdr_video(path):
            return ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]

        text = _video_info_text(path)

        args: list[str] = []
        if "bt2020nc" in text:
            args.extend(["-colorspace", "bt2020nc"])
        elif "bt2020" in text:
            args.extend(["-colorspace", "bt2020nc"])
        elif "bt709" in text:
            args.extend(["-colorspace", "bt709"])

        if "bt2020" in text:
            args.extend(["-color_primaries", "bt2020"])
        elif "bt709" in text:
            args.extend(["-color_primaries", "bt709"])

        if "smpte2084" in text:
            args.extend(["-color_trc", "smpte2084"])
        elif "arib-std-b67" in text:
            args.extend(["-color_trc", "arib-std-b67"])
        elif "bt709" in text:
            args.extend(["-color_trc", "bt709"])

        return args

    def _get_rotation(self) -> str:
        return self._get_rotation_for(self.video_path)

    def _get_rotation_for(self, path: str) -> str:
        try:
            r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
            m = re.search(r"rotate\s*:\s*(-?\d+)", r.stderr)
            return m.group(1) if m else "0"
        except Exception:
            return "0"

    def _run_ffmpeg_with_progress(self, cmd: list[str], seg_duration_s: float,
                                   total_s: float, done_s: float,
                                   callback) -> subprocess.CompletedProcess:
        """Run ffmpeg with -progress pipe:1, parse out_time= and call callback(fraction)."""
        idx = 1
        full_cmd = cmd[:idx] + ["-progress", "pipe:1"] + cmd[idx:]

        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time="):
                time_str = line.split("=", 1)[1].strip()
                secs = _parse_ffmpeg_time(time_str)
                if secs is not None and total_s > 0:
                    overall = min(1.0, (done_s + min(secs, seg_duration_s)) / total_s)
                    callback(overall)

        proc.wait()
        return proc

    def _restore_action_buttons(self):
        state = "normal" if self.segments else "disabled"
        self.export_btn.configure(state=state)
        self.export_clips_btn.configure(state=state)
        self.preview_btn.configure(state=state)

    # ── AUDIO PREVIEW ─────────────────────────────────────────────────────────

    def preview_cut(self):
        """Toggle audio playback of the cut."""
        if not self.segments:
            return
        if self._aud_playing:
            # Save position then pause
            self._aud_paused_pos = self._aud_start_pos + (_time.monotonic() - self._aud_start_mono)
            self._aud_paused_pos = min(self._aud_paused_pos, self._aud_duration)
            self._aud_stop()
            return
        # Resume from paused position if audio already built
        if self._aud_path and os.path.exists(self._aud_path):
            self._aud_start(self._aud_paused_pos, self._aud_duration)
        else:
            self._aud_paused_pos = 0.0
            self.preview_btn.configure(text="Building…", state="disabled")
            threading.Thread(target=self._aud_build_and_play, daemon=True).start()

    def _aud_build_and_play(self, seek_pos: float = 0.0):
        """Background: extract cut audio then start playback."""
        if not self.segments:
            return
        try:
            audio_filter = self._build_audio_filter_for_segments()
            if not audio_filter:
                return
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            r = subprocess.run(
                [FFMPEG, "-y", "-i", self.video_path,
                 "-filter_complex", audio_filter,
                 "-map", "[outa]", "-vn", "-ar", "44100", "-ac", "2", tmp.name],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                self.after(0, lambda: self.preview_btn.configure(
                    text="▶  Play", state="normal"))
                return
            self._aud_path = tmp.name
            # Get duration
            dur = self._aud_wav_duration(tmp.name)
            self.after(0, lambda d=dur: self._aud_start(seek_pos, d))
        except Exception:
            self.after(0, lambda: self.preview_btn.configure(
                text="▶  Play", state="normal"))

    def _build_audio_filter_for_segments(self) -> str | None:
        """Build atrim/asetpts filter_complex for all kept segments."""
        filters, labels = [], []
        for i, (s_ms, e_ms) in enumerate(self.segments):
            s_s, e_s = s_ms / 1000, e_ms / 1000
            filters.append(
                f"[0:a]atrim=start={s_s:.6f}:end={e_s:.6f},"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
            labels.append(f"[a{i}]")
        if not labels:
            return None
        if len(labels) == 1:
            filters.append(f"{labels[0]}anull[outa]")
        else:
            filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[outa]")
        return ";".join(filters)

    def _aud_wav_duration(self, path: str) -> float:
        try:
            r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            pass
        return 0.0

    def _aud_start(self, pos: float, duration: float):
        """Main thread: start afplay from pos and kick off the tick loop."""
        self._aud_stop()
        if not self._aud_path or not os.path.exists(self._aud_path):
            return
        self._aud_duration = duration

        # Trim WAV to start position (fast PCM copy)
        if pos > 0.1:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            r = subprocess.run(
                [FFMPEG, "-y", "-ss", f"{pos:.3f}", "-i", self._aud_path, tmp.name],
                capture_output=True, text=True,
            )
            play_path = tmp.name if r.returncode == 0 else self._aud_path
        else:
            play_path = self._aud_path

        self._aud_proc = subprocess.Popen(
            ["afplay", play_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._aud_playing = True
        self._aud_start_mono = _time.monotonic()
        self._aud_start_pos = pos
        self.preview_btn.configure(text="⏸  Pause", state="normal")
        self._aud_tick()

    def _aud_stop(self):
        if self._aud_tick_job:
            self.after_cancel(self._aud_tick_job)
            self._aud_tick_job = None
        if self._aud_proc:
            try:
                self._aud_proc.terminate()
                self._aud_proc.wait(timeout=0.5)
            except Exception:
                pass
            self._aud_proc = None
        self._aud_playing = False
        self.preview_btn.configure(text="▶  Play", state="normal" if self.segments else "disabled")

    def _aud_tick(self):
        """Update waveform cursor every 50 ms."""
        if not self._aud_playing:
            return
        if self._aud_proc and self._aud_proc.poll() is not None:
            # afplay finished naturally
            self._aud_stop()
            self._set_waveform_playhead(0.0, 0.0)
            self._pos_label.configure(text=f"0:00 / {_fmt_time(self._aud_duration)}")
            return

        elapsed = _time.monotonic() - self._aud_start_mono
        out_pos = min(self._aud_start_pos + elapsed, self._aud_duration)
        src_pos = self._aud_pos_to_source(out_pos)
        self._set_waveform_playhead(src_pos, out_pos)
        self._pos_label.configure(
            text=f"{_fmt_time(out_pos)} / {_fmt_time(self._aud_duration)}"
        )
        self._aud_tick_job = self.after(50, self._aud_tick)

    def _aud_pos_to_source(self, out_pos_s: float) -> float:
        """Map output audio position → source video position."""
        cursor = 0.0
        for s_ms, e_ms in self.segments:
            seg_dur = (e_ms - s_ms) / 1000
            if out_pos_s <= cursor + seg_dur:
                return s_ms / 1000 + (out_pos_s - cursor)
            cursor += seg_dur
        return self.duration_ms / 1000

    def _on_waveform_click(self, event):
        """Click on waveform → seek audio to that source position."""
        if event.inaxes != self.ax or self.duration_ms == 0 or not self._aud_path:
            return
        src_pos = max(0.0, min(float(event.xdata), self.duration_ms / 1000))
        # Map source position → output audio position
        out_pos = 0.0
        for s_ms, e_ms in self.segments:
            s_s, e_s = s_ms / 1000, e_ms / 1000
            if src_pos <= s_s:
                break
            if src_pos <= e_s:
                out_pos += src_pos - s_s
                break
            out_pos += e_s - s_s
        out_pos = min(out_pos, self._aud_duration)
        self._set_waveform_playhead(src_pos, out_pos)
        if self._aud_playing:
            self._aud_stop()
            self.after(50, lambda p=out_pos, d=self._aud_duration: self._aud_start(p, d))

    def export_clips(self):
        if not self.segments:
            return
        folder = filedialog.askdirectory(title="Choose folder to save clips")
        if not folder:
            return
        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self._show_progress(True)
        self.progress.set(0)
        self.stats_label.configure(text="Exporting clips…")
        threading.Thread(target=self._do_export_clips, args=(folder,), daemon=True).start()

    def _do_export_clips(self, folder: str):
        try:
            stem = Path(self.video_path).stem
            rotate = self._get_rotation()
            video_enc = self._get_video_encoder()
            video_filter_args = self._get_video_filter_args(self.video_path)
            color_args = self._get_color_metadata_args(self.video_path)
            ext = self._get_extension()
            n = len(self.segments)
            total_s = self.duration_ms / 1000

            for i, (s_ms, e_ms) in enumerate(self.segments):
                self.after(0, lambda i=i: self.stats_label.configure(
                    text=f"Exporting clip {i+1}/{n}…"))
                out_path = os.path.join(folder, f"{stem}_clip_{i+1:03d}.{ext}")
                seg_dur = (e_ms - s_ms) / 1000
                done_s  = s_ms / 1000

                def _cb(v, btn=self.progress):
                    self.after(0, lambda: self.progress.set(v))

                cmd = [
                    FFMPEG, "-y",
                    "-ss", f"{s_ms/1000:.3f}",
                    "-i", self.video_path,
                    "-t", f"{seg_dur:.3f}",
                    *video_filter_args,
                    *video_enc,
                    *color_args,
                    "-c:a", "aac", "-b:a", "192k",
                    "-metadata:s:v:0", f"rotate={rotate}",
                    "-movflags", "+faststart",
                    out_path,
                ]
                proc = self._run_ffmpeg_with_progress(cmd, seg_dur, total_s, done_s, _cb)
                if proc.returncode != 0:
                    raise RuntimeError(f"ffmpeg failed for clip {i+1}")

            self.after(0, lambda: self._export_done(f"Saved {n} clips to folder!"))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self.stats_label.configure(text=f"Export failed: {msg}"))
            self.after(0, self._restore_action_buttons)
            self.after(0, lambda: self._show_progress(False))

    def export_video(self):
        if not self.segments:
            return

        stem = Path(self.video_path).stem
        ext  = self._get_extension()
        out_path = filedialog.asksaveasfilename(
            title="Save Exported Video",
            defaultextension=f".{ext}",
            filetypes=[("MP4", "*.mp4"), ("MOV", "*.mov")],
            initialfile=f"{stem}_autocut.{ext}",
        )
        if not out_path:
            return

        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self._show_progress(True)
        self.progress.set(0)
        self.stats_label.configure(text="Exporting… this may take a moment")
        all_segs = self._build_all_segments()
        threading.Thread(target=self._do_export_with_progress,
                         args=(out_path, all_segs, False), daemon=True).start()

    def _do_export_with_progress(self, out_path: str,
                                  all_segs: list[tuple[int, int, float]],
                                  low_quality: bool = False,
                                  open_after: bool = False,
                                  done_msg: str = "Export complete!"):
        tmp_dir = tempfile.mkdtemp()
        segment_files: list[str] = []
        concat_txt = None
        try:
            if not all_segs:
                self.after(0, lambda: self.stats_label.configure(text="Nothing to export."))
                self.after(0, self._restore_action_buttons)
                self.after(0, lambda: self._show_progress(False))
                return

            rotate    = self._get_rotation()
            crf       = 28 if low_quality else self._get_crf()
            video_enc = (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p"]
                         if low_quality else self._get_video_encoder())
            color_args = self._get_color_metadata_args(self.video_path)
            video_filter_args = self._get_video_filter_args(self.video_path)
            ext       = Path(out_path).suffix.lstrip(".")
            n         = len(all_segs)
            total_s   = self.duration_ms / 1000

            # Calculate total output duration for accurate progress
            total_output_s = sum((e - s) / speed / 1000 for s, e, speed in all_segs)
            done_output_s  = 0.0

            for i, (s_ms, e_ms, speed) in enumerate(all_segs):
                seg_dur_input  = (e_ms - s_ms) / 1000
                seg_dur_output = seg_dur_input / speed

                seg = os.path.join(tmp_dir, f"seg_{i:04d}.mp4")

                if speed == 1.0:
                    cmd = [
                        FFMPEG, "-y",
                        "-ss", f"{s_ms/1000:.3f}",
                        "-i", self.video_path,
                        "-t", f"{seg_dur_input:.3f}",
                        *video_filter_args,
                        *video_enc,
                        *color_args,
                        "-c:a", "aac", "-b:a", "192k",
                        seg,
                    ]
                else:
                    # Speed up silence: setpts for video, atempo chain for audio
                    atempo = self._atempo_chain(speed)
                    cmd = [
                        FFMPEG, "-y",
                        "-ss", f"{s_ms/1000:.3f}",
                        "-i", self.video_path,
                        "-t", f"{seg_dur_input:.3f}",
                        "-filter_complex",
                        f"[0:v]{self._get_segment_video_filter(speed, self.video_path)}[v];[0:a]{atempo}[a]",
                        "-map", "[v]", "-map", "[a]",
                        *video_enc,
                        *color_args,
                        "-c:a", "aac", "-b:a", "192k",
                        seg,
                    ]

                captured_done = done_output_s

                def _cb(v, d=captured_done, dur=seg_dur_output):
                    self.after(0, lambda: self.progress.set(v))

                proc = self._run_ffmpeg_with_progress(
                    cmd, seg_dur_output, total_output_s, done_output_s, _cb
                )
                if proc.returncode != 0:
                    # Fallback: run without progress tracking
                    plain_cmd = cmd.copy()
                    r2 = subprocess.run(plain_cmd, capture_output=True, text=True)
                    if r2.returncode != 0:
                        raise RuntimeError(r2.stderr[-400:])

                segment_files.append(seg)
                done_output_s += seg_dur_output

            if len(segment_files) == 1:
                r = subprocess.run([
                    FFMPEG, "-y", "-i", segment_files[0],
                    "-c", "copy", "-metadata:s:v:0", f"rotate={rotate}",
                    "-movflags", "+faststart",
                    out_path,
                ], capture_output=True, text=True)
            else:
                concat_txt = os.path.join(tmp_dir, "concat.txt")
                with open(concat_txt, "w") as f:
                    for seg in segment_files:
                        f.write(f"file '{seg}'\n")
                r = subprocess.run([
                    FFMPEG, "-y",
                    "-f", "concat", "-safe", "0", "-i", concat_txt,
                    "-c", "copy", "-metadata:s:v:0", f"rotate={rotate}",
                    "-movflags", "+faststart",
                    out_path,
                ], capture_output=True, text=True)

            if r.returncode != 0:
                raise RuntimeError(r.stderr[-400:])

            if open_after:
                self.after(0, lambda p=out_path: _open_with_system(p))
            self.after(0, lambda m=done_msg: self._export_done(m))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self.stats_label.configure(text=f"Export failed: {msg}"))
            self.after(0, self._restore_action_buttons)
            self.after(0, lambda: self._show_progress(False))
        finally:
            for seg in segment_files:
                try: os.unlink(seg)
                except: pass
            if concat_txt:
                try: os.unlink(concat_txt)
                except: pass
            try: os.rmdir(tmp_dir)
            except: pass

    def _export_done(self, msg="Export complete!"):
        self._show_progress(False)
        self.progress.set(1)
        self.stats_label.configure(text=msg)
        self._restore_action_buttons()

    def _show_progress(self, show: bool):
        if show and not self.progress_shown:
            self.progress.pack(side="right", padx=10, pady=12)
            self.progress.configure(mode="determinate")
            self.progress.set(0)
            self.progress_shown = True
        elif not show and self.progress_shown:
            self.progress.configure(mode="determinate")
            self.progress.pack_forget()
            self.progress_shown = False


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _parse_ffmpeg_time(time_str: str) -> float | None:
    """Parse out_time value from ffmpeg -progress output.
    Accepts HH:MM:SS.ffffff or plain seconds."""
    try:
        time_str = time_str.strip()
        if ":" in time_str:
            parts = time_str.split(":")
            h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
            return h * 3600 + m * 60 + s
        else:
            return float(time_str)
    except Exception:
        return None


if __name__ == "__main__":
    app = AutoCutApp()
    app.mainloop()
