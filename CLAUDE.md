# StudyDash — instructions for Claude Code

## What this project is

A local-first, Japanese-first **study dashboard**: upload a photo or PDF, AI
proposes flashcards, you approve them, and spaced repetition schedules them.
Runs only on `127.0.0.1`. Single user. Owner is a student.

**This is NOT the owner's other projects.** If the request is about:

| Request is about | It belongs to | Path |
|---|---|---|
| prompt entries, roots, the Lexicon, `/forge` | **PromptForge** | `~/Desktop/Everything/Projects/PromptForge` |
| niche hunting, signals, candidates, market evidence | **NicheForge** | `~/Desktop/Everything/Projects/NicheForge` |

If a request sounds like a sibling project's, **say so and ask before acting.**
Do not implement a PromptForge idea inside StudyDash because the words overlap
("prompts" here means `prompts/*.txt`, the AI templates — not Lexicon entries).

## Before you start — you may not be the only session

The owner runs several Claude Code sessions in parallel, one per project. That is
fine *across* repos and dangerous *within* one: uncommitted work is what gets
destroyed.

1. Run `git status` and `git log --oneline -3` **first**.
2. If there are uncommitted changes you did not make, **STOP and ask the owner**
   whether another session owns them. Never `git checkout` / `reset` / `stash`
   someone else's work away.
3. If `git log` shows commits you did not write, another session is active here —
   tell the owner before continuing.
4. **Commit as soon as work is verified.** Do not sit on a large uncommitted diff.
5. Never force-push. Never amend a commit you did not write.

## Non-negotiable guardrails

- **Stack is frozen:** Python 3.9 / Flask (single blueprint) / SQLite WAL with a
  process-wide write lock / vanilla JS+CSS / background `worker.py`.
  **No React, no Node, no build step, ZERO new pip dependencies.** The frontend
  must work offline — no CDN, no npm.
- **Schema changes are additive and idempotent only** — `_ensure_column` or
  `CREATE TABLE IF NOT EXISTS`, registered in `db.MIGRATIONS`. Never drop,
  rename, or alter a column. Back up before a phase's first migration.
- **D-4:** every `*_at` value is ISO8601 UTC via `db.now_utc_iso()`.
- **D-5:** `content_hash = sha256(NFKC(front) + \x1f + NFKC(back))`.
  `media_json`, `translation_json` and `fsrs_json` are **excluded** from it.
  Changing this formula is forbidden — it is how cards keep their identity.
- **All SQLite writes** go through `db.write` / `write_returning` / `write_many`.
- **AI is the local `claude` CLI only** (sonnet for images, haiku for text).
  Never add an API key or a hosted call.
- Existing endpoint success contracts stay unchanged; response keys may only be
  **added**.
- Japanese-first bilingual UI. 曖昧な点は推測せず、必ず質問すること。
- **Commit only when the owner asks.** Trailer:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

## Verify before you claim done

```
./venv/bin/python -m pytest -q       # currently 352 tests
./venv/bin/ruff check .
/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc \
  -e 'new Function(readFile("static/app.js"))'
```

Server restart (needed for any `.py` or template change; static JS/CSS serve
fresh with ⌘⇧R):

```
lsof -nP -iTCP@127.0.0.1:5000 -sTCP:LISTEN -t | xargs kill
nohup ./venv/bin/python app.py > studydash.log 2>&1 & disown
```

## Distribution

`DISTRIBUTE.md` is the owner's playbook (excluded from the pack itself).
`python3 pack.py` builds the shareable zip from `git archive`, so **only tracked
files ship** — that is the structural guarantee that the owner's `study.db`,
`uploads/` and credentials cannot leak. It also refuses to ship any file whose
contents contain a home-directory path.
