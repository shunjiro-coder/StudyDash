# 変更履歴 / Changelog

新しいものが上。更新後にここを見れば何が変わったか分かります。
Newest first — read this after an update to see what changed.

---

## v1.2.0 — 2026-08-16

### 追加 / Added
- **FSRS を選べるようになりました** — 復習タブの「出題間隔の決め方」で SM-2 と
  FSRS を切り替えられます。FSRS は「その記憶がいまどれくらい持つか」を推定して
  間隔を決めるので、得意な札は早く卒業し、苦手な札は手元に残ります。
  *Pick your scheduler in the review tab. FSRS models how long each memory
  currently lasts, so easy cards leave faster and lapsing cards stop being
  flung into the far future.*
- **SM-2 のままでも今までどおり。** 既定は SM-2 で、切り替えても SM-2 の記録は
  消えないので、いつでも元に戻せます。
  *SM-2 stays the default and its state is preserved, so the switch is reversible.*

---

## v1.1.0 — 2026-08-14

### 追加 / Added
- **困ったとき・要望を伝える** — 画面左下の **？** ボタンから、うまく動かないこと・
  分かりにくいこと・こうしてほしいことを報告できます。押すと `feedback/` に
  レポート（.md）が保存されるので、それを開発者に送ってください。
  *A **?** button in the footer writes a problem report to `feedback/` — send that
  file back to the maintainer, who can hand it straight to Claude Code to diagnose.*
- **何も送信されません。** レポートは自動送信されず、必ず自分で送る形です。
  含まれるのはバージョン・OS・件数まで。**カードやノートの中身は入りません。**
  教材名やログを含めるかどうかは、チェックボックスで自分で選べます。
  *Nothing is transmitted anywhere. Reports carry version, OS and counts — never
  card or note content. Including the last error or the log tail is opt-in, and
  home directories and file names are scrubbed from both.*

---

## v1.0.0 — 2026-08-14

**初回配布版。 / First distributable release.**

### 追加 / Added
- **配布パック** — Mac は `Start-Mac.command`、Windows は `Start-Windows.bat` を
  ダブルクリックするだけ。初回セットアップも自動。
  *One double-click to install and run on either platform.*
- **更新スクリプト** — `Update-Mac.command` / `Update-Windows.bat`。学習データには
  触れず、更新前に自動バックアップ。ローカル変更があるときは中止して上書き事故を防ぎます。
  *Update scripts that back up first, never touch your data, and abort rather than
  overwrite local changes.*
- **環境チェック（`doctor.py`）** — Python・Flask・Claude・画像変換・データの有無を
  日英で診断。起動時に自動で走ります。
  *A bilingual environment check that runs automatically at launch.*
- **バージョン表示** — 画面左下に表示。不具合報告のときに使います。
  *Version number in the footer, for bug reports.*
- **Windows 対応** — AI 呼び出しのプロセス管理と `claude.cmd` の実行経路を
  Windows でも動くように修正。
  *Windows support for the AI subprocess path.*

### 改善 / Improved
- **AI の出力品質** — カード・クイズ・要約・翻訳・用語表の指示を全面的に見直し。
  - 多肢選択の誤答が**実際にありがちな勘違い**になり、正解だけ長い・形で当てられる、が解消
  - 穴埋めは**空所1つだけ**、答えが一意に決まる文脈を残す
  - 教材にない事実・数値を作らない（断定度も教材のまま保つ）
  - 出力前の自己チェックを全テンプレートに追加
  *Overhauled every AI prompt: misconception-based distractors, single-blank cloze,
  no invented facts, hedge preservation, and a self-check before emitting.*
- **用語表** — 教材ごとの日英対訳表を保存し、全ての AI 呼び出しに注入。
  「鋳型」と「テンプレート」が混ざるようなブレがなくなります。
  *A per-material glossary is now persisted and injected into every AI call, so
  terminology no longer drifts between separate calls.*

### 注意 / Known limitations
- **HEIC は macOS でのみ変換できます。** Windows では JPG/PNG で保存し直してください
  （JPG・PNG・PDF は全環境で問題なく動きます）。
  *HEIC conversion is macOS-only; re-save as JPG elsewhere.*
- **Windows は実機未検証です。** 動かない箇所があれば `python doctor.py` の結果と
  一緒に教えてください。
  *Windows has not been tested on real hardware yet — please report issues.*
