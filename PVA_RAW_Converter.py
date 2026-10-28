#!/usr/bin/env python3
"""
PVA RAW to JPEG Converter - Peak Valley Aerials

GUI tool: pick a source folder, tick the flight subfolders to process, and every
RAW inside is converted to a full-resolution JPEG sized to a target window
(default 7-10 MB) with all metadata (GPS, DJI XMP gimbal/altitude/RTK) copied over.

Requires: Python 3.9+, `pip install rawpy pillow numpy`, and ExifTool
(exiftool.exe on PATH or next to this script).
"""

import io
import json
import multiprocessing as mp
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "PVA RAW to JPEG Converter"
VERSION = "v1.0"
RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2",
            ".pef", ".srw", ".3fr", ".iiq"}
MB = 1024 * 1024
METADATA_HEADROOM = 256 * 1024
SETTINGS_FILE = Path.home() / ".pva_raw_converter.json"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # hide exiftool console windows


# ----------------------------------------------------------------------------
# Conversion core (runs in worker processes)
# ----------------------------------------------------------------------------

def find_exiftool():
    for name in ("exiftool", "exiftool.exe", "exiftool(-k).exe"):
        p = shutil.which(name)
        if p:
            return p
    here = Path(sys.argv[0]).resolve().parent
    for name in ("exiftool.exe", "exiftool(-k).exe", "exiftool"):
        if (here / name).exists():
            return str(here / name)
    return None


def _encode(img, quality):
    # 4:4:4 chroma at every quality keeps size rising smoothly with q.
    for optimize in (True, False):
        buf = io.BytesIO()
        try:
            img.save(buf, "JPEG", quality=quality, subsampling=0, optimize=optimize)
            return buf.getvalue()
        except OSError:
            if not optimize:
                raise


def _encode_to_target(img, lo_bytes, hi_bytes):
    """Binary-search JPEG quality so the file lands in [lo, hi], nearest the middle."""
    hi_bytes -= METADATA_HEADROOM
    target = (lo_bytes + hi_bytes) / 2
    cache = {}

    def size_at(q):
        if q not in cache:
            cache[q] = _encode(img, q)
        return len(cache[q])

    if size_at(100) < lo_bytes:
        return cache[100], 100, "below-min"
    lo_q, hi_q, best_q = 40, 100, None
    while lo_q <= hi_q:
        mid = (lo_q + hi_q) // 2
        if size_at(mid) > hi_bytes:
            hi_q = mid - 1
        else:
            best_q, lo_q = mid, mid + 1
    if best_q is None:
        return cache.get(40) or _encode(img, 40), 40, "above-max"
    fitting = [q for q in cache if lo_bytes <= len(cache[q]) <= hi_bytes]
    if fitting:
        q = min(fitting, key=lambda q: abs(len(cache[q]) - target))
        return cache[q], q, "ok"
    return cache[best_q], best_q, "ok" if len(cache[best_q]) >= lo_bytes else "below-min"


def _run_exiftool(exiftool, args):
    return subprocess.run([exiftool] + args, capture_output=True, text=True,
                          creationflags=NO_WINDOW)


def _set_file_times(path, src_stat):
    """Copy modified time (all OSes) and creation time (Windows) from the RAW."""
    os.utime(path, (src_stat.st_atime, src_stat.st_mtime))
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        ctime = getattr(src_stat, "st_birthtime", src_stat.st_ctime)
        ft = int(ctime * 10_000_000) + 116444736000000000
        ftime = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
        handle = ctypes.windll.kernel32.CreateFileW(str(path), 0x100, 0, None, 3, 0x80, None)
        if handle != -1:
            ctypes.windll.kernel32.SetFileTime(handle, ctypes.byref(ftime), None, None)
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def convert_one(raw_path, out_path, lo_mb, hi_mb, exiftool, keep_dates, overwrite):
    """Convert one RAW. Returns (status, message). Runs in a worker process."""
    import rawpy
    from PIL import Image

    raw_path, out_path = Path(raw_path), Path(out_path)
    if out_path.exists() and not overwrite:
        return "skip", f"'{out_path.name}': Skipped (exists)"
    try:
        with rawpy.imread(str(raw_path)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8, half_size=False)
        img = Image.fromarray(rgb)
        data, q, flag = _encode_to_target(img, int(lo_mb * MB), int(hi_mb * MB))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)

        r = _run_exiftool(exiftool, [
            "-q", "-q", "-overwrite_original", "-m",
            "-TagsFromFile", str(raw_path), "-all:all", "-unsafe",
            "--PreviewImage", "--ThumbnailImage", "--JpgFromRaw", "--OtherImage",
            "-Orientation=1", "-n", str(out_path)])
        if r.returncode != 0:
            return "fail", f"'{out_path.name}': Metadata copy failed: {r.stderr.strip()}"

        if keep_dates:
            _set_file_times(out_path, raw_path.stat())

        def gps(p):
            o = _run_exiftool(exiftool, ["-s3", "-n", "-GPSLatitude", "-GPSLongitude", str(p)])
            return [v for v in o.stdout.split() if v]

        src_gps, dst_gps = gps(raw_path), gps(out_path)
        size = out_path.stat().st_size / MB
        w, h = img.size
        if src_gps and not dst_gps:
            return "warn", f"'{out_path.name}': {w}x{h} q{q} {size:.2f} MB - GPS MISSING!"
        note = {"ok": "", "below-min": " (under min - low detail)",
                "above-max": " (over max at q40)"}[flag]
        gps_note = "GPS ok" if dst_gps else "no GPS in source"
        status = "ok" if flag == "ok" else "warn"
        return status, f"'{out_path.name}': {w}x{h} q{q} {size:.2f} MB, {gps_note}{note}"
    except Exception as e:
        return "fail", f"'{raw_path.name}': FAILED - {e}"


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

def list_raws(folder):
    try:
        return sorted(f for f in Path(folder).iterdir()
                      if f.is_file() and f.suffix.lower() in RAW_EXTS)
    except OSError:
        return []


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("1100x860")
        self.minsize(900, 700)

        self.msgq = queue.Queue()
        self.pause_evt = threading.Event()
        self.stop_evt = threading.Event()
        self.worker = None
        self.checked = set()

        s = self._load_settings()
        self.src_var = tk.StringVar(value=s.get("source", ""))
        self.dst_var = tk.StringVar(value=s.get("target", ""))
        self.keep_dates = tk.BooleanVar(value=s.get("keep_dates", True))
        self.overwrite = tk.BooleanVar(value=s.get("overwrite", False))
        self.min_mb = tk.DoubleVar(value=s.get("min_mb", 7.0))
        self.max_mb = tk.DoubleVar(value=s.get("max_mb", 10.0))
        self.workers = tk.IntVar(value=s.get("workers", max(1, (os.cpu_count() or 2) - 1)))
        self.progress_txt = tk.StringVar(value="Progress: 0% (Idle)")

        self._build()
        self.exiftool = find_exiftool()
        self.log(f"{APP_NAME} {VERSION} started.")
        if self.exiftool:
            self.log(f"ExifTool found: {self.exiftool}")
        else:
            self.log("WARNING: ExifTool not found. Put exiftool.exe next to this script or on PATH.")
        if self.src_var.get():
            self.scan_source()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._drain_queue)

    # ---------- layout ----------
    def _build(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Bold.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=24)
        pad = dict(padx=10, pady=4)

        # Locations
        loc = ttk.LabelFrame(self, text="Locations")
        loc.pack(fill="x", **pad)
        loc.columnconfigure(1, weight=1)
        ttk.Label(loc, text="Source Folder:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(loc, textvariable=self.src_var).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Button(loc, text="Browse...", command=self.browse_src).grid(row=0, column=2, padx=8)
        ttk.Label(loc, text="Target Folder:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(loc, textvariable=self.dst_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(loc, text="Browse...", command=self.browse_dst).grid(row=1, column=2, padx=8)

        # Options
        opt = ttk.LabelFrame(self, text="Processing Options")
        opt.pack(fill="x", **pad)
        ttk.Checkbutton(opt, text="Keep original file creation date (Uncheck for today's date)",
                        variable=self.keep_dates).grid(row=0, column=0, columnspan=6, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(opt, text="Overwrite existing JPEGs",
                        variable=self.overwrite).grid(row=1, column=0, columnspan=6, sticky="w", padx=8, pady=4)
        ttk.Label(opt, text="Target JPEG size:").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ttk.Spinbox(opt, from_=1, to=50, increment=0.5, width=6,
                    textvariable=self.min_mb).grid(row=2, column=1)
        ttk.Label(opt, text="to").grid(row=2, column=2, padx=4)
        ttk.Spinbox(opt, from_=1, to=60, increment=0.5, width=6,
                    textvariable=self.max_mb).grid(row=2, column=3)
        ttk.Label(opt, text="MB      Parallel workers:").grid(row=2, column=4, padx=4)
        ttk.Spinbox(opt, from_=1, to=max(1, os.cpu_count() or 1), width=4,
                    textvariable=self.workers).grid(row=2, column=5, sticky="w")

        # Output info
        info = ttk.LabelFrame(self, text="Output")
        info.pack(fill="x", **pad)
        self.info_lbl = ttk.Label(info, foreground="#555")
        self.info_lbl.pack(anchor="w", padx=8, pady=6)
        self.dst_var.trace_add("write", lambda *_: self._update_info())
        self._update_info()

        # Subfolder table
        hdr = ttk.Frame(self)
        hdr.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(hdr, text="Subfolders at Source (Select to Process):",
                  style="Bold.TLabel").pack(side="left")
        ttk.Button(hdr, text="Refresh", command=self.scan_source).pack(side="right")
        ttk.Button(hdr, text="Select None", command=lambda: self._select_all(False)).pack(side="right", padx=4)
        ttk.Button(hdr, text="Select All", command=lambda: self._select_all(True)).pack(side="right")

        tf = ttk.Frame(self)
        tf.pack(fill="both", expand=True, padx=10, pady=4)
        cols = ("sel", "name", "date", "count", "out")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=8)
        for c, text, w, anchor in [("sel", "", 36, "center"), ("name", "Folder Name", 420, "w"),
                                   ("date", "Creation Date", 120, "center"),
                                   ("count", "RAW Count", 90, "e"),
                                   ("out", "Output Folder", 300, "w")]:
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c in ("name", "out")))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", lambda e: [self._toggle(i) for i in self.tree.selection()])

        # Progress
        self.pbar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pbar.pack(fill="x", padx=12, pady=(8, 2))
        ttk.Label(self, textvariable=self.progress_txt).pack(anchor="w", padx=12)

        # Buttons
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=6)
        bf.columnconfigure((0, 1, 2), weight=1, uniform="b")
        self.process_btn = ttk.Button(bf, text="Process (0) Folders", command=self.start)
        self.process_btn.grid(row=0, column=0, sticky="ew", padx=4, ipady=6)
        self.pause_btn = ttk.Button(bf, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_btn.grid(row=0, column=1, sticky="ew", padx=4, ipady=6)
        self.open_btn = ttk.Button(bf, text="Open Target Folder", command=self.open_target)
        self.open_btn.grid(row=0, column=2, sticky="ew", padx=4, ipady=6)

        # Log
        ttk.Label(self, text="Processing Log:", style="Bold.TLabel").pack(anchor="w", padx=10)
        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.logbox = tk.Text(lf, height=10, bg="#0c0c0c", fg="#7CFC7C", insertbackground="#7CFC7C",
                              font=("Consolas", 10), wrap="none", state="disabled", relief="flat")
        self.logbox.tag_configure("warn", foreground="#FFD75E")
        self.logbox.tag_configure("fail", foreground="#FF6B6B")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lsb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    # ---------- helpers ----------
    def _update_info(self):
        dst = self.dst_var.get() or "<Target Folder>"
        self.info_lbl.config(text=f"JPEGs will be saved to:  {dst}\\<Folder Name>\\   "
                                  f"(full resolution, metadata + GPS preserved)")

    def log(self, msg, tag=None):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{datetime.now():%H:%M:%S} - {msg}\n", tag or ())
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def browse_src(self):
        d = filedialog.askdirectory(initialdir=self.src_var.get() or str(Path.home()))
        if d:
            self.src_var.set(d)
            self.scan_source()

    def browse_dst(self):
        d = filedialog.askdirectory(initialdir=self.dst_var.get() or str(Path.home()))
        if d:
            self.dst_var.set(d)
            self.scan_source()

    def scan_source(self):
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        src = Path(self.src_var.get())
        if not src.is_dir():
            self._refresh_count()
            return
        folders = []
        if list_raws(src):
            folders.append(src)  # RAWs directly in the source folder
        folders += sorted(p for p in src.iterdir() if p.is_dir() and list_raws(p))
        dst = self.dst_var.get()
        for f in folders:
            n = len(list_raws(f))
            st = f.stat()
            cdate = datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_ctime)).strftime("%Y-%m-%d")
            name = f.name if f != src else f"{f.name} (source root)"
            out = str(Path(dst) / f.name) if dst else "(set Target Folder)"
            self.tree.insert("", "end", iid=str(f), values=("☐", name, cdate, n, out))
        self.log(f"Scanned source: {len(folders)} folder(s) with RAW files.")
        self._refresh_count()

    def _on_tree_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_region(event.x, event.y) == "cell":
            self._toggle(row)

    def _toggle(self, iid, state=None):
        on = (iid not in self.checked) if state is None else state
        (self.checked.add if on else self.checked.discard)(iid)
        vals = list(self.tree.item(iid, "values"))
        vals[0] = "☑" if on else "☐"
        self.tree.item(iid, values=vals)
        self._refresh_count()

    def _select_all(self, on):
        for iid in self.tree.get_children():
            self._toggle(iid, on)

    def _refresh_count(self):
        if not self.worker:
            self.process_btn.config(text=f"Process ({len(self.checked)}) Folders",
                                    state="normal" if self.checked else "disabled")

    def open_target(self):
        d = self.dst_var.get()
        if d and Path(d).is_dir():
            if os.name == "nt":
                os.startfile(d)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])

    # ---------- processing ----------
    def start(self):
        if not self.exiftool:
            self.exiftool = find_exiftool()
            if not self.exiftool:
                messagebox.showerror(APP_NAME, "ExifTool not found.\n\nPut exiftool.exe next to "
                                               "this script or on your PATH, then try again.")
                return
        dst = self.dst_var.get().strip()
        if not dst:
            messagebox.showerror(APP_NAME, "Set a Target Folder first.")
            return
        try:
            lo, hi = float(self.min_mb.get()), float(self.max_mb.get())
            assert 0 < lo < hi
        except Exception:
            messagebox.showerror(APP_NAME, "Target size: min must be smaller than max.")
            return
        src = Path(self.src_var.get())
        jobs = []
        for iid in self.tree.get_children():
            if iid not in self.checked:
                continue
            folder = Path(iid)
            out_dir = Path(dst) / folder.name
            for r in list_raws(folder):
                jobs.append((folder.name, r, out_dir / (r.stem + ".jpg")))
        if not jobs:
            return
        self._save_settings()
        self.pause_evt.clear()
        self.stop_evt.clear()
        self.process_btn.config(state="disabled", text="Processing...")
        self.pause_btn.config(state="normal", text="Pause")
        self.pbar["value"] = 0
        self.log(f"--- Starting: {len(self.checked)} folder(s), {len(jobs)} RAW file(s), "
                 f"target {lo:g}-{hi:g} MB ---")
        args = (jobs, lo, hi, self.exiftool, self.keep_dates.get(), self.overwrite.get(),
                max(1, int(self.workers.get())))
        self.worker = threading.Thread(target=self._run, args=args, daemon=True)
        self.worker.start()

    def _run(self, jobs, lo, hi, exiftool, keep_dates, overwrite, n_workers):
        total, done = len(jobs), 0
        counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
        per_folder_left = {}
        for fname, _, _ in jobs:
            per_folder_left[fname] = per_folder_left.get(fname, 0) + 1
        t0 = time.time()
        pending = {}
        it = iter(jobs)
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as pool:
            while True:
                # keep the pool fed unless paused/stopped
                while (len(pending) < n_workers * 2 and not self.pause_evt.is_set()
                       and not self.stop_evt.is_set()):
                    nxt = next(it, None)
                    if nxt is None:
                        break
                    fname, raw, out = nxt
                    fut = pool.submit(convert_one, str(raw), str(out), lo, hi,
                                      exiftool, keep_dates, overwrite)
                    pending[fut] = fname
                if not pending:
                    if self.stop_evt.is_set():
                        break
                    if self.pause_evt.is_set():
                        time.sleep(0.2)
                        continue
                    break
                finished, _ = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
                for fut in finished:
                    fname = pending.pop(fut)
                    try:
                        status, msg = fut.result()
                    except Exception as e:
                        status, msg = "fail", f"worker crashed: {e}"
                    counts[status] += 1
                    done += 1
                    tag = {"warn": "warn", "fail": "fail"}.get(status)
                    self.msgq.put(("log", f"  [{fname}] {msg}", tag))
                    per_folder_left[fname] -= 1
                    if per_folder_left[fname] == 0:
                        self.msgq.put(("log", f"[{fname}] Conversion complete.", None))
                    elapsed = time.time() - t0
                    eta = elapsed / done * (total - done)
                    self.msgq.put(("progress", done, total, eta))
        stopped = self.stop_evt.is_set()
        self.msgq.put(("log", f"--- {'Stopped' if stopped else 'Workflow Complete'} --- "
                              f"{counts['ok']} ok, {counts['warn']} warnings, {counts['fail']} failed, "
                              f"{counts['skip']} skipped in {time.time() - t0:.0f}s", None))
        self.msgq.put(("done", stopped))

    def toggle_pause(self):
        if self.pause_evt.is_set():
            self.pause_evt.clear()
            self.pause_btn.config(text="Pause")
            self.log("Resumed.")
        else:
            self.pause_evt.set()
            self.pause_btn.config(text="Resume")
            self.log("Paused (images already in progress will finish).")

    def _drain_queue(self):
        try:
            while True:
                m = self.msgq.get_nowait()
                if m[0] == "log":
                    self.log(m[1], m[2])
                elif m[0] == "progress":
                    _, done, total, eta = m
                    pct = done / total * 100
                    self.pbar["value"] = pct
                    self.progress_txt.set(f"Progress: {pct:.0f}% ({done}/{total}, "
                                          f"~{int(eta // 60)}m {int(eta % 60)}s left)")
                elif m[0] == "done":
                    self.worker = None
                    self.pause_btn.config(state="disabled", text="Pause")
                    if not m[1]:
                        self.pbar["value"] = 100
                        self.progress_txt.set("Progress: 100% (Complete)")
                    self._refresh_count()
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    # ---------- settings ----------
    def _load_settings(self):
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return {}

    def _save_settings(self):
        try:
            SETTINGS_FILE.write_text(json.dumps({
                "source": self.src_var.get(), "target": self.dst_var.get(),
                "keep_dates": self.keep_dates.get(), "overwrite": self.overwrite.get(),
                "min_mb": float(self.min_mb.get()), "max_mb": float(self.max_mb.get()),
                "workers": int(self.workers.get())}, indent=2))
        except Exception:
            pass

    def on_close(self):
        if self.worker and not messagebox.askyesno(APP_NAME, "Processing is running. Stop and exit?"):
            return
        self.stop_evt.set()
        self.pause_evt.clear()
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    mp.freeze_support()
    App().mainloop()
