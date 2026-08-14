#!/bin/bash
# StudyDash 更新 — ダブルクリックで最新版に / double-click to update.
# 学習データ（study.db・uploads）は git の管理外なので絶対に消えません。
# Your study data is git-ignored, so an update can never touch it.
cd "$(dirname "$0")" || exit 1

PY=./venv/bin/python

echo ""
echo "  StudyDash を更新します / updating StudyDash"
echo ""

if [ ! -d .git ]; then
  echo "  この配布物は git 版ではないので自動更新できません。"
  echo "  This copy is not a git checkout, so it cannot self-update."
  echo "  新しい zip を受け取って入れ替えてください / replace it with the new zip."
  read -r -p "  Enter を押すと閉じます / press Enter to close " _
  exit 1
fi

BEFORE=$(cat VERSION 2>/dev/null || echo "?")

# 念のためのバックアップ（更新は data を触らないが、安全側に倒す）
# Belt-and-braces snapshot: updates don't touch data, but cheap insurance.
if [ -x "$PY" ] && [ -f study.db ]; then
  "$PY" -c "import backup; print('  backup:', backup.create_backup())" 2>/dev/null || true
fi

# 自分のローカル変更は守る / never clobber local edits
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "  変更されたファイルがあるため中止しました（上書き事故を防ぐため）。"
  echo "  Local changes present — aborting so nothing of yours is overwritten."
  git status --short
  read -r -p "  Enter を押すと閉じます / press Enter to close " _
  exit 1
fi

echo "  最新版を取得中… / fetching…"
if ! git pull --ff-only; then
  echo ""
  echo "  更新に失敗しました（ネットワークか権限）。 / update failed (network or access)."
  read -r -p "  Enter を押すと閉じます / press Enter to close " _
  exit 1
fi

# 依存が増えていても壊れないように / keep deps in sync
"$PY" -m pip install -q -r requirements.txt 2>/dev/null || true

AFTER=$(cat VERSION 2>/dev/null || echo "?")
echo ""
if [ "$BEFORE" = "$AFTER" ]; then
  echo "  すでに最新です（v$AFTER） / already up to date (v$AFTER)"
else
  echo "  更新しました: v$BEFORE  ->  v$AFTER"
  echo "  Updated: v$BEFORE -> v$AFTER"
  echo "  変更点は CHANGELOG.md を見てください / see CHANGELOG.md for what changed"
fi
echo ""
echo "  次に Start-Mac.command をダブルクリックしてください。"
echo "  Now double-click Start-Mac.command."
echo ""
read -r -p "  Enter を押すと閉じます / press Enter to close " _
