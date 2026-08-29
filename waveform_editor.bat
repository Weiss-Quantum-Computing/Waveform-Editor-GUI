@echo off
rem Launch the editor with pythonw, so no console window sits behind it.
rem
rem The XP machine will not have Python on PATH as often as not, so the usual
rem install locations are tried in order before giving up. Add one here if the
rem interpreter lives somewhere else.

cd /d "%~dp0"

if exist "C:\Python27\pythonw.exe" (
    start "" "C:\Python27\pythonw.exe" waveform_editor_gui.py
    goto :eof
)
if exist "C:\Python27\python.exe" (
    start "" "C:\Python27\python.exe" waveform_editor_gui.py
    goto :eof
)
if exist "%LOCALAPPDATA%\Programs\Python\Python27\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Programs\Python\Python27\pythonw.exe" waveform_editor_gui.py
    goto :eof
)

pythonw waveform_editor_gui.py
if errorlevel 1 (
    echo.
    echo Could not start pythonw. Edit this file and point it at the Python
    echo 2.7 install on this machine.
    pause
)
