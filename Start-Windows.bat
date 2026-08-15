@echo off
REM StudyDash - ダブルクリックで起動 / double-click to start.
REM chcp 65001 = UTF-8. Without it the Japanese below prints as mojibake.
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

set "PY=venv\Scripts\python.exe"

echo.
echo   StudyDash を起動します / starting StudyDash
echo.

REM --- 0. Python があるか / is Python present
where python >nul 2>&1
if errorlevel 1 (
  echo   Python が見つかりません。 https://www.python.org/downloads/ から入れてください。
  echo   Python not found. Install it from https://www.python.org/downloads/
  echo   ※インストール時に "Add Python to PATH" に必ずチェックを入れてください。
  echo   IMPORTANT: tick "Add Python to PATH" during install.
  echo.
  pause
  exit /b 1
)

REM --- 1. 初回セットアップ / first-run setup (idempotent)
if not exist "%PY%" (
  echo   初回セットアップ中… 数分かかります。 / First-time setup, this takes a few minutes.
  python -m venv venv
  if errorlevel 1 (
    echo   venv の作成に失敗 / venv creation failed
    pause
    exit /b 1
  )
  "%PY%" -m pip install --upgrade pip >nul
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo   ライブラリの導入に失敗しました / dependency install failed
    pause
    exit /b 1
  )
  echo   セットアップ完了 / setup complete
  echo.
)

REM --- 2. 環境チェック / environment check
"%PY%" doctor.py
if errorlevel 1 (
  echo   上の問題を直してからもう一度ダブルクリックしてください。
  echo   Fix the problem above, then double-click again.
  pause
  exit /b 1
)

REM --- 3. 起動 / launch.
REM ブラウザは「ポートが開いたら」開く。サーバ本体はこのウィンドウで前面実行する
REM （ログが見えるし、ウィンドウを閉じれば確実に止まる）。
REM Browser opens once the port answers; the server runs in the foreground here so
REM its log is visible and closing the window reliably stops it.
start "" /b powershell -NoProfile -Command ^
  "for($i=0;$i -lt 40;$i++){try{(New-Object Net.Sockets.TcpClient).Connect('127.0.0.1',5000);Start-Process 'http://127.0.0.1:5000';break}catch{Start-Sleep -Milliseconds 500}}"

echo   起動中… ブラウザが自動で開きます → http://127.0.0.1:5000
echo   Starting… your browser will open at http://127.0.0.1:5000
echo   このウィンドウを閉じると StudyDash も止まります。
echo   Closing this window stops StudyDash.
echo.
"%PY%" app.py
echo.
echo   StudyDash を終了しました / StudyDash has stopped.
pause
