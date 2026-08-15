@echo off
REM StudyDash 更新 / double-click to update.
REM 学習データ（study.db・uploads）は git 管理外なので絶対に消えません。
REM Your study data is git-ignored, so an update can never touch it.
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

set "PY=venv\Scripts\python.exe"

echo.
echo   StudyDash を更新します / updating StudyDash
echo.

if not exist ".git" (
  echo   この配布物は git 版ではないので自動更新できません。
  echo   This copy is not a git checkout, so it cannot self-update.
  echo   新しい zip を受け取って入れ替えてください / replace it with the new zip.
  pause
  exit /b 1
)

where git >nul 2>&1
if errorlevel 1 (
  echo   git が見つかりません。 https://git-scm.com/download/win から入れてください。
  echo   git not found. Install it from https://git-scm.com/download/win
  pause
  exit /b 1
)

set "BEFORE=?"
if exist VERSION set /p BEFORE=<VERSION

if exist "%PY%" if exist study.db (
  "%PY%" -c "import backup; print('  backup:', backup.create_backup())" 2>nul
)

REM 自分のローカル変更は守る / never clobber local edits
git diff --quiet
if errorlevel 1 goto :dirty
git diff --cached --quiet
if errorlevel 1 goto :dirty

echo   最新版を取得中… / fetching…
git pull --ff-only
if errorlevel 1 (
  echo.
  echo   更新に失敗しました（ネットワークか権限）。 / update failed (network or access).
  pause
  exit /b 1
)

if exist "%PY%" "%PY%" -m pip install -q -r requirements.txt 2>nul

set "AFTER=?"
if exist VERSION set /p AFTER=<VERSION
echo.
if "%BEFORE%"=="%AFTER%" (
  echo   すでに最新です（v%AFTER%） / already up to date (v%AFTER%)
) else (
  echo   更新しました: v%BEFORE%  -^>  v%AFTER%
  echo   Updated: v%BEFORE% -^> v%AFTER%
  echo   変更点は CHANGELOG.md を見てください / see CHANGELOG.md
)
echo.
echo   次に Start-Windows.bat をダブルクリックしてください。
echo   Now double-click Start-Windows.bat.
echo.
pause
exit /b 0

:dirty
echo   変更されたファイルがあるため中止しました（上書き事故を防ぐため）。
echo   Local changes present — aborting so nothing of yours is overwritten.
git status --short
pause
exit /b 1
