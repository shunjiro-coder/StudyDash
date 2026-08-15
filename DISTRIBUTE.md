# 配る手順 / Distribution playbook

**これは自分用のメモです。配布パックには入りません**（`.gitattributes` の
`export-ignore` で除外）。 *Owner-only; excluded from the pack.*

---

## 0. いまある道具 / What exists

| もの | 何のため |
|---|---|
| `python3 pack.py` | 配布用 zip を作る（`dist/StudyDash-v<版>.zip`） |
| `python3 pack.py --check` | 自分のデータが混ざっていないか監査だけする |
| `tools/lock.html`（PromptForge 側） | ファイルに合言葉で鍵をかける |
| `site/index.html` | 公開用の紹介ページ（コードは非公開のまま） |
| アプリ内の **？** ボタン | 相手からの不具合報告を受け取る口 |

---

## 1. パックを作る / Build the pack

```
python3 pack.py
```

安全性は**仕組みで**担保しています。zip は `git archive` が作るので、
**git が追跡しているファイルしか入りません。** `study.db`・`uploads/`・
`backups/`・`credentials.json` は `.gitignore` にあるため追跡されず、
したがって物理的に入りようがない。作った zip を再監査して、
もし入っていたら zip ごと削除します。

> 迷ったら `python3 pack.py --check` だけ先に流す。

**先にコミットすること。** `git archive HEAD` なので、未コミットの変更は入りません。

---

## 2. 渡し方を選ぶ / Choose how to hand it over

### A. 信頼する人だけ・非公開リポジトリ（推奨）
1. GitHub で **Private** リポジトリを作る
2. `git remote add origin <URL>` → `git push -u origin remnote-phase-b1`
3. Settings → Collaborators で相手を招待（相手も無料アカウントが要る）
4. 相手は `git clone` して `Start-Mac.command` / `Start-Windows.bat`

これだと**更新が一番ラク**です（相手は `Update-*` を押すだけ）。
知らない人はリポジトリの存在すら見えません（404）。

### B. どこに置いてもいい・合言葉で鍵をかける
GitHub アカウントを持たない相手や、置き場所が公開でも構わない場合。

1. `python3 pack.py` で zip を作る
2. PromptForge の `make lock`（または鍵アプリを開く）
3. zip を入れて、**長めの合言葉**（単語1つではなく文章）を入力
4. できた `StudyDash-v…-locked.html` をどこにでも置く
5. **合言葉は別の経路で**伝える（リンクと同じ場所に書かない）

中身は AES-256-GCM で暗号化されているので、偶然見つけた人には
ただの暗号文です。ただし——

- 合言葉を渡した人は、**他人にも渡せます**（個別の取り消しは不可）
- 合言葉を忘れると**誰にも開けません**（復旧手段なし）
- 更新のたびに鍵をかけ直す必要があります（A より手間）

---

## 3. 相手に送る文面 / What to tell them

> StudyDash を渡します。教材を撮ると AI がカードを作って、忘れる直前に出してくれる勉強アプリです。
>
> **入れ方**：フォルダを解凍して、Mac なら `Start-Mac.command`、Windows なら
> `Start-Windows.bat` をダブルクリック。初回だけ数分かかります。
> （Mac で「開発元が未確認」と出たら、右クリック → 開く）
>
> **AI について**：AI の部分はあなた自身の Claude Code を使います。
> 無くても、カードを手で作る・復習する・ノートを書くところは全部動きます。
>
> **データ**：全部あなたのパソコンの中だけです。どこにも送信されません。
>
> **困ったら**：画面左下の **？** を押して、何が起きたか書いて「レポートを作る」。
> `feedback/` にできた `.md` ファイルを私に送ってください。

---

## 4. 更新を届ける / Shipping an update

1. 直す → `make test` → コミット
2. `VERSION` を上げる（これを忘れると相手が「同じ版です」と言われる）
3. `CHANGELOG.md` に日本語で1行足す（相手が読むのはここだけ）
4. `git push`
5. 相手は `Update-Mac.command` / `Update-Windows.bat` をダブルクリック

更新は**相手の学習データに触れません**。`study.db` と `uploads/` は git の
管理外だからで、加えて更新前に自動でバックアップも取ります。
相手がファイルをいじっていた場合は、上書きせず中止します。

---

## 5. 報告が返ってきたら / When a report comes back

相手から `report-….md` が届いたら、**そのまま Claude Code に渡せます。**
レポートには版・OS・Python・claude の有無・適用済みマイグレーション・件数が
入っています（カードやノートの中身は入りません）。

```
「このレポートの原因を調べて直して」＋ ファイルを貼る
```

同じ報告が複数から来たら、それが次に直すものです。

---

## 6. あとで課金・広告にする場合 / If this ever earns money

いまの作りは**そこを塞いでいません**が、素直には繋がりません。
実際に必要になるのは：

- **多人数対応** — いまは単一利用者前提（`study.db` が1つ、認証なし）。
  各利用者のデータを分ける層が要ります。
- **AI の費用** — いまは各自の Claude Code なので**あなたの負担はゼロ**。
  ホスティング型にすると AI 費用があなたに来ます。ここが一番効きます。
- **お金の導線** — 決済は親名義の口座を通る必要があります（未成年のため）。
  技術より先に、ここが実際の制約です。

→ 当面は「ポートフォリオ＋招待制」のままが、費用ゼロで一番強い。
