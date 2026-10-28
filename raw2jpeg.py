#!/usr/bin/env python3
"""
raw2jpeg.py - Peak Valley Aerials RAW -> JPEG converter

- Keeps full resolution / aspect ratio (no resizing)
- Picks the JPEG quality per image so the file lands in a target size window
  (default 7-10 MB, aiming for ~8.5 MB)
- Copies ALL metadata from the RAW (EXIF GPS, DJI XMP gimbal/RTK/altitude tags)
  via ExifTool, then verifies GPS made it into the JPEG

Usage:
    python raw2jpeg.py <file-or-folder> [more files/folders...] [options]

Options:
    --out DIR        Output folder (default: "jpeg" subfolder next to the RAWs)
    --min MB         Minimum target size in MB (default 7)
    --max MB         Maximum target size in MB (default 10)
    --workers N      Parallel conversions (default: CPU cores - 1)
    --overwrite      Replace existing JPEGs instead of skipping them

Requires: pip install rawpy pillow numpy   +   exiftool on PATH
"""

import argparse
import io
import os
import shutil
import subprocess
import sys
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq"}
MB = 1024 * 1024
METADATA_HEADROOM = 256 * 1024  # room for EXIF/XMP added after encoding


def find_exiftool():
    for name in ("exiftool", "exiftool.exe", "exiftool(-k).exe"):
        p = shutil.which(name)
        if p:
            return p
    # also look next to this script
    here = Path(__file__).resolve().parent
    for name in ("exiftool.exe", "exiftool(-k).exe", "exiftool"):
        if (here / name).exists():
            return str(here / name)
    return None


def collect_raws(inputs):
    files = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.suffix.lower() in RAW_EXTS)
        elif p.is_file() and p.suffix.lower() in RAW_EXTS:
            files.append(p)
        else:
            print(f"  skip (not a RAW file/folder): {item}")
    return files


def develop(raw_path):
    """Demosaic the RAW to an 8-bit RGB array at full resolution."""
    import rawpy
    with rawpy.imread(str(raw_path)) as raw:
        rgb = raw.postprocess(
            use_camera_wb=True,      # white balance as shot
            output_bps=8,
            no_auto_bright=False,    # LibRaw's standard exposure normalization
            half_size=False,         # full resolution
        )
    return rgb


def encode(img, quality):
    # 4:4:4 chroma at every quality keeps file size rising smoothly with q.
    # optimize=True shaves a few % but libjpeg can choke on extreme detail at
    # very high q, so fall back to a plain encode if it does.
    for optimize in (True, False):
        buf = io.BytesIO()
        try:
            img.save(buf, "JPEG", quality=quality, subsampling=0, optimize=optimize)
            return buf.getvalue()
        except OSError:
            if not optimize:
                raise


def encode_to_target(img, lo_bytes, hi_bytes):
    """Binary-search JPEG quality so size lands in [lo, hi], closest to the middle."""
    hi_bytes -= METADATA_HEADROOM
    target = (lo_bytes + hi_bytes) / 2
    cache = {}

    def size_at(q):
        if q not in cache:
            cache[q] = encode(img, q)
        return len(cache[q])

    # Detail too low to ever reach the minimum: best possible is q=100
    if size_at(100) < lo_bytes:
        return cache[100], 100, "below-min"
    lo_q, hi_q = 40, 100
    best_q = None
    while lo_q <= hi_q:
        mid = (lo_q + hi_q) // 2
        s = size_at(mid)
        if s > hi_bytes:
            hi_q = mid - 1
        else:
            best_q = mid      # fits under max; try higher quality
            lo_q = mid + 1
    if best_q is None:
        # Even q=40 too big (enormous sensor) -> use q=40 anyway and flag it
        return cache.get(40) or encode(img, 40), 40, "above-max"
    # among qualities that fit, prefer the one nearest the middle of the window
    fitting = [q for q in cache if lo_bytes <= len(cache[q]) <= hi_bytes]
    if fitting:
        q = min(fitting, key=lambda q: abs(len(cache[q]) - target))
        return cache[q], q, "ok"
    return cache[best_q], best_q, "ok" if len(cache[best_q]) >= lo_bytes else "below-min"


def copy_metadata(exiftool, src, dst):
    """Copy every tag from the RAW into the JPEG, minus embedded previews and orientation."""
    cmd = [
        exiftool, "-q", "-q", "-overwrite_original", "-m",
        "-TagsFromFile", str(src), "-all:all", "-unsafe",
        "--PreviewImage", "--ThumbnailImage", "--JpgFromRaw", "--OtherImage",
        "-Orientation=1", "-n",   # rawpy already rotated the pixels
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def read_gps(exiftool, path):
    r = subprocess.run([exiftool, "-s3", "-n", "-GPSLatitude", "-GPSLongitude", "-GPSAltitude", str(path)],
                       capture_output=True, text=True)
    vals = [v.strip() for v in r.stdout.splitlines() if v.strip()]
    return vals


def convert_one(raw_path, out_dir, lo_mb, hi_mb, exiftool, overwrite):
    from PIL import Image
    raw_path = Path(raw_path)
    out_path = Path(out_dir) / (raw_path.stem + ".jpg")
    if out_path.exists() and not overwrite:
        return f"skip  {raw_path.name} (JPEG exists)"

    rgb = develop(raw_path)
    img = Image.fromarray(rgb)
    data, q, flag = encode_to_target(img, int(lo_mb * MB), int(hi_mb * MB))
    out_path.write_bytes(data)
    copy_metadata(exiftool, raw_path, out_path)

    src_gps = read_gps(exiftool, raw_path)
    dst_gps = read_gps(exiftool, out_path)
    size_mb = out_path.stat().st_size / MB
    w, h = img.size

    if src_gps and not dst_gps:
        gps_note = "GPS MISSING!"
    elif dst_gps:
        gps_note = "GPS ok"
    else:
        gps_note = "no GPS in source"
    note = {"ok": "", "below-min": "  (max quality still under min size - low-detail image)",
            "above-max": "  (over max even at q40)"}[flag]
    return f"done  {raw_path.name} -> {out_path.name}  {w}x{h}  q{q}  {size_mb:.2f} MB  {gps_note}{note}"


def main():
    ap = argparse.ArgumentParser(description="RAW -> JPEG at full res, sized to a target window, metadata preserved")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--min", type=float, default=7.0)
    ap.add_argument("--max", type=float, default=10.0)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()

    exiftool = find_exiftool()
    if not exiftool:
        sys.exit("ExifTool not found. Put exiftool.exe on PATH or next to this script.")

    raws = collect_raws(a.inputs)
    if not raws:
        sys.exit("No RAW files found.")

    print(f"{len(raws)} RAW file(s), target {a.min}-{a.max} MB, {a.workers} worker(s)\n")
    jobs = []
    with ProcessPoolExecutor(max_workers=a.workers, mp_context=mp.get_context("spawn")) as pool:
        for r in raws:
            out_dir = Path(a.out) if a.out else r.parent / "jpeg"
            out_dir.mkdir(parents=True, exist_ok=True)
            jobs.append(pool.submit(convert_one, r, out_dir, a.min, a.max, exiftool, a.overwrite))
        failures = 0
        for j in as_completed(jobs):
            try:
                print(j.result())
            except Exception as e:
                failures += 1
                print(f"FAIL  {e}")
    print(f"\nFinished. {len(raws) - failures} ok, {failures} failed.")


if __name__ == "__main__":
    main()
