# StudyDash

**教材を撮る → AI がカードを作る → 忘れる直前に復習する。**
自分のパソコンの中だけで動く、日本語ファーストの学習ダッシュボード。

**Photograph your material → AI turns it into cards → review them right before you forget.**
A Japanese-first study dashboard that runs entirely on your own computer.

---

## 5分ではじめる / Get started in 5 minutes

### 1. 必要なもの / What you need

| | 必須 / Required | 理由 / Why |
|---|---|---|
| **Python 3.9+** | はい / yes | アプリ本体 / the app itself |
| **Claude Code** | いいえ / no | AI機能（写真解析・カード生成）だけに使う / only for the AI features |

Python は [python.org](https://www.python.org/downloads/) から。
**Windows の人はインストール時に「Add Python to PATH」に必ずチェックを入れてください。**
Windows users: tick **"Add Python to PATH"** during installation.

### 2. 起動 / Launch

| OS | ダブルクリックするファイル / Double-click |
|---|---|
| **Mac** | `Start-Mac.command` |
| **Windows** | `Start-Windows.bat` |

初回は自動でセットアップ（数分）してからブラウザが開きます。
2回目からはすぐ起動します。
The first run sets everything up (a few minutes), then your browser opens. Later runs are instant.

> **Mac で「開発元が未確認」と出たら / If macOS blocks the file:**
> ファイルを **右クリック → 開く** を選び、確認ダイアログで「開く」。初回だけです。
> Right-click the file → **Open**, then confirm. Only needed once.

### 3. 更新 / Update

| OS | ダブルクリックするファイル / Double-click |
|---|---|
| **Mac** | `Update-Mac.command` |
| **Windows** | `Update-Windows.bat` |

**あなたの学習データは絶対に消えません。** カード・成績・アップロードした写真は
git の管理外にあるので、更新は物理的にそれらへ触れません。更新前に自動バックアップも取ります。

**Your study data is never touched.** Cards, review history and uploaded files live
outside version control, so an update physically cannot reach them. A backup is
taken before every update anyway.

---

## AI について / About the AI

StudyDash は **あなた自身の Claude Code** を呼び出します。
アプリが API キーを持つことはなく、あなたの教材が私や第三者のサーバーに送られることもありません。

StudyDash shells out to **your own Claude Code** installation. The app holds no API
key, and your materials are never sent to me or to any third-party server.

1. [claude.com/claude-code](https://claude.com/claude-code) をインストール
2. ターミナル（Windows はコマンドプロンプト）で `claude login` を一度実行
3. StudyDash を起動 — 自動で見つけます

**AI がなくても StudyDash は動きます。** カードを手で作る・復習する・ノートを書く・
進捗を見る、これらは全部そのまま使えます。AI は「教材から自動でカードを作る」部分だけです。

**StudyDash works fine without AI.** Manual cards, review, notes and progress all work
as normal; the AI only automates *making* cards from your materials.

環境が正しいか確認したいときは / To check your environment:

```
python3 doctor.py
```

---

## できること / What it does

- **教材の取り込み** — 写真・PDF をアップロードすると AI が全文を書き起こし、
  カード候補を提案します。**提案は確認してから**キューに入るので、勝手に増えません。
  *Upload a photo or PDF; the AI transcribes it and proposes cards. Nothing enters your
  queue until you approve it.*
- **間隔反復（SM-2）** — 忘れる直前に出題。1日の新規枚数に上限があります。
  *Spaced repetition that shows a card right before you'd forget it.*
- **締め切りペース配分** — 教材に試験日を設定すると、**残り日数から1日の枚数を自動計算**します。
  *Set a target date on a material and the daily new-card count derives itself from the days left.*
- **多様な学び方** — 一問一答・用語・穴埋め・解法ステップ・自己説明・多肢選択・画像オクルージョン。
  *Q&A, terms, cloze, worked steps, self-explanation, multiple choice, image occlusion.*
- **科学的な復習戦略** — 想起練習・インターリーブ・出題形式の切替・精緻化。
  *Retrieval practice, interleaving, recognition mode, elaboration.*
- **日英の切り替え** — カード単位でも、デッキ全体でも。教材ごとの用語表で訳語がぶれません。
  *Flip any card — or the whole deck — between Japanese and English. A per-material
  glossary keeps terminology consistent across every AI call.*
- **ノート** — アウトライナー（RemNote 風）。ノートとカードは地続きです。
  *An outliner where notes and flashcards are the same substance.*
- **課題トラッカー** — 締め切りと優先順位。Google Classroom 連携は任意。
  *Assignment tracking with priorities. Google Classroom sync optional.*

---

## データはどこにあるか / Where your data lives

すべてこのフォルダの中です。クラウドはありません。

| ファイル / File | 中身 / Contents |
|---|---|
| `study.db` | カード・成績・ノート・課題 / everything you've studied |
| `uploads/` | アップロードした写真と PDF / your uploaded files |
| `backups/` | 自動バックアップ / automatic snapshots |

**バックアップ = このフォルダをコピーするだけ。** アンインストール = フォルダを削除するだけ。
To back up, copy this folder. To uninstall, delete it.

---

## 困ったとき / Troubleshooting

| 症状 / Symptom | 対処 / Fix |
|---|---|
| ブラウザが開かない | 手動で `http://127.0.0.1:5000` を開く / open it manually |
| 「Python が見つかりません」 | python.org から入れ直す（Windows は PATH のチェックを忘れずに） |
| HEIC がアップロードできない | Mac 以外では未対応。JPG で保存し直す / not supported off macOS — re-save as JPG |
| AI が動かない | `claude login` を実行 / run `claude login` |
| 何かがおかしい | 左下の **？** からレポートを作って送る / hit **?** and send the report |

### 不具合・要望を送る / Reporting a problem

画面左下の **？** を押し、何が起きたかを書いて「レポートを作る」。
`feedback/report-….md` が保存されるので、**そのファイルを開発者に送ってください。**
開発者はそれをそのまま Claude Code に渡して原因を調べられます。

Hit **?** at the bottom-left, describe what happened, and a report is saved into
`feedback/`. Send that file back — it is written so it can be handed to Claude Code
as-is for diagnosis.

**自動送信は一切ありません。** レポートに入るのはバージョン・OS・件数までで、
**カードやノートの中身は入りません。** 教材のエラーやログを含めるかは自分で選べます
（含めても、ユーザー名やファイル名は伏せ字になります）。

*Nothing is sent automatically — this app has no server to send to. Reports carry
version, OS and counts, never your card or note content. Including the last error
or the log tail is opt-in, and home directories and file names are scrubbed.*

---

## 技術メモ / Technical notes

Python 3.9 · Flask · SQLite (WAL) · バニラ JS — **ビルド不要・npm 不要・CDN 不要**。
フロントエンドは完全にオフラインで動きます。依存は `requirements.txt` の 6 つだけ。

Python 3.9 · Flask · SQLite (WAL) · vanilla JS — **no build step, no npm, no CDN.**
The frontend is fully offline-capable; the only dependencies are the six in `requirements.txt`.

```
make test     # テスト / run the test suite
make lint     # ruff
python3 pack.py   # 配布用 zip を作る / build a distributable zip
```

---

## ライセンスと配布 / License and distribution

現在は**招待制**です。作者が直接渡した相手だけが使えます。再配布はしないでください。
Currently **invite-only** — please don't redistribute the pack.

© 2026 StudyDash. All rights reserved.
