from __future__ import annotations

import os
import queue
import shutil
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.config import BatchSettings
from core.ffmpeg_runner import run_job
from core.jobs import ConversionJob, scan_directory_for_jobs
from core.probe import check_dependencies


QUICK_PRESETS: dict[str, tuple[str, str]] = {
    "Retime 23.976 -> 25 (PAL speed-up)": ("retime", "23.976_to_25"),
    "Retime 23.976 -> 24": ("retime", "23.976_to_24"),
    "Retime 25 -> 23.976 (PAL to film)": ("retime", "25_to_23.976"),
    "Retime 24 -> 23.976": ("retime", "24_to_23.976"),
    "Retime 25 -> 24": ("retime", "25_to_24"),
    "Retime 24 -> 25": ("retime", "24_to_25"),
    "Convert only (no FPS retime)": ("convert", "none"),
}

CODEC_OPTIONS = ["auto", "aac", "ac3", "eac3", "mp3", "flac", "wav", "opus"]
CONTAINER_OPTIONS = ["auto", "m4a", "aac", "ac3", "eac3", "mp3", "flac", "wav", "opus"]
OVERWRITE_OPTIONS = ["skip", "overwrite", "rename"]
BITRATE_OPTIONS = ["auto", "128k", "160k", "192k", "224k", "256k", "320k", "384k", "448k", "640k"]
ENGINE_OPTIONS = [
    "ffmpeg",
    "rubberband_hq",
    "sox_hq",
]
ENGINE_LABELS = {
    "ffmpeg": "FFmpeg (Fast / Standard)",
    "rubberband_hq": "Rubber Band HQ (Better retime quality)",
    "sox_hq": "SoX HQ (Audio-focused retime)",
}


class FpsAudioGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("FPS Audio Batch Converter")
        self.geometry("1220x760")
        self.minsize(980, 640)

        self.jobs: list[ConversionJob] = []
        self.row_map: dict[str, str] = {}
        self.ui_queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.scan_thread: threading.Thread | None = None
        self.batch_thread: threading.Thread | None = None
        self.stop_requested = False
        self.batch_running = False

        self._init_vars()
        self._build_ui()
        self._start_ui_pump()
        self._check_dependencies()

    def _init_vars(self) -> None:
        cwd = Path.cwd()
        self.input_dir_var = tk.StringVar(value=str(cwd))
        self.output_dir_var = tk.StringVar(value=str(cwd / "converted_audio"))
        self.recursive_var = tk.BooleanVar(value=False)
        self.quick_preset_var = tk.StringVar(value=list(QUICK_PRESETS.keys())[0])
        self.codec_var = tk.StringVar(value="auto")
        self.container_var = tk.StringVar(value="auto")
        self.bitrate_var = tk.StringVar(value="192k")
        self.bitrate_var.set("auto")
        self.overwrite_var = tk.StringVar(value="skip")
        self.engine_var = tk.StringVar(value="ffmpeg")
        default_parallel = max(1, min(4, os.cpu_count() or 4))
        self.parallel_jobs_var = tk.IntVar(value=default_parallel)
        self.show_advanced_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")
        self.summary_var = tk.StringVar(value="Queue: 0 jobs")
        self.current_job_var = tk.StringVar(value="Current job: -")
        self.overall_progress_var = tk.DoubleVar(value=0.0)

    def _build_ui(self) -> None:
        self.configure(bg="#f2f4f7")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Root.TFrame", background="#eef2f7")
        style.configure("Card.TLabelframe", background="#ffffff", borderwidth=1, relief="solid")
        style.configure("Card.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Header.TLabel", background="#eef2f7", foreground="#111827", font=("Segoe UI", 20, "bold"))
        style.configure("SubHeader.TLabel", background="#eef2f7", foreground="#475467", font=("Segoe UI", 10))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TEntry", padding=6)
        style.configure("TCombobox", padding=4)

        root = ttk.Frame(self, padding=14, style="Root.TFrame")
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)
        root.rowconfigure(6, weight=1)

        title = ttk.Label(root, text="FPS Audio Batch Converter", style="Header.TLabel")
        title.grid(row=0, column=0, sticky="w")

        guide_text = (
            "Quick start: 1) Select input/output folders  2) Choose a preset  "
            "3) Click Scan  4) Click Run Batch\n"
            "Supports audio files and audio tracks inside MP4 / MKV / MOV."
        )
        ttk.Label(root, text=guide_text, justify="left", style="SubHeader.TLabel").grid(
            row=1, column=0, sticky="we", pady=(4, 12)
        )

        self._build_folders_panel(root).grid(row=2, column=0, sticky="we", pady=(0, 8))
        self._build_settings_panel(root).grid(row=3, column=0, sticky="we", pady=(0, 8))
        self._build_queue_panel(root).grid(row=4, column=0, sticky="nsew", pady=(0, 8))
        self._build_progress_panel(root).grid(row=5, column=0, sticky="we", pady=(0, 8))
        self._build_log_panel(root).grid(row=6, column=0, sticky="nsew")

    def _build_folders_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="Folders", padding=12, style="Card.TLabelframe")
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(4, weight=1)

        ttk.Label(panel, text="Input folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(panel, textvariable=self.input_dir_var).grid(row=0, column=1, sticky="we", padx=(8, 8))
        ttk.Button(panel, text="Browse...", command=self._choose_input_dir).grid(row=0, column=2, sticky="w")

        ttk.Label(panel, text="Output folder").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(panel, textvariable=self.output_dir_var).grid(row=1, column=1, sticky="we", padx=(8, 8), pady=(10, 0))
        ttk.Button(panel, text="Browse...", command=self._choose_output_dir).grid(row=1, column=2, sticky="w", pady=(8, 0))

        ttk.Checkbutton(panel, text="Scan subfolders (recursive)", variable=self.recursive_var).grid(
            row=0, column=3, columnspan=2, sticky="w", padx=(18, 0)
        )

        action_row = ttk.Frame(panel)
        action_row.grid(row=1, column=3, columnspan=2, sticky="e", padx=(16, 0), pady=(8, 0))
        ttk.Button(action_row, text="Scan", command=self.start_scan, style="Accent.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(action_row, text="Run Batch", command=self.start_batch, style="Accent.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(action_row, text="Stop", command=self.request_stop).pack(side="left", padx=(0, 6))
        ttk.Button(action_row, text="Clear Queue", command=self.clear_queue).pack(side="left")
        return panel

    def _build_settings_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="Conversion", padding=12, style="Card.TLabelframe")
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(3, weight=1)

        ttk.Label(panel, text="Quick preset").grid(row=0, column=0, sticky="w")
        preset_box = ttk.Combobox(
            panel,
            textvariable=self.quick_preset_var,
            values=list(QUICK_PRESETS.keys()),
            state="readonly",
        )
        preset_box.grid(row=0, column=1, sticky="we", padx=(8, 12))
        preset_box.bind("<<ComboboxSelected>>", lambda _e: self._on_preset_changed())

        ttk.Checkbutton(
            panel,
            text="Show advanced settings",
            variable=self.show_advanced_var,
            command=self._toggle_advanced,
        ).grid(row=0, column=2, columnspan=2, sticky="w")

        self.mode_info_label = ttk.Label(panel, text="")
        self.mode_info_label.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        self.advanced_frame = ttk.Frame(panel)
        self.advanced_frame.grid(row=2, column=0, columnspan=4, sticky="we", pady=(8, 0))
        for i in range(10):
            self.advanced_frame.columnconfigure(i, weight=1 if i % 2 == 1 else 0)

        ttk.Label(self.advanced_frame, text="Codec").grid(row=0, column=0, sticky="w")
        ttk.Combobox(self.advanced_frame, textvariable=self.codec_var, values=CODEC_OPTIONS, state="readonly").grid(
            row=0, column=1, sticky="we", padx=(6, 12)
        )
        ttk.Label(self.advanced_frame, text="Output format").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            self.advanced_frame,
            textvariable=self.container_var,
            values=CONTAINER_OPTIONS,
            state="readonly",
        ).grid(row=0, column=3, sticky="we", padx=(6, 12))
        ttk.Label(self.advanced_frame, text="Bitrate").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            self.advanced_frame,
            textvariable=self.bitrate_var,
            values=BITRATE_OPTIONS,
            state="normal",
            width=12,
        ).grid(
            row=0, column=5, sticky="w", padx=(6, 12)
        )
        ttk.Label(self.advanced_frame, text="(auto = same as source)").grid(row=0, column=6, sticky="w")
        ttk.Label(self.advanced_frame, text="If output exists").grid(row=0, column=7, sticky="w")
        ttk.Combobox(
            self.advanced_frame,
            textvariable=self.overwrite_var,
            values=OVERWRITE_OPTIONS,
            state="readonly",
            width=12,
        ).grid(row=0, column=8, sticky="w", padx=(6, 0))

        ttk.Label(self.advanced_frame, text="Audio engine").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.engine_combo = ttk.Combobox(
            self.advanced_frame,
            textvariable=self.engine_var,
            values=ENGINE_OPTIONS,
            state="readonly",
            width=18,
        )
        self.engine_combo.grid(row=1, column=1, sticky="w", padx=(6, 12), pady=(8, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_engine_changed())

        self.engine_info_label = ttk.Label(self.advanced_frame, text="")
        self.engine_info_label.grid(row=1, column=2, columnspan=7, sticky="w", pady=(8, 0))

        ttk.Label(self.advanced_frame, text="Parallel jobs").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(
            self.advanced_frame,
            from_=1,
            to=32,
            textvariable=self.parallel_jobs_var,
            width=8,
        ).grid(row=2, column=1, sticky="w", padx=(6, 12), pady=(8, 0))
        ttk.Label(
            self.advanced_frame,
            text="Runs multiple FFmpeg processes at once (higher = faster, but uses more CPU/disk).",
        ).grid(row=2, column=2, columnspan=7, sticky="w", pady=(8, 0))

        self._toggle_advanced()
        self._on_preset_changed()
        self._on_engine_changed()
        return panel

    def _build_queue_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="Queue", padding=10, style="Card.TLabelframe")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)

        columns = ("source", "track", "codec", "lang", "ch", "status", "progress", "output")
        self.tree = ttk.Treeview(panel, columns=columns, show="headings", height=14)
        headings = {
            "source": "Source",
            "track": "Track",
            "codec": "Codec",
            "lang": "Lang",
            "ch": "Ch",
            "status": "Status",
            "progress": "Progress",
            "output": "Output",
        }
        widths = {
            "source": 260,
            "track": 60,
            "codec": 80,
            "lang": 70,
            "ch": 50,
            "status": 100,
            "progress": 80,
            "output": 420,
        }
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w", stretch=(col in {"source", "output"}))

        yscroll = ttk.Scrollbar(panel, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(panel, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        return panel

    def _build_progress_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="Progress", padding=12, style="Card.TLabelframe")
        panel.columnconfigure(0, weight=1)

        ttk.Label(panel, textvariable=self.current_job_var).grid(row=0, column=0, sticky="w")
        ttk.Progressbar(panel, variable=self.overall_progress_var, maximum=100).grid(
            row=1, column=0, sticky="we", pady=(6, 4)
        )
        ttk.Label(panel, textvariable=self.summary_var).grid(row=2, column=0, sticky="w")
        ttk.Label(panel, textvariable=self.status_var).grid(row=3, column=0, sticky="w", pady=(4, 0))
        return panel

    def _build_log_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="Log", padding=10, style="Card.TLabelframe")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)

        self.log_text = tk.Text(panel, height=10, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(panel, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        return panel

    def _choose_input_dir(self) -> None:
        path = filedialog.askdirectory(title="Select input folder", initialdir=self.input_dir_var.get() or str(Path.cwd()))
        if path:
            self.input_dir_var.set(path)

    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="Select output folder", initialdir=self.output_dir_var.get() or str(Path.cwd()))
        if path:
            self.output_dir_var.set(path)

    def _toggle_advanced(self) -> None:
        if self.show_advanced_var.get():
            self.advanced_frame.grid()
        else:
            self.advanced_frame.grid_remove()

    def _on_preset_changed(self) -> None:
        preset = self.quick_preset_var.get()
        mode, profile = QUICK_PRESETS.get(preset, ("retime", "23.976_to_25"))
        if mode == "retime":
            self.mode_info_label.config(
                text=f"Mode: FPS retime + convert | Profile: {profile.replace('_', ' ')}"
            )
        else:
            self.mode_info_label.config(text="Mode: Convert only (no FPS speed change)")
        self._on_engine_changed()

    def _on_engine_changed(self) -> None:
        preset = self.quick_preset_var.get()
        mode, _profile = QUICK_PRESETS.get(preset, ("retime", "23.976_to_25"))
        engine = self.engine_var.get() or "ffmpeg"
        if mode != "retime":
            self.engine_info_label.config(text="Engine is ignored in Convert-only mode (FFmpeg will be used).")
            return
        if engine == "rubberband_hq":
            self.engine_info_label.config(
                text="HQ retime using Rubber Band. Requires 'rubberband' installed in PATH."
            )
        elif engine == "sox_hq":
            self.engine_info_label.config(
                text="HQ retime using SoX tempo. Requires 'sox' installed in PATH."
            )
        else:
            self.engine_info_label.config(text="Fast retime using FFmpeg atempo filter (default).")

    def _check_dependencies(self) -> None:
        missing = check_dependencies()
        if missing:
            self._set_status(f"Missing dependencies: {', '.join(missing)}")
            self._log(f"Missing dependencies: {', '.join(missing)}")
            messagebox.showwarning(
                "Missing Dependencies",
                "This app needs ffmpeg and ffprobe.\n\n"
                f"Missing: {', '.join(missing)}\n\n"
                "Install FFmpeg and make sure ffmpeg/ffprobe are in PATH.",
            )

    def _collect_settings(self) -> BatchSettings:
        preset = self.quick_preset_var.get()
        mode, profile = QUICK_PRESETS.get(preset, ("retime", "23.976_to_25"))
        return BatchSettings(
            input_dir=Path(self.input_dir_var.get()).expanduser(),
            output_dir=Path(self.output_dir_var.get()).expanduser(),
            recursive=self.recursive_var.get(),
            profile_key=profile,
            mode=mode,
            engine=self.engine_var.get() or "ffmpeg",
            target_codec=self.codec_var.get() or "auto",
            target_container=self.container_var.get() or "auto",
            bitrate=(self.bitrate_var.get() or "auto").strip(),
            overwrite_policy=self.overwrite_var.get() or "skip",
            parallel_jobs=max(1, int(self.parallel_jobs_var.get() or 1)),
        )

    def start_scan(self) -> None:
        if self.batch_running:
            self._log("Cannot scan while batch is running")
            return
        if self.scan_thread and self.scan_thread.is_alive():
            self._log("Scan already in progress")
            return

        input_dir = Path(self.input_dir_var.get()).expanduser()
        if not input_dir.exists():
            messagebox.showerror("Input Folder", f"Input folder not found:\n{input_dir}")
            return

        self._set_status("Scanning files...")
        self._log(f"Scanning: {input_dir} | recursive={self.recursive_var.get()}")
        self.scan_thread = threading.Thread(target=self._scan_worker, daemon=True)
        self.scan_thread.start()

    def _scan_worker(self) -> None:
        settings = self._collect_settings()
        scan = scan_directory_for_jobs(settings.input_dir, recursive=settings.recursive)
        self.ui_queue.put(("scan_complete", {"jobs": scan.jobs, "errors": scan.errors}))

    def clear_queue(self) -> None:
        if self.batch_running:
            self._log("Cannot clear queue while batch is running")
            return
        self.jobs.clear()
        self.row_map.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.summary_var.set("Queue: 0 jobs")
        self.current_job_var.set("Current job: -")
        self.overall_progress_var.set(0.0)
        self._set_status("Queue cleared")

    def request_stop(self) -> None:
        self.stop_requested = True
        self._set_status("Stop requested (current job will finish)")
        self._log("Stop requested")

    def start_batch(self) -> None:
        if self.batch_running:
            self._log("Batch already running")
            return
        runnable = [job for job in self.jobs if job.audio_stream.stream_index >= 0]
        if not runnable:
            messagebox.showinfo("Queue", "No runnable jobs. Click Scan first.")
            return

        settings = self._collect_settings()
        if settings.mode == "retime":
            if settings.engine == "rubberband_hq" and shutil.which("rubberband") is None:
                messagebox.showerror(
                    "Rubber Band Not Found",
                    "Rubber Band HQ engine is selected, but 'rubberband' was not found in PATH.\n\n"
                    "Install Rubber Band CLI or switch engine to 'ffmpeg'.",
                )
                return
            if settings.engine == "sox_hq" and shutil.which("sox") is None:
                messagebox.showerror(
                    "SoX Not Found",
                    "SoX HQ engine is selected, but 'sox' was not found in PATH.\n\n"
                    "Install SoX or switch engine to 'ffmpeg'.",
                )
                return
        self.stop_requested = False
        self.batch_running = True
        self._set_status("Batch started")
        self._log(
            f"Starting batch: {len(runnable)} jobs | preset={self.quick_preset_var.get()} "
            f"| engine={settings.engine} | codec={settings.target_codec} | format={settings.target_container} "
            f"| parallel={settings.parallel_jobs}"
        )
        self.batch_thread = threading.Thread(target=self._batch_worker, args=(settings,), daemon=True)
        self.batch_thread.start()

    def _batch_worker(self, settings: BatchSettings) -> None:
        runnable_jobs = [job for job in self.jobs if job.audio_stream.stream_index >= 0]
        total = len(runnable_jobs)
        done = failed = skipped = 0
        submitted = 0
        completed = 0
        max_workers = max(1, min(settings.parallel_jobs, total or 1))

        self.ui_queue.put(("current_job", {"text": f"Active jobs: 0/{max_workers}"}))
        self.ui_queue.put(("log", {"text": f"Parallel processing enabled: {max_workers} FFmpeg jobs"}))

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ffmpeg-job") as pool:
            active: dict = {}

            while active or submitted < total:
                while not self.stop_requested and submitted < total and len(active) < max_workers:
                    job = runnable_jobs[submitted]
                    submitted += 1
                    self.ui_queue.put(("job_state", {"job_id": job.job_id, "status": "Running", "progress": 0.0}))
                    future = pool.submit(self._run_one_job_worker, job, settings)
                    active[future] = job

                self.ui_queue.put(("current_job", {"text": f"Active jobs: {len(active)}/{max_workers}"}))
                self.ui_queue.put(
                    (
                        "batch_progress",
                        {
                            "summary": (
                                f"Processed {completed}/{total} | done={done} failed={failed} skipped={skipped} "
                                f"| active={len(active)} queued={max(0, total - submitted)}"
                            )
                        },
                    )
                )

                if not active:
                    break

                finished, _ = wait(list(active.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
                if not finished:
                    continue

                for future in finished:
                    job = active.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:  # noqa: BLE001
                        outcome = {
                            "ok": False,
                            "status": "Failed",
                            "output": "",
                            "error": str(exc),
                        }

                    status = str(outcome.get("status", "Failed"))
                    completed += 1

                    if outcome.get("ok"):
                        if status == "Skipped":
                            skipped += 1
                        else:
                            done += 1
                    else:
                        failed += 1

                    job_payload = {
                        "job_id": job.job_id,
                        "status": status,
                        "progress": 100.0 if status in {"Done", "Skipped"} else float(getattr(job, "progress", 0.0)),
                    }
                    if outcome.get("output"):
                        job_payload["output"] = str(outcome["output"])
                    if outcome.get("error"):
                        job_payload["error"] = str(outcome["error"])
                    self.ui_queue.put(("job_state", job_payload))

                    if outcome.get("ok"):
                        self.ui_queue.put(("log", {"text": f"{status}: {job.job_id}"}))
                    else:
                        self.ui_queue.put(("log", {"text": f"Failed: {job.job_id} | {outcome.get('error', 'Unknown error')}"}))

            stopped_early = self.stop_requested and submitted < total

        payload = {"done": done, "failed": failed, "skipped": skipped, "total": total}
        if stopped_early:
            self.ui_queue.put(("batch_stopped", payload))
        else:
            self.ui_queue.put(("batch_done", payload))

    def _run_one_job_worker(self, job: ConversionJob, settings: BatchSettings) -> dict:
        def callback(kind: str, payload: dict) -> None:
            if kind == "progress":
                self.ui_queue.put(
                    (
                        "job_state",
                        {
                            "job_id": job.job_id,
                            "status": "Running",
                            "progress": float(payload.get("percent", 0.0)),
                        },
                    )
                )
            elif kind == "command":
                cmd = payload.get("cmd", [])
                if cmd:
                    self.ui_queue.put(("log", {"text": "$ " + " ".join(cmd)}))

        try:
            result = run_job(job, settings, callback=callback)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "Failed", "output": "", "error": str(exc)}

        if result.output_path is not None:
            job.output_path = result.output_path

        return {
            "ok": result.ok,
            "status": result.status,
            "output": str(result.output_path or ""),
            "error": result.error or "",
        }

    def _start_ui_pump(self) -> None:
        self.after(100, self._pump_ui_queue)

    def _pump_ui_queue(self) -> None:
        while True:
            try:
                event, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_ui_event(event, payload)
        self.after(100, self._pump_ui_queue)

    def _handle_ui_event(self, event: str, payload: dict) -> None:
        if event == "scan_complete":
            self.jobs = payload["jobs"]
            self._populate_tree()
            errors = payload.get("errors", [])
            for err in errors:
                self._log(f"Probe error: {err}")
            runnable = len([j for j in self.jobs if j.audio_stream.stream_index >= 0])
            self.summary_var.set(f"Queue: {len(self.jobs)} jobs ({runnable} runnable)")
            self.current_job_var.set("Current job: -")
            self.overall_progress_var.set(0.0)
            self._set_status(f"Scan complete: {len(self.jobs)} jobs, {runnable} runnable")
            return

        if event == "job_state":
            self._update_job_row(payload)
            return

        if event == "batch_progress":
            self.summary_var.set(payload.get("summary", self.summary_var.get()))
            if "overall" in payload:
                self.overall_progress_var.set(float(payload.get("overall", 0.0)))
            self._set_status(payload.get("summary", "Running"))
            return

        if event == "current_job":
            self.current_job_var.set(payload.get("text", "Current job: -"))
            return

        if event == "batch_done":
            self.batch_running = False
            self.current_job_var.set("Current job: -")
            self.overall_progress_var.set(100.0)
            msg = (
                f"Batch finished | total={payload['total']} "
                f"done={payload['done']} failed={payload['failed']} skipped={payload['skipped']}"
            )
            self.summary_var.set(msg)
            self._set_status(msg)
            self._log(msg)
            messagebox.showinfo("Batch Finished", msg)
            return

        if event == "batch_stopped":
            self.batch_running = False
            self.current_job_var.set("Current job: -")
            msg = (
                f"Batch stopped | total={payload['total']} "
                f"done={payload['done']} failed={payload['failed']} skipped={payload['skipped']}"
            )
            self.summary_var.set(msg)
            self._set_status(msg)
            self._log(msg)
            return

        if event == "log":
            self._log(payload.get("text", ""))

    def _populate_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.row_map.clear()

        for job in self.jobs:
            track = "-" if job.audio_stream.stream_index < 0 else f"a{job.audio_stream.stream_index}"
            item_id = self.tree.insert(
                "",
                "end",
                values=(
                    job.source_path.name,
                    track,
                    job.audio_stream.codec_name,
                    job.audio_stream.language or "-",
                    str(job.audio_stream.channels or "-"),
                    job.status,
                    f"{job.progress:.0f}%",
                    str(job.output_path or ""),
                ),
            )
            self.row_map[job.job_id] = item_id

    def _update_job_row(self, payload: dict) -> None:
        job_id = str(payload.get("job_id", ""))
        row_id = self.row_map.get(job_id)
        if not row_id:
            return

        for job in self.jobs:
            if job.job_id != job_id:
                continue
            if "status" in payload:
                job.status = str(payload["status"])
            if "progress" in payload:
                job.progress = float(payload["progress"])
            if "output" in payload and payload["output"]:
                job.output_path = Path(str(payload["output"]))
            if "error" in payload and payload["error"]:
                job.error = str(payload["error"])
                self._log(f"{job_id}: {job.error}")
            break

        values = list(self.tree.item(row_id, "values"))
        if not values:
            return
        values[5] = payload.get("status", values[5])
        if "progress" in payload:
            values[6] = f"{float(payload['progress']):.0f}%"
        if payload.get("output"):
            values[7] = str(payload["output"])
        self.tree.item(row_id, values=values)
        self._refresh_overall_progress()

    def _refresh_overall_progress(self) -> None:
        runnable = [job for job in self.jobs if job.audio_stream.stream_index >= 0]
        if not runnable:
            self.overall_progress_var.set(0.0)
            return
        avg = sum(max(0.0, min(100.0, float(job.progress))) for job in runnable) / len(runnable)
        self.overall_progress_var.set(avg)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _log(self, text: str) -> None:
        if not text:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    app = FpsAudioGui()
    app.mainloop()


if __name__ == "__main__":
    main()
