@echo off
REM Double-click to launch the PVA RAW to JPEG Converter.
REM First run installs the Python packages it needs.
python -c "import rawpy, PIL, numpy" 2>nul || python -m pip install rawpy pillow numpy
start "" pythonw "%~dp0PVA_RAW_Converter.py"
