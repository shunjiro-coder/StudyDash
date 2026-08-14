#!/bin/bash
# StudyDash — ダブルクリックで起動 / double-click to start.
# 初回はセットアップも自動でやります。 First run also does the setup.
cd "$(dirname "$0")" || exit 1

PY=./venv/bin/python

echo ""
echo "  StudyDash を起動します / starting StudyDash"
echo ""

# --- 0. Python があるか / is Python present at all
if ! command -v python3 >/dev/null 2>&1; then
  echo "  Python3 が見つかりません。 https://www.python.org/downloads/ から入れてください。"
  echo "  Python3 not found. Install it from https://www.python.org/downloads/"
  echo ""
  read -r -p "  Enter を押すと閉じます / press Enter to close " _
  exit 1
fi

# --- 1. 初回セットアップ / first-run setup (idempotent)
if [ ! -x "$PY" ]; then
  echo "  初回セットアップ中… 数分かかります。 / First-time setup, this takes a few minutes."
  python3 -m venv venv || { echo "  venv の作成に失敗 / venv creation failed"; read -r _; exit 1; }
  "$PY" -m pip install --upgrade pip >/dev/null
  "$PY" -m pip install -r requirements.txt || {
    echo "  ライブラリの導入に失敗しました / dependency install failed"; read -r _; exit 1; }
  echo "  セットアップ完了 / setup complete"
  echo ""
fi

# --- 2. 環境チェック / environment check (never blocks unless fatal)
"$PY" doctor.py || {
  echo "  上の問題を直してからもう一度ダブルクリックしてください。"
  echo "  Fix the problem above, then double-click again."
  read -r -p "  Enter を押すと閉じます / press Enter to close " _
  exit 1
}

# --- 3. 起動 / launch
"$PY" app.py &
SERVER_PID=$!

# ポートが開くまで待つ（固定 sleep だと遅い機種で空振りする）
# Wait for the port instead of sleeping a fixed time — a slow machine would miss it.
for _ in $(seq 1 40); do
  if curl -s -o /dev/null -m 1 http://127.0.0.1:5000/ ; then break; fi
  sleep 0.5
done

open "http://127.0.0.1:5000"
echo ""
echo "  開きました → http://127.0.0.1:5000"
echo "  このウィンドウを閉じると StudyDash も止まります。"
echo "  Closing this window stops StudyDash."
echo ""
wait $SERVER_PID
