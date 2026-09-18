@echo off
REM ===========================================================================
REM  Single entry point. ALL logic lives in scripts\launch.ps1.
REM
REM  Why this file contains ASCII ONLY, and why it must stay CRLF:
REM     cmd.exe parses a .bat by reading its bytes through the ACTIVE CODE PAGE.
REM     Non-ASCII (e.g. UTF-8 Chinese) gets mis-decoded, desynchronises cmd's
REM     file position, and splits commands into fragments -- e.g. "timeout"
REM     becomes "meout", "if not exist" becomes "exist", and you get a pile of
REM     '...is not recognized as an internal or external command' errors.
REM     LF-only line endings break the same parser the same way (measured:
REM     CRLF -> 8/8 steps run; LF -> 4/8 and commands shredded).
REM  PowerShell is immune to both, and prints Chinese via the Unicode console
REM  API regardless of the code page -- so Chinese messages live in launch.ps1.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" set "PS_EXE=powershell"

set "SCRIPT=%~dp0scripts\launch.ps1"
if not exist "%SCRIPT%" (
    echo [FATAL] scripts\launch.ps1 not found.
    echo         Expected at: %SCRIPT%
    echo         The repository looks incomplete. Re-pull it and retry.
    pause
    exit /b 1
)

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [FATAL] launcher exited with code %RC% -- see messages above.
    pause
)
exit /b %RC%
