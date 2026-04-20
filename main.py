import threading
import subprocess
import tempfile
import os
import re
import json
import platform
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
import tkinter as tk

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

import customtkinter as ctk
import numpy as np

from pydub import AudioSegment

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# ── Optional drag-and-drop ────────────────────────────────────────────────────
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND = True
except ImportError:
    _DND = False

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


# ── Base class: either TkinterDnD.Tk or ctk.CTk ──────────────────────────────
if _DND:
    _BaseClass = TkinterDnD.Tk
else:
    _BaseClass = ctk.CTk


class AutoCutApp(_BaseClass):
    def __init__(self):
        super().__init__()

        # When using TkinterDnD base, we need to set CTk appearance manually
        if _DND:
            ctk.set_appearance_mode("light")
            ctk.set_default_color_theme("blue")

        self.title("AutoCut")
        self.geometry("1100x780")
        self.minsize(900, 650)
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

        # Undo/redo history: list of (threshold, min_silence, padding)
        self._history: list[tuple[float, float, float]] = []
        self._history_idx: int = -1
        self._history_updating: bool = False

        # Batch state
        self.batch_files: list[str] = []

        # Presets
        self._presets: dict[str, dict] = {}
        self._load_presets_from_disk()

        self._build_ui()
        self._bind_undo_redo()

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

        self.batch_btn = ctk.CTkButton(
            header, text="+ Batch", width=100, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color="#7C3AED", hover_color="#6D28D9",
            command=self._add_batch_files
        )
        self.batch_btn.pack(side="right", padx=(0, 8), pady=14)

        self.file_label = ctk.CTkLabel(header, text="No file selected",
                                       font=("", 12), text_color=MUTED)
        self.file_label.pack(side="right", padx=4)

        # ── Batch file list (hidden until batch active) ────────────────────
        self.batch_frame = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                                        border_width=1, border_color=BORDER)
        # Not packed yet — shown only when batch_files is non-empty

        batch_inner = ctk.CTkFrame(self.batch_frame, fg_color="transparent")
        batch_inner.pack(fill="both", expand=True, padx=12, pady=8)

        batch_title_row = ctk.CTkFrame(batch_inner, fg_color="transparent")
        batch_title_row.pack(fill="x")
        ctk.CTkLabel(batch_title_row, text="Batch Files", font=("", 12, "bold"),
                     text_color=TEXT).pack(side="left")
        self.export_all_btn = ctk.CTkButton(
            batch_title_row, text="Export All", width=110, height=28,
            corner_radius=6, font=("", 12, "bold"),
            fg_color="#7C3AED", hover_color="#6D28D9",
            command=self._export_all_batch
        )
        self.export_all_btn.pack(side="right")

        clear_btn = ctk.CTkButton(
            batch_title_row, text="Clear", width=70, height=28,
            corner_radius=6, font=("", 12),
            fg_color=BORDER, text_color=TEXT, hover_color="#CBD5E1",
            command=self._clear_batch
        )
        clear_btn.pack(side="right", padx=(0, 6))

        self.batch_listbox = tk.Listbox(
            batch_inner, height=4, bg=WAVE_BG, fg=TEXT,
            selectbackground=ACCENT, selectforeground=PANEL,
            relief="flat", borderwidth=0, font=("", 11),
            activestyle="none"
        )
        self.batch_listbox.pack(fill="x", pady=(4, 0))
        self.batch_listbox.bind("<<ListboxSelect>>", self._on_batch_select)

        # ── Waveform ──────────────────────────────────────────────────────────
        self.wave_card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                                      border_width=1, border_color=BORDER)
        self.wave_card.pack(fill="both", expand=True, padx=20, pady=14)

        wave_header = ctk.CTkFrame(self.wave_card, fg_color="transparent")
        wave_header.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(wave_header, text="Audio Waveform", font=("", 12, "bold"),
                     text_color=TEXT).pack(side="left")
        legend = ctk.CTkFrame(wave_header, fg_color="transparent")
        legend.pack(side="right")
        _dot(legend, KEEP, "Keep").pack(side="left", padx=8)
        _dot(legend, REMOVE, "Remove").pack(side="left", padx=(0, 4))

        self.fig = Figure(figsize=(9, 2.8), dpi=100, facecolor=WAVE_BG)
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

        # Drag-and-drop bindings
        if _DND:
            self.wave_card.drop_target_register(DND_FILES)
            self.wave_card.dnd_bind("<<Drop>>", self._on_drop)
            self.wave_widget.drop_target_register(DND_FILES)
            self.wave_widget.dnd_bind("<<Drop>>", self._on_drop)

        # ── Controls ──────────────────────────────────────────────────────────
        controls_card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                                     border_width=1, border_color=BORDER)
        controls_card.pack(fill="x", padx=20, pady=(0, 6))

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
        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                              border_width=1, border_color=BORDER)
        footer.pack(fill="x", padx=20, pady=(0, 18))

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

        # Preview button (green)
        self.preview_btn = ctk.CTkButton(
            footer, text="▶ Preview", width=110, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color="#10B981", hover_color="#059669",
            command=self._preview, state="disabled"
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
                 f"Remove {removed_ms/1000:.1f}s  ·  {len(self.segments)} clips"
        )
        state = "normal" if self.segments else "disabled"
        self.export_btn.configure(state=state)
        self.export_clips_btn.configure(state=state)
        self.preview_btn.configure(state=state)
        self._draw_waveform()

    # ── SILENCE MODE ──────────────────────────────────────────────────────────

    def _on_silence_mode_change(self, value):
        pass  # Segments stay the same; mode is used at export time

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

    @staticmethod
    def _atempo_chain(speed: float) -> str:
        """Build chained atempo filter string for speeds > 2x."""
        # atempo supports 0.5–2.0; chain for higher speeds
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
        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self.stats_label.configure(text="Loading audio…")
        threading.Thread(target=self._load_audio, daemon=True).start()

    def _load_audio(self):
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            self._tmp_audio = tmp.name

            subprocess.run(
                [
                    FFMPEG, "-y", "-i", self.video_path,
                    "-vn", "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
                    self._tmp_audio,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            self.audio = AudioSegment.from_wav(self._tmp_audio)
            self.duration_ms = len(self.audio)
            self._compute_energy()
            self.after(0, self._analyze_and_draw)
        except Exception as exc:
            self.after(0, lambda: self.stats_label.configure(text=f"Error: {exc}"))

    # ── DRAG & DROP ───────────────────────────────────────────────────────────

    def _on_drop(self, event):
        path = event.data.strip().strip("{}")
        if path:
            self._load_single(path)

    # ── UNDO / REDO ───────────────────────────────────────────────────────────

    def _bind_undo_redo(self):
        self.bind("<Control-z>", lambda e: self._undo())
        self.bind("<Control-y>", lambda e: self._redo())
        self.bind("<Command-z>", lambda e: self._undo())
        self.bind("<Command-Z>", lambda e: self._redo())  # Cmd+Shift+Z

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

    # ── BATCH PROCESSING ──────────────────────────────────────────────────────

    def _add_batch_files(self):
        paths = filedialog.askopenfilenames(
            title="Select Videos for Batch",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.m4v *.webm"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        for p in paths:
            if p not in self.batch_files:
                self.batch_files.append(p)
                self.batch_listbox.insert(tk.END, Path(p).name)

        if self.batch_files:
            self.batch_frame.pack(fill="x", padx=20, pady=(8, 0), after=self._get_header_widget())

    def _get_header_widget(self):
        # Return the header frame (first child of self that is a CTkFrame)
        for child in self.winfo_children():
            if isinstance(child, ctk.CTkFrame):
                return child
        return None

    def _on_batch_select(self, event):
        sel = self.batch_listbox.curselection()
        if sel:
            path = self.batch_files[sel[0]]
            self._load_single(path)

    def _clear_batch(self):
        self.batch_files.clear()
        self.batch_listbox.delete(0, tk.END)
        self.batch_frame.pack_forget()

    def _export_all_batch(self):
        if not self.batch_files:
            return
        folder = filedialog.askdirectory(title="Choose folder to save batch exports")
        if not folder:
            return
        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self._show_progress(True)
        self.stats_label.configure(text="Batch export starting…")
        threading.Thread(target=self._do_batch_export, args=(folder,), daemon=True).start()

    def _do_batch_export(self, folder: str):
        # Save current state
        saved = (
            self.video_path,
            self.audio,
            self.duration_ms,
            self.energy_db,
            self.segments,
            self.wave_xs,
            self.wave_env,
        )

        threshold  = self.threshold_var.get()
        min_sil_ms = int(self.min_silence_var.get())
        pad_ms     = int(self.padding_var.get())
        fmt        = self.format_var.get().lower()
        crf        = self._get_crf()

        total = len(self.batch_files)
        for idx, path in enumerate(self.batch_files):
            try:
                self.after(0, lambda i=idx, t=total: self.stats_label.configure(
                    text=f"Batch: loading file {i+1}/{t}…"))

                # Load audio for this file
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp.close()
                subprocess.run(
                    [FFMPEG, "-y", "-i", path,
                     "-vn", "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
                     tmp.name],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                audio = AudioSegment.from_wav(tmp.name)
                duration_ms = len(audio)

                # Compute energy
                samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
                if audio.channels == 2:
                    samples = samples.reshape(-1, 2).mean(axis=1)
                sr = audio.frame_rate
                chunk = max(1, int(sr * self.chunk_ms / 1000))
                n = len(samples)
                n_chunks = n // chunk
                trimmed = samples[: n_chunks * chunk].reshape(n_chunks, chunk)
                rms = np.sqrt((trimmed ** 2).mean(axis=1))
                rms = np.maximum(rms, 1e-10)
                energy_db = 20 * np.log10(rms / 32768.0)

                segs = self._compute_segments_from_energy(
                    energy_db, duration_ms, threshold, min_sil_ms, pad_ms
                )

                if not segs:
                    continue

                stem = Path(path).stem
                out_path = os.path.join(folder, f"{stem}_autocut.{fmt}")

                self.after(0, lambda i=idx, t=total: self.stats_label.configure(
                    text=f"Batch: exporting file {i+1}/{t}…"))

                self._export_one_file(path, segs, duration_ms, out_path, crf)

                overall = (idx + 1) / total
                self.after(0, lambda v=overall: self.progress.set(v))

                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass

            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg, i=idx: self.stats_label.configure(
                    text=f"Batch error on file {i+1}: {m}"))

        # Restore state
        (self.video_path, self.audio, self.duration_ms,
         self.energy_db, self.segments, self.wave_xs, self.wave_env) = saved

        self.after(0, lambda: self._export_done(f"Batch export complete! {total} files."))

    def _export_one_file(self, video_path: str, segs: list[tuple[int, int]],
                          duration_ms: int, out_path: str, crf: int):
        """Export a single file using concat demuxer. No progress callback (batch loop handles it)."""
        tmp_dir = tempfile.mkdtemp()
        segment_files: list[str] = []
        concat_txt = None
        try:
            rotate = self._get_rotation_for(video_path)
            video_enc = self._get_video_encoder_with_crf(crf, out_path)
            n = len(segs)

            for i, (s_ms, e_ms) in enumerate(segs):
                seg = os.path.join(tmp_dir, f"seg_{i:04d}.mp4")
                r = subprocess.run([
                    FFMPEG, "-y",
                    "-ss", f"{s_ms/1000:.3f}",
                    "-i", video_path,
                    "-t", f"{(e_ms-s_ms)/1000:.3f}",
                    *video_enc,
                    "-c:a", "aac", "-b:a", "192k",
                    seg,
                ], capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr[-400:])
                segment_files.append(seg)

            if len(segment_files) == 1:
                r = subprocess.run([
                    FFMPEG, "-y", "-i", segment_files[0],
                    "-c", "copy", "-metadata:s:v:0", f"rotate={rotate}",
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
                    out_path,
                ], capture_output=True, text=True)

            if r.returncode != 0:
                raise RuntimeError(r.stderr[-400:])
        finally:
            for seg in segment_files:
                try: os.unlink(seg)
                except: pass
            if concat_txt:
                try: os.unlink(concat_txt)
                except: pass
            try: os.rmdir(tmp_dir)
            except: pass

    # ── PREVIEW ───────────────────────────────────────────────────────────────

    def _preview(self):
        if not self.segments or not self.video_path:
            return
        self.preview_btn.configure(state="disabled")
        self.stats_label.configure(text="Generating preview…")
        threading.Thread(target=self._do_preview, daemon=True).start()

    def _do_preview(self):
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            tmp.close()
            out_path = tmp.name

            # Low quality export
            all_segs = self._build_all_segments()
            self._do_export_with_progress(out_path, all_segs, low_quality=True)

            self.after(0, lambda: self.stats_label.configure(text="Opening preview…"))
            _open_with_system(out_path)
            self.after(0, lambda: self.preview_btn.configure(state="normal"))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self.stats_label.configure(text=f"Preview failed: {msg}"))
            self.after(0, lambda: self.preview_btn.configure(state="normal"))

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
        r = subprocess.run([FFMPEG, "-encoders"], capture_output=True, text=True)
        if "h264_videotoolbox" in r.stdout and not out_path.endswith(".mov"):
            return ["-c:v", "h264_videotoolbox", "-q:v", "60", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "fast", "-crf", str(crf), "-pix_fmt", "yuv420p"]

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
        # Insert -progress pipe:1 after the initial 'ffmpeg' call
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
                    *video_enc,
                    "-c:a", "aac", "-b:a", "192k",
                    "-metadata:s:v:0", f"rotate={rotate}",
                    out_path,
                ]
                proc = self._run_ffmpeg_with_progress(cmd, seg_dur, total_s, done_s, _cb)
                if proc.returncode != 0:
                    raise RuntimeError(f"ffmpeg failed for clip {i+1}")

            self.after(0, lambda: self._export_done(f"Saved {n} clips to folder!"))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self.stats_label.configure(text=f"Export failed: {msg}"))
            self.after(0, lambda: self.export_btn.configure(state="normal"))
            self.after(0, lambda: self.export_clips_btn.configure(state="normal"))
            self.after(0, lambda: self.preview_btn.configure(state="normal"))
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
                                  low_quality: bool = False):
        tmp_dir = tempfile.mkdtemp()
        segment_files: list[str] = []
        concat_txt = None
        try:
            if not all_segs:
                self.after(0, lambda: self.stats_label.configure(text="Nothing to export."))
                return

            rotate    = self._get_rotation()
            crf       = 28 if low_quality else self._get_crf()
            video_enc = (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p"]
                         if low_quality else self._get_video_encoder())
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
                        *video_enc,
                        "-c:a", "aac", "-b:a", "192k",
                        seg,
                    ]
                else:
                    # Speed up silence: setpts for video, atempo chain for audio
                    atempo = self._atempo_chain(speed)
                    pts_factor = 1.0 / speed
                    cmd = [
                        FFMPEG, "-y",
                        "-ss", f"{s_ms/1000:.3f}",
                        "-i", self.video_path,
                        "-t", f"{seg_dur_input:.3f}",
                        "-filter_complex",
                        f"[0:v]setpts={pts_factor:.6f}*PTS[v];[0:a]{atempo}[a]",
                        "-map", "[v]", "-map", "[a]",
                        *video_enc,
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
                    r = subprocess.run(cmd[: cmd.index("-progress")] if "-progress" in cmd else cmd,
                                       capture_output=True, text=True)
                    # Re-run without progress pipe
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
                    out_path,
                ], capture_output=True, text=True)

            if r.returncode != 0:
                raise RuntimeError(r.stderr[-400:])

            self.after(0, self._export_done)
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self.stats_label.configure(text=f"Export failed: {msg}"))
            self.after(0, lambda: self.export_btn.configure(state="normal"))
            self.after(0, lambda: self.export_clips_btn.configure(state="normal"))
            self.after(0, lambda: self.preview_btn.configure(state="normal"))
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
        self.export_btn.configure(state="normal")
        self.export_clips_btn.configure(state="normal")
        self.preview_btn.configure(state="normal")

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
