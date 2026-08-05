# Verifying the conversation-history branch locally

Three tiers, cheapest first. **Tier 0 and 1 need no credentials at all.**
Tier 2 is the only one that proves the Telegram transport and that the real
Anthropic API accepts what we store — everything below it uses stubs.

Every command was run on this branch before being written down; the expected
output is what it actually produced.

```bash
git checkout claude/persist-conversation-history-90CmU
make install          # uv sync --group dev
```

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

## Tier 2 — end to end over Telegram (~30 min)

This is the only tier that exercises the Telegram transport and a **real**
Anthropic call. Nothing below Tier 2 proves the API accepts our normalized
content, because every test above stubs the client.

**You need:** a *second* Telegram bot token (never the production one — talk to
@BotFather), an `ANTHROPIC_API_KEY`, and Docker. The Picnic API is mocked, so
no real order can be placed and no Picnic credentials are used.

```bash
cp .env.mock.example .env.mock
# edit .env.mock: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY,
#                 ALLOWED_TELEGRAM_USER_IDS=<your telegram user id>
```

The mock image is pulled from ghcr by default. To run *your* branch, build it:

```bash
docker build --target runtime -t picnic-local:verify .
# then in docker-compose.mock.yml point image: at picnic-local:verify
make mock-up && make mock-logs
```

**Startup check:** the log should show
`Database initialised. Conversation persistence: on`.

> The image runs `alembic upgrade head`? **No** — it is the documented deploy
> step, not automatic. `init_db()` in `_post_init` creates missing tables on a
> fresh volume, which is fine here. On a volume that predates this branch, run
> `docker compose -f docker-compose.mock.yml run --rm picnic-bot-mock \
> alembic upgrade head` first, and `alembic stamp head` if that DB was
> originally created by `init_db()`.

Then, in Telegram:

| # | Do this | Expect |
|---|---|---|
| 1 | `/start` | Help text listing `/forget` and `/privacy` |
| 2 | "we usually buy oat milk on Fridays" | A normal reply |
| 3 | "what did I just tell you?" | It remembers — in-session context works |
| 4 | `make mock-down && make mock-up` | Container restarts |
| 5 | "what did I tell you about Fridays?" | **It still remembers.** This is the feature |
| 6 | `/forecast` (forces tool use), then restart, then a follow-up | Coherent — a tool turn survived storage and replay. This is the case most likely to 400 if serialization were wrong |
| 7 | `/privacy` | Reports `ON` and explains the options |
| 8 | `/privacy off` | Confirms it stopped saving **and deleted** what was stored |
| 9 | Chat, then restart, then ask about it | Forgotten — nothing was written while off |
| 10 | `/privacy on`, chat, restart, ask | Remembers again |
| 11 | `/forget` | "deleted N stored message(s)"; the bot loses the thread |
| 12 | `/forget` again | "There was nothing stored." |

Confirm on disk between steps:
```bash
docker compose -f docker-compose.mock.yml exec picnic-bot-mock \
  python -c "import sqlite3;print(sqlite3.connect('data/picnic-mock.db')\
.execute('SELECT COUNT(*) FROM conversation_messages').fetchone())"
```

**Tear down:** `make mock-down` — or `docker compose -f docker-compose.mock.yml
down -v` to drop the volume too.

---

## What "verified" means after each tier

| Tier | Proves | Does **not** prove |
|---|---|---|
| 0 | Logic, migrations, no regressions | Anything about real I/O |
| 1 | Persistence, turn integrity, deletion, opt-out, log hygiene against a real SQLite file | Telegram, or that the Anthropic API accepts our messages |
| 2 | The whole path, including a real API round-trip | Behaviour against a *large* archive, or the real Picnic API |

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
