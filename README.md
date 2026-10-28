# RAW to JPEG — PVA RAW Converter

Desktop tool (Windows, Python 3.9+) that converts DJI/camera RAW files (DNG, CR2, NEF, ARW…) to
**full-resolution JPEGs sized to a target window (default 7–10 MB)** while copying **all metadata**,
including GPS and DJI XMP tags (gimbal, relative altitude, RTK). Each output is checked for GPS.

## Features
- Same resolution and aspect ratio as the RAW
- JPEG quality picked per image (binary search) to land in the size window
- Folder-of-folders batch processing (one row per flight folder), pause/resume, progress, log
- Keeps the RAW's file dates (optional)

## Run
- `PVA_RAW_Converter.bat` — double-click for the GUI (first run installs `rawpy pillow numpy`)
- `raw2jpeg.py` — command line: `python raw2jpeg.py <files-or-folders> [--min 7 --max 10]`

## ExifTool (required)
Download the Windows 64-bit zip from https://exiftool.org, rename `exiftool(-k).exe` to
`exiftool.exe`, and keep it **and** the `exiftool_files` folder next to the scripts.

## Notes
The RAW development is a plain LibRaw conversion (camera white balance, no sharpening or lens
correction) — it won't match Lightroom/Camera Raw edits.
