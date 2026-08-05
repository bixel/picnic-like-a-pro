# Verifying the conversation-history branch locally

Cheapest first. **Tiers 0 and 1 need no credentials and no Docker.** Tier 2
runs the real bot locally with plain Python — the first tier that proves the
Telegram transport and that the Anthropic API accepts what we store, since
everything below it uses stubs. Tier 3 is Docker, and is optional: it proves
packaging, not behaviour.

Every command was run on this branch before being written down; the expected
output is what it actually produced. The exceptions are the steps that need
outbound Telegram/Anthropic access or a Docker daemon — neither was available
where this was written, and those are called out where they appear.

```bash
git checkout claude/persist-conversation-history-90CmU
make install          # uv sync --group dev
```

`uv.lock` is committed, so this installs the exact versions CI uses. If you
have a pre-existing `.venv` from before that change, `uv sync --frozen --group
dev` will reconcile it.

---

## Tier 0 — the suite and the migration (~2 min)

```bash
make test-fast
```
**Expect:** `438 passed`. If you want coverage instead, `make test`.

The 11 tests added by the review-fix commit are the ones that matter most.
Run them alone to see what they assert:

```bash
uv run pytest tests/test_history.py -q \
  -k "window or concurrent or leak or head"
uv run pytest tests/test_bot_conversation.py::TestRunClaudeUnfinishedTurns -v
```

**Prove they are real regression tests** — they must fail against the code as
it was before the fixes:

```bash
git stash -u                       # if you have local edits
git checkout HEAD~1 -- src/        # old source, new tests
uv run pytest tests/test_history.py tests/test_bot_conversation.py -q
#   -> expect 10 failed
git checkout HEAD -- src/          # put it back
uv run pytest -q                   # -> 438 passed
```

Migration round-trip on a scratch DB (never touches `data/picnic.db`):

```bash
export DB_PATH=/tmp/verify.db && rm -f $DB_PATH
uv run alembic upgrade head        # -> runs 0001 then 0002
uv run alembic downgrade -1        # -> 0002 -> 0001, no error
uv run alembic upgrade head
uv run alembic current             # -> 0002 (head)
uv run alembic revision --autogenerate -m drift
```
**Expect:** the generated file's `upgrade()` body is just `pass` — models and
migration agree. **Delete it afterwards:** `rm alembic/versions/*drift*.py`.

---

## Tier 1 — the feature, without Telegram or Anthropic (~10 min)

Still on the scratch DB. This drives `history.py` directly, which is where all
the policy lives.

```bash
export DB_PATH=/tmp/verify.db && rm -f $DB_PATH
uv run alembic upgrade head
```

### 1a. It survives a restart — the whole point

```bash
uv run python - <<'EOF'
import asyncio
from picnic_meal_planner import history as H

TURN = [{"role": "user", "content": "we need milk"},
        {"role": "assistant", "content": [{"type": "text", "text": "Adding milk."}]}]

async def main():
    await H.commit_turn(42, list(TURN), 0)
    H._reset_caches()                       # <- simulates a process restart
    print("after restart:", await H.get_context(42))
    print("turns        :", await H.list_turns(42))
    print("forget removed:", await H.clear_history(42))
    print("after forget :", await H.get_context(42))

asyncio.run(main())
EOF
```
**Expect:** the turn comes back after the reset; `list_turns` shows
`message_count: 2` with preview `we need milk`; `clear_history` returns `2`;
the context is then `[]`.

### 1b. A tool turn is never cut in half (fix C1)

```bash
uv run python - <<'EOF'
import asyncio
from picnic_meal_planner import history as H

TOOL_TURN = [
  {"role":"user","content":"what are we low on?"},
  {"role":"assistant","content":[{"type":"tool_use","id":"t","name":"forecast_order","input":{}}]},
  {"role":"user","content":[{"type":"tool_result","tool_use_id":"t","content":"milk"}]},
  {"role":"assistant","content":[{"type":"text","text":"Milk."}]},
]

async def main():
    await H.clear_history(1); H._reset_caches()
    await H.commit_turn(1, list(TOOL_TURN), 0)
    for size in range(1, 6):                 # windows that fall *inside* the turn
        H.MAX_HISTORY_MESSAGES = size; H._reset_caches()
        w = await H.get_context(1)
        ok = (not w) or H.is_turn_start(w[0])
        print(f"window={size} len={len(w)} head_valid={ok}")
        assert ok, "window opened mid-turn -> the API would 400"
    print("OK: never opens mid-turn")

asyncio.run(main())
EOF
```
**Expect:** `head_valid=True` at every size, and lengths of only `0` or `4` —
the turn is loaded whole or dropped, never truncated.

### 1c. Opt-out and the kill-switch

```bash
uv run python - <<'EOF'
import asyncio, os
from picnic_meal_planner import history as H
T = [{"role":"user","content":"secret"},{"role":"assistant","content":"ok"}]

async def main():
    await H.clear_history(9); H._reset_caches()
    await H.commit_turn(9, list(T), 0)
    print("stored          :", len(await H.list_turns(9)))       # 1
    await H.set_persistence(9, False)
    print("after opt-out   :", await H.list_turns(9))            # [] - deleted
    print("live context kept:", len(await H.get_context(9)))     # 2 - still there
    await H.commit_turn(9, list(T), 0)
    print("writes while off:", await H.list_turns(9))            # [] - nothing new
    H._reset_caches()
    print("after restart   :", await H.get_context(9))           # [] - not loaded

asyncio.run(main())
EOF
```
**Expect:** opting out deletes what was stored but keeps the in-progress
conversation in memory — that is deliberate, so the chat doesn't lose its
thread mid-sentence. Nothing is written while off, and nothing loads after a
restart.

Global kill-switch:
```bash
PERSIST_CONVERSATIONS=false uv run python -c "
import asyncio
from picnic_meal_planner import history as H
print('enabled:', asyncio.run(H.is_persistence_enabled(1)))"     # -> False
```

### 1d. A failed write must not log the conversation (fix H1)

```bash
uv run python - <<'EOF'
import asyncio, logging, io
from sqlalchemy import text
from picnic_meal_planner import history as H
from picnic_meal_planner.db import engine as eng

SECRET = "peanut allergy, alarm code 1234"
buf = io.StringIO(); logging.basicConfig(stream=buf, level=logging.DEBUG, force=True)

async def main():
    await eng.init_db()
    async with eng.get_engine().begin() as c:      # make every INSERT fail
        await c.execute(text("DROP TABLE conversation_messages"))
        await c.execute(text("CREATE TABLE conversation_messages (id INTEGER PRIMARY KEY,"
            " chat_id INTEGER, turn_id INTEGER, role TEXT, content TEXT, created_at TEXT,"
            " CHECK (role='nope'))"))
    await H.commit_turn(7, [{"role":"user","content":SECRET}], 0)

asyncio.run(main())
log = buf.getvalue()
print("secret leaked  :", SECRET in log)             # must be False
print("failure logged :", "Could not persist turn" in log)   # must be True
EOF
```
**Expect:** `secret leaked: False`, `failure logged: True`. Note this captures
at DEBUG, which is stricter than production (the app runs at INFO).

### 1e. Inspect the database directly

```bash
uv run python -c "
import sqlite3, os
c = sqlite3.connect(os.environ['DB_PATH'])
for r in c.execute('SELECT id,turn_id,role,substr(content,1,60) FROM conversation_messages ORDER BY id'):
    print(r)
print('settings:', c.execute('SELECT * FROM chat_settings').fetchall())
"
```
Messages of one exchange share a `turn_id` — that grouping is what makes
deletion safe. (Uses Python's stdlib, so no `sqlite3` CLI needed.)

**Clean up:** `rm -f /tmp/verify.db && unset DB_PATH`

---

## Tier 2 — the real bot, run locally with plain Python (~30 min)

No Docker. This runs the same `uv run bot` entry point the container runs, just
directly on your machine, so a "restart" is Ctrl-C and up-arrow.

This is the first tier that exercises the Telegram transport and a **real**
Anthropic call. Nothing below it proves the API accepts our normalized content,
because every test above stubs the client.

**You need:** a *second* Telegram bot token (never the production one — talk to
@BotFather) and an `ANTHROPIC_API_KEY`. The Picnic API is mocked, so no Picnic
credentials are used and no real order can be placed.

### Configure

Export everything rather than editing `.env`. `load_dotenv()` runs with
`override=False`, so exported variables win — but anything you *don't* export
is still filled in from a `.env` in the repo root, and that includes
`TELEGRAM_BOT_TOKEN`.

> **Export the token explicitly.** If you have a production `.env`, an
> unexported token means this local process starts polling with your
> **production** bot. Two pollers on one token fight over updates and Telegram
> will error — and your family would be talking to this test process.

```bash
export TELEGRAM_BOT_TOKEN='<your TEST bot token>'
export ANTHROPIC_API_KEY='sk-ant-...'
export ALLOWED_TELEGRAM_USER_IDS='<your telegram user id>'   # or ALLOW_ALL_USERS=true
export PICNIC_MOCK=true
export DB_PATH="$PWD/data/verify.db"
export PERSIST_CONVERSATIONS=true
```

### Create the schema, then start

```bash
rm -f "$DB_PATH"
uv run alembic upgrade head     # 0001, then 0002
uv run bot
```

Run `alembic upgrade head` yourself rather than relying on startup. `bot.py`'s
`_post_init` does call `init_db()`, but PTB runs `post_init` *after* it has
bootstrapped a Telegram connection — so on a fresh DB no tables exist until
Telegram is reachable, and doing it explicitly also matches the production
deploy step.

**Startup looks like:**
```
... - picnic_meal_planner.bot - INFO - Database initialised. Conversation persistence: on
... - picnic_meal_planner.bot - INFO - Starting bot with long polling...
```

If instead you get `Network Retry Loop (Bootstrap Initialize Application)` with
an httpx/proxy error, that is Telegram connectivity — a bad token, or a network
that blocks `api.telegram.org`. It is not a problem with this branch.

### If you get `AuthenticationError: 401 — API key is invalid`

The bot is fine — Telegram connected, the DB initialised, and the request
reached Anthropic, which rejected the key. Nothing was stored (a failed turn is
never persisted), so the database is untouched.

`bot.py` reads `os.environ["ANTHROPIC_API_KEY"]`, which would raise `KeyError`
if it were unset. So *something* supplied a value Anthropic doesn't accept.
Three placeholders in this repo are the usual culprits:

| File | Value |
|---|---|
| `.env.example` | `sk-ant-...` |
| `.env.mock.example` | `REPLACE_WITH_API_KEY` |
| `.env.test` | `sk-ant-test-key-not-real` |

If you copied one of those to `.env` and did not export a real key, `load_dotenv()`
supplies the placeholder. Find out which source wins:

```bash
uv run python - <<'EOF'
import os
from dotenv import dotenv_values, find_dotenv, load_dotenv

PLACEHOLDERS = {"sk-ant-...", "REPLACE_WITH_API_KEY", "sk-ant-test-key-not-real"}

def shape(v):
    if v is None:
        return "<unset>"
    clean = v.strip().strip('"').strip("'")
    notes = []
    if v != clean:            notes.append("HAS SURROUNDING WHITESPACE/QUOTES")
    if clean in PLACEHOLDERS: notes.append("IS A PLACEHOLDER FROM THE REPO")
    if "\n" in v or "\r" in v: notes.append("CONTAINS A NEWLINE")
    if not clean.startswith("sk-ant-"):
        notes.append(f"DOES NOT START WITH sk-ant- (starts {clean[:7]!r})")
    return (f"len={len(v)} {clean[:11]}...{clean[-4:] if len(clean) > 15 else ''}"
            + ("  <-- " + "; ".join(notes) if notes else "  (looks well-formed)"))

print("1. exported in your shell :", shape(os.environ.get("ANTHROPIC_API_KEY")))
path = find_dotenv(usecwd=True)
print("2. dotenv file found      :", path or "<none>")
if path:
    print("   value inside it        :", shape(dotenv_values(path).get("ANTHROPIC_API_KEY")))
load_dotenv()          # exactly what bot.py does; override=False
print("3. what bot.py will use   :", shape(os.environ.get("ANTHROPIC_API_KEY")))
EOF
```

It prints only the length and the first/last few characters, never the key.
Line 3 is what the bot uses; a shell export wins over the dotenv file, so if
line 1 is `<unset>` the value in line 2 is the one being sent.

Then confirm the key independently of the bot:

```bash
uv run python -c "
import anthropic, os
try:
    anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY']).messages.create(
        model='claude-sonnet-4-6', max_tokens=8,
        messages=[{'role':'user','content':'hi'}])
    print('KEY WORKS')
except anthropic.AuthenticationError:
    print('AUTH FAILED (401) — reached Anthropic and was rejected')
"
```

If that says `AUTH FAILED`, the key itself is the problem, not this project.
Common causes beyond the placeholders: a trailing newline from copy-paste, a
Console *session* token rather than an API key, or a key from a different
workspace. Mint a fresh one at console.anthropic.com and
`export ANTHROPIC_API_KEY='sk-ant-...'` in the shell you run `uv run bot` from.

### Walk through it in Telegram

**Restart** below means: `Ctrl-C` in the terminal, then `uv run bot` again.

| # | Do this | Expect |
|---|---|---|
| 1 | `/start` | Help text listing `/forget` and `/privacy` |
| 2 | "we usually buy oat milk on Fridays" | A normal reply |
| 3 | "what did I just tell you?" | It remembers — in-session context works |
| 4 | **Restart** | Comes back up |
| 5 | "what did I tell you about Fridays?" | **It still remembers.** This is the feature |
| 6 | `/forecast` (forces tool use), **restart**, then a follow-up | Coherent. A tool turn survived storage and replay — the case most likely to 400 if serialization were wrong |
| 7 | `/privacy` | Reports `ON` and explains the options |
| 8 | `/privacy off` | Confirms it stopped saving **and deleted** what was stored |
| 9 | Chat, **restart**, ask about it | Forgotten — nothing was written while off |
| 10 | `/privacy on`, chat, **restart**, ask | Remembers again |
| 11 | `/forget` | "deleted N stored message(s)"; the bot loses the thread |
| 12 | `/forget` again | "There was nothing stored." |

### Watch the database while it runs

In a second terminal, `export DB_PATH=...` to the same path and run this after
each step:

```bash
uv run python -c "
import sqlite3, os
c = sqlite3.connect(os.environ['DB_PATH'])
print('messages:', c.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0])
print('turns   :', c.execute('SELECT COUNT(DISTINCT turn_id) FROM conversation_messages').fetchone()[0])
print('settings:', c.execute('SELECT * FROM chat_settings').fetchall())
"
```

SQLite handles the concurrent read fine while the bot is running. Wrap it in
`watch -n2 '...'` if you want it live — using single quotes outside, since the
snippet already contains double quotes (and note `watch` is not installed by
default on macOS).

Step 6 is the interesting one: a `/forecast` turn should add **4+** messages
under a *single* `turn_id`, not 2. That grouping is what makes `/forget` and
turn-level deletion safe.

**Clean up:** `rm -f "$DB_PATH"`, and unset the exports (or just close the
shell) so you don't leave `PICNIC_MOCK=true` lying around.

---

## Tier 3 (optional) — the same thing in Docker

Only worth doing if you want parity with CI and production images; it proves
packaging, not behaviour. Tier 2 already covered the feature.

```bash
docker build --target runtime -t picnic-local:verify .
cp .env.mock.example .env.mock     # fill in the TEST token + Anthropic key
# point `image:` in docker-compose.mock.yml at picnic-local:verify
make mock-up && make mock-logs
```

Here `_post_init`'s `init_db()` does create the tables on a fresh volume. On a
volume that predates this branch, run
`docker compose -f docker-compose.mock.yml run --rm picnic-bot-mock alembic
upgrade head` first — and `alembic stamp head` before that if the DB was
originally created by `init_db()` rather than Alembic.

**Tear down:** `make mock-down`, or add `-v` to drop the volume.

---

## What "verified" means after each tier

| Tier | Proves | Does **not** prove |
|---|---|---|
| 0 | Logic, migrations, no regressions | Anything about real I/O |
| 1 | Persistence, turn integrity, deletion, opt-out, log hygiene against a real SQLite file | Telegram, or that the Anthropic API accepts our messages |
| 2 | The whole path, including a real API round-trip | Behaviour against a *large* archive, or the real Picnic API |
| 3 | The image builds and runs the same way | Nothing about the feature that tier 2 didn't |

---

## Before deploying to production

1. `uv run alembic upgrade head` **before** restarting the bot — this branch
   adds migration `0002`. On a DB originally created by `init_db()`, run
   `uv run alembic stamp head` first so Alembic doesn't try to recreate tables.
2. Existing chats start empty; history accrues from the first message after the
   upgrade. Nothing is back-filled.
3. Conversations are now **stored unencrypted** on the data volume. If that is
   not acceptable, set `PERSIST_CONVERSATIONS=false` — the bot keeps working
   exactly as before, in memory only.
4. Skim `OPEN_POINTS.md` §1 — particularly the unbounded tool loop, which is
   a cost risk rather than a correctness one.
