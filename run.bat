@echo off
REM Double-click: run the default daily push (pull latest WiGLE upload,
REM push to WDGWars). Both keys must already be saved (setup.bat).

if "%~1"=="" (
    python "%~dp0wigle_to_wdgwars.py" --from-wigle --chunk-size 10000
) else (
    python "%~dp0wigle_to_wdgwars.py" %*
)
echo.
pause
