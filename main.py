import threading
import subprocess
import tempfile
import os
import re
from pathlib import Path

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()   # bundled binary, no system install needed

import customtkinter as ctk
from tkinter import filedialog, messagebox
import numpy as np

from pydub import AudioSegment
from pydub.silence import detect_nonsilent

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# Light palette — B2B Professional
BG       = "#F1F5F9"   # slate-100 background
PANEL    = "#FFFFFF"   # white cards
BORDER   = "#E2E8F0"   # slate-200 borders
TEXT     = "#0F172A"   # slate-900 headings
MUTED    = "#64748B"   # slate-500 secondary text
PRIMARY  = "#0F172A"   # navy primary button
ACCENT   = "#0369A1"   # sky-700 accent/CTA
KEEP     = "#0369A1"   # waveform kept region
REMOVE   = "#F87171"   # red-400 waveform removed
WAVE     = "#0F172A"   # waveform line color
WAVE_BG  = "#F8FAFC"   # waveform canvas background


class AutoCutApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AutoCut")
        self.geometry("1000x700")
        self.minsize(820, 600)
        self.configure(fg_color=BG)

        self.video_path = None
        self.audio: AudioSegment | None = None
        self.duration_ms = 0
        self.segments: list[tuple[int, int]] = []
        self._debounce_id = None
        # Pre-computed energy arrays (set once on audio load)
        self.energy_db: np.ndarray | None = None
        self.chunk_ms = 20
        self.wave_xs: np.ndarray | None = None
        self.wave_env: np.ndarray | None = None

        self._build_ui()

    # ── UI BUILD ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                              border_width=1, border_color=BORDER)
        header.pack(fill="x", padx=20, pady=(18, 0))

        # Logo mark + title
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
        self.import_btn.pack(side="right", padx=16, pady=14)

        self.file_label = ctk.CTkLabel(header, text="No file selected",
                                       font=("", 12), text_color=MUTED)
        self.file_label.pack(side="right", padx=4)

        # ── Waveform ──────────────────────────────────────────────────────────
        wave_card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                                 border_width=1, border_color=BORDER)
        wave_card.pack(fill="both", expand=True, padx=20, pady=14)

        # Section label
        wave_header = ctk.CTkFrame(wave_card, fg_color="transparent")
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

        self.canvas = FigureCanvasTkAgg(self.fig, master=wave_card)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=12, pady=(6, 12))

        self._draw_empty_waveform()

        # ── Controls ──────────────────────────────────────────────────────────
        controls_card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                                     border_width=1, border_color=BORDER)
        controls_card.pack(fill="x", padx=20, pady=(0, 14))

        inner = ctk.CTkFrame(controls_card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=16)
        inner.columnconfigure((0, 1, 2), weight=1)

        self.threshold_var  = ctk.DoubleVar(value=-40)
        self.min_silence_var = ctk.DoubleVar(value=500)
        self.padding_var    = ctk.DoubleVar(value=100)

        self._slider_col(inner, 0, "Silence Threshold",  self.threshold_var,  -70, -10,  "{:.0f} dB")
        self._slider_col(inner, 1, "Min Silence Length",  self.min_silence_var, 100, 3000, "{:.0f} ms")
        self._slider_col(inner, 2, "Padding",             self.padding_var,    0,   600,  "{:.0f} ms")

        # no Analyze button — sliders update in real-time

        # ── Footer ────────────────────────────────────────────────────────────
        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14,
                              border_width=1, border_color=BORDER)
        footer.pack(fill="x", padx=20, pady=(0, 18))

        self.stats_label = ctk.CTkLabel(footer, text="Import a video to begin",
                                        font=("", 12), text_color=MUTED)
        self.stats_label.pack(side="left", padx=20, pady=14)

        self.export_btn = ctk.CTkButton(
            footer, text="Export as One", width=145, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color=PRIMARY, hover_color="#1e293b",
            command=self.export_video, state="disabled"
        )
        self.export_btn.pack(side="right", padx=(0, 16), pady=14)

        self.export_clips_btn = ctk.CTkButton(
            footer, text="Export as Clips", width=145, height=36,
            corner_radius=8, font=("", 13, "bold"),
            fg_color=ACCENT, hover_color="#075985",
            command=self.export_clips, state="disabled"
        )
        self.export_clips_btn.pack(side="right", padx=(0, 8), pady=14)

        self.progress = ctk.CTkProgressBar(footer, width=160, height=6,
                                           corner_radius=3, fg_color=BORDER,
                                           progress_color=ACCENT)
        self.progress.set(0)
        self.progress_shown = False

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

        # Red tint = silence regions
        self.ax.axvspan(0, total_s, color=REMOVE, alpha=0.10, linewidth=0)

        # Blue tint = kept speech segments
        for s_ms, e_ms in self.segments:
            self.ax.axvspan(s_ms / 1000, e_ms / 1000, color=KEEP, alpha=0.10, linewidth=0)

        # Waveform — slate in silence, blue in kept
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

    # ── ANALYSIS — pre-computed energy, instant slider response ──────────────

    def _debounced_update(self):
        """80ms debounce — fast enough to feel real-time, avoids excess redraws."""
        if self._debounce_id:
            self.after_cancel(self._debounce_id)
        self._debounce_id = self.after(80, self._analyze_and_draw)

    def _compute_energy(self):
        """Run once after audio loads. Stores RMS-dB per 20ms chunk."""
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
        # Normalise to 16-bit full-scale so dB values match pydub convention
        self.energy_db = 20 * np.log10(rms / 32768.0)

        # Pre-compute display envelope (downsampled to ~3000 points)
        peak = np.abs(samples).max() or 1
        samples /= peak
        step = max(1, n // 3000)
        t = samples[: n - n % step].reshape(-1, step)
        self.wave_env = np.abs(t).max(axis=1)
        self.wave_xs  = np.linspace(0, self.duration_ms / 1000, len(self.wave_env))

    def _analyze_and_draw(self):
        """Pure numpy — completes in < 5 ms regardless of video length."""
        if self.energy_db is None:
            return

        threshold = self.threshold_var.get()
        min_sil_ms = int(self.min_silence_var.get())
        pad_ms     = int(self.padding_var.get())

        # 1. Boolean speech mask from pre-computed energy
        is_speech = self.energy_db > threshold

        # 2. Find raw speech runs (start/end in ms)
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

        # 3. Merge segments separated by less than min_sil_ms
        merged: list[list[int]] = []
        for s, e in raw:
            if merged and (s - merged[-1][1]) < min_sil_ms:
                merged[-1][1] = e
            else:
                merged.append([s, e])

        # 4. Add padding + merge overlaps
        result: list[tuple[int, int]] = []
        for s, e in merged:
            s = max(0, s - pad_ms)
            e = min(self.duration_ms, e + pad_ms)
            if result and s <= result[-1][1]:
                result[-1] = (result[-1][0], max(result[-1][1], e))
            else:
                result.append((s, e))

        self.segments = result

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
        self._draw_waveform()

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
        self.video_path = path
        self.file_label.configure(text=Path(path).name, text_color=TEXT)
        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self.stats_label.configure(text="Loading audio…")
        threading.Thread(target=self._load_audio, daemon=True).start()

    def _load_audio(self):
        try:
            # Extract audio to a small 16kHz mono WAV via ffmpeg — fast even for huge MOV files
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            self._tmp_audio = tmp.name

            subprocess.run(
                [
                    FFMPEG, "-y", "-i", self.video_path,
                    "-vn",                  # no video
                    "-ar", "16000",         # 16kHz sample rate (enough for silence detection)
                    "-ac", "1",             # mono
                    "-sample_fmt", "s16",
                    self._tmp_audio,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            self.audio = AudioSegment.from_wav(self._tmp_audio)
            self.duration_ms = len(self.audio)
            self._compute_energy()          # pre-compute once
            self.after(0, self._analyze_and_draw)
        except Exception as exc:
            self.after(0, lambda: self.stats_label.configure(text=f"Error: {exc}"))

    # ── EXPORT ────────────────────────────────────────────────────────────────

    def export_clips(self):
        if not self.segments:
            return
        folder = filedialog.askdirectory(title="Choose folder to save clips")
        if not folder:
            return
        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self._show_progress(True)
        self.stats_label.configure(text="Exporting clips…")
        threading.Thread(target=self._do_export_clips, args=(folder,), daemon=True).start()

    def _do_export_clips(self, folder: str):
        try:
            stem = Path(self.video_path).stem
            rotate = self._get_rotation()
            video_enc = self._get_video_encoder()
            n = len(self.segments)

            for i, (s_ms, e_ms) in enumerate(self.segments):
                self.after(0, lambda i=i: self.stats_label.configure(
                    text=f"Exporting clip {i+1}/{n}…"))
                out_path = os.path.join(folder, f"{stem}_clip_{i+1:03d}.mp4")
                r = subprocess.run([
                    FFMPEG, "-y",
                    "-ss", f"{s_ms/1000:.3f}",
                    "-i", self.video_path,
                    "-t", f"{(e_ms-s_ms)/1000:.3f}",
                    *video_enc,
                    "-c:a", "aac", "-b:a", "192k",
                    "-metadata:s:v:0", f"rotate={rotate}",
                    out_path,
                ], capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr[-400:])

            self.after(0, lambda: self._export_done(f"Saved {n} clips to folder!"))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self.stats_label.configure(text=f"Export failed: {msg}"))
            self.after(0, lambda: self.export_btn.configure(state="normal"))
            self.after(0, lambda: self.export_clips_btn.configure(state="normal"))
            self.after(0, lambda: self._show_progress(False))

    def export_video(self):
        if not self.segments:
            return

        stem = Path(self.video_path).stem
        out_path = filedialog.asksaveasfilename(
            title="Save Exported Video",
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4")],
            initialfile=f"{stem}_autocut.mp4",
        )
        if not out_path:
            return

        self.export_btn.configure(state="disabled")
        self.export_clips_btn.configure(state="disabled")
        self._show_progress(True)
        self.stats_label.configure(text="Exporting… this may take a moment")
        threading.Thread(target=self._do_export, args=(out_path,), daemon=True).start()

    def _get_video_encoder(self) -> list[str]:
        r = subprocess.run([FFMPEG, "-encoders"], capture_output=True, text=True)
        if "h264_videotoolbox" in r.stdout:
            # Mac hardware encoder — very fast
            return ["-c:v", "h264_videotoolbox", "-q:v", "60", "-pix_fmt", "yuv420p"]
        # Fallback: software with fast preset
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"]

    def _get_rotation(self) -> str:
        try:
            # ffmpeg -i prints stream info to stderr; parse rotate tag from there
            r = subprocess.run(
                [FFMPEG, "-i", self.video_path],
                capture_output=True, text=True
            )
            m = re.search(r"rotate\s*:\s*(-?\d+)", r.stderr)
            return m.group(1) if m else "0"
        except Exception:
            return "0"

    def _do_export(self, out_path: str):
        tmp_dir = tempfile.mkdtemp()
        segment_files: list[str] = []
        concat_txt = None
        try:
            if not self.segments:
                self.after(0, lambda: self.stats_label.configure(text="Nothing to export."))
                return

            rotate = self._get_rotation()
            video_enc = self._get_video_encoder()
            n = len(self.segments)

            for i, (s_ms, e_ms) in enumerate(self.segments):
                self.after(0, lambda i=i: self.stats_label.configure(
                    text=f"Exporting segment {i+1}/{n}…"))
                seg = os.path.join(tmp_dir, f"seg_{i:04d}.mp4")
                r = subprocess.run([
                    FFMPEG, "-y",
                    "-ss", f"{s_ms/1000:.3f}",
                    "-i", self.video_path,
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

            self.after(0, self._export_done)
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self.stats_label.configure(text=f"Export failed: {msg}"))
            self.after(0, lambda: self.export_btn.configure(state="normal"))
            self.after(0, lambda: self.export_clips_btn.configure(state="normal"))
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

    def _show_progress(self, show: bool):
        if show and not self.progress_shown:
            self.progress.pack(side="right", padx=10, pady=12)
            self.progress.configure(mode="indeterminate")
            self.progress.start()
            self.progress_shown = True
        elif not show and self.progress_shown:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.pack_forget()
            self.progress_shown = False


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _dot(parent, color: str, label: str) -> ctk.CTkFrame:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    ctk.CTkFrame(row, width=10, height=10, corner_radius=5, fg_color=color).pack(side="left", padx=(0, 5))
    ctk.CTkLabel(row, text=label, font=("", 11), text_color=MUTED).pack(side="left")
    return row


if __name__ == "__main__":
    app = AutoCutApp()
    app.mainloop()
