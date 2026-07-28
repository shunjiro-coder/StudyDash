#!/bin/bash
# StudyDash launcher — double-click to run. No terminal commands needed.
cd "$(dirname "$0")" || exit 1

# first run: create venv + install pinned deps
if [ ! -x venv/bin/python ]; then
  echo "初回セットアップ中…（数分かかります）"
  python3 -m venv venv
  ./venv/bin/python -m pip install --upgrade pip
  ./venv/bin/python -m pip install -r requirements.txt
fi

./venv/bin/python app.py &
SERVER_PID=$!
sleep 2
open "http://127.0.0.1:5000"
echo "StudyDash 起動中。 このウィンドウを閉じると停止します。"
wait $SERVER_PID
