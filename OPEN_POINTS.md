# Open points — conversation history

Everything the conversation-history feature set out to do is done and merged.
This is the residue: findings from the architecture and security reviews that
were **deliberately not fixed**, plus smaller cleanups noticed along the way.

Nothing here is a known-broken user-facing behaviour. The four defects that
were (C1 truncation, C3 unfinished turns, H1 log leak, M1 turn-id race) are
fixed, reproduced, and regression-tested — see `PLAN_CONVERSATION_HISTORY.md`.

Sizes: **XS** minutes · **S** under an hour · **M** half a day.

---

## 1. Worth doing next

### 1.1 Commit `uv.lock` — XS, supply chain
`.gitignore:11` ignores it and it is untracked. `Dockerfile:7,19` runs
`uv sync --frozen ... || uv sync ...`, and `COPY pyproject.toml uv.lock* ./`
globs to nothing, so `--frozen` fails and the fallback resolves fresh from
PyPI. Every image build and CI run pulls whatever PyPI serves that day; a
compromised transitive dependency would land with no diff and no reproducible
build to compare against.
**Fix:** untrack from `.gitignore`, commit the lock, drop the `||` fallback so
a missing lock fails loudly.

### 1.2 Bound the tool-use loop — S, cost/availability
`bot.py` runs `while True` with no iteration cap and no request timeout. The
whole message list is re-sent each round, so cost grows quadratically, and a
model stuck in a search→refine loop burns Anthropic credits and Picnic quota
until the process is killed. Since turns are now archived, one runaway turn
can also write megabytes.
**Fix:** `for _ in range(MAX_TOOL_ROUNDS)` (~10) plus `timeout=` on
`messages.create`. Decide what to tell the user when the cap is hit.

### 1.3 Let a de-authorized user erase their own data — S, privacy
`cmd_forget` / `cmd_privacy` check `_is_allowed` before deleting. Removing
someone from `ALLOWED_TELEGRAM_USER_IDS` permanently strips their ability to
`/forget`, while their transcript stays on disk forever (retention is "keep
everything"). There is also no operator-side path to delete one chat.
**Fix:** either let these two commands run for anyone — deleting your own data
is not a privileged operation — or add an admin entry point
(`python -m picnic_meal_planner.admin forget <chat_id>`) and document
offboarding.

### 1.4 Move the privacy caveats where operators will read them — XS, docs
They live only in `PLAN_CONVERSATION_HISTORY.md`, which nobody opens after a
feature ships. Three facts belong in `ARCHITECTURE.md` beside the conversation
model, and one line beside `PERSIST_CONVERSATIONS` in `.env.example`:

- the DB is unencrypted, and now holds dietary/household detail plus full
  order history with prices;
- retention is unbounded by default;
- `DELETE` frees pages but does not scrub them — data survives until `VACUUM`.

Related: `/forget` replies *"deleted N stored message(s)"*, which overstates
unrecoverability. Either soften the wording or `VACUUM` after a full clear.

---

## 2. Correctness, not currently reachable

### 2.1 A corrupt row can wedge a chat (review ref: C2) — S
`queries.py::_rows_to_messages` skips a row whose JSON will not parse. Skipping
one from the *middle* of a turn orphans a `tool_result`, and `trim_history`
repairs the head only — so the chat 400s on every request with no self-heal,
and the generic "Sorry, something went wrong" gives no hint that `/forget` is
the cure. Content is always written with `json.dumps`, so reaching this needs
corruption or manual editing, not user input.
**Fix:** drop the whole `turn_id` on a parse failure (the column is right
there). Optionally catch `anthropic.BadRequestError` in `_chat`, invalidate the
cache, retry once from empty, and log loudly — that turns *any* future
sequence bug into one bad turn instead of a permanent outage.

### 2.2 `MAX_HISTORY_TURNS` is read at import, unvalidated — XS
`history.py` reads it at import while `PERSIST_CONVERSATIONS` is read lazily.
The current `load_dotenv()` ordering in `bot.py` is correct, but a future
`from . import history` placed above it would silently pin the default while
the kill-switch kept working. A typo raises a bare `ValueError` at import;
`MAX_HISTORY_TURNS=0` silently degrades to one message per request.
**Fix:** make both lazy; clamp and validate.

---

## 3. Performance

### 3.1 Swap the index (review ref: m1) — S, needs migration `0003`
`EXPLAIN QUERY PLAN` on all five conversation queries: **none** use
`idx_conversation_messages_time`. `delete_turns_in_range` filters on
`HAVING MIN(created_at)` after grouping, which that index cannot serve, so it
is pure write amplification on an append-only table. Meanwhile
`append_turn`'s `MAX(turn_id) WHERE chat_id=?` scans the chat's whole index
range on every append, growing linearly forever under "keep everything".
**Fix:** drop `..._time`, add `(chat_id, turn_id)`.

### 3.2 Cap and re-encode `tool_result` — S, changes model-visible behaviour
`bot.py` stores `str(result)` — a Python `repr`, unbounded. Measured against
the repo's own seed fixture: `get_order_history` → **10.4 KB**,
`forecast_order` → 2.8 KB. The disk cost is survivable (~73 MB/chat/year); the
sharper cost is tokens, since a 10 KB result stays in the 40-message window and
is re-uploaded on every request for ~10 turns.
**Fix:** `json.dumps(result)[:N]` with an explicit `…truncated` marker — JSON
is smaller than `repr` and the model parses it more reliably. Test the
truncation marker doesn't confuse the model before shipping.

### 3.3 Prompt caching — S
`SYSTEM_PROMPT` plus 15 tool schemas are byte-identical on every request and
sit at the front of the prefix; nothing sets `cache_control`. Given 3.2's
payloads riding in the window, a top-level `cache_control: ephemeral` is close
to free.

### 3.4 `list_turns` reads full bodies for 120-char previews — XS
Builds an `IN (...)` over one id per turn (SQLite caps at 32,766 binds) and
pulls each first message's entire `content` before truncating in Python.
**Fix:** `func.substr(content, 1, 200)` and a join on `MIN(id)`.

---

## 4. Small cleanups

| | Item | Size |
|---|---|---|
| 4.1 | `trim_history` returning `[]` is silent total context loss — add a `logger.warning` | XS |
| 4.2 | `chat_id` is `Integer`; Telegram supergroup ids exceed int32. `BigInteger` documents intent and survives a Postgres move | XS |
| 4.3 | `get_db` is defined identically in both `queries.py` and `engine.py`; the comment claims a re-export. Make `queries.py` do `from .engine import get_db` | XS |
| 4.4 | `bot.py` calls `history._persistence_enabled_globally()` — private across a module boundary. Expose `history.persistence_status()` | XS |
| 4.5 | `role` is an unconstrained `String`; `CHECK (role IN ('user','assistant'))` catches corruption at write time | XS |
| 4.6 | Migration `0002` drops indexes before the table; `DROP TABLE` already removes them | XS |
| 4.7 | The model is hardcoded to `claude-sonnet-4-6` in `bot.py`. Make it an env var, and consider a newer Sonnet | XS |
| 4.8 | Container runs as root; the DB is `root:root 0644` in the volume. Add a non-root `USER` and `read_only: true` *(relayed from review; not independently verified)* | S |

---

## 5. Explicitly not doing

- **Encrypting the DB at rest.** Full-disk or filesystem encryption on the VPS
  is the proportionate control for a single-family self-hosted bot; SQLCipher
  is not worth the operational cost here.
- **Multi-replica correctness.** The in-memory cache is per-process and
  `invalidate()` is local. Documented in `history.py`; revisit only if the
  deployment stops being a single container.
- **Exposing partial deletion over Telegram.** `delete_turns`, `delete_day`,
  and `list_turns` exist and are tested; no UI until someone wants one.

---

## Do not "fix" these — they look wrong and are not

1. `trim_history` scans **forward** from the cut point, dropping more than
   strictly necessary. That is the safe direction.
2. `delete_turns_in_range` resolves the range to whole turns *before* deleting,
   rather than deleting rows by timestamp. That is what stops a turn straddling
   midnight from being split in half and orphaning a `tool_result`.
3. `clear_history` clears the DB **before** memory, while `set_persistence`
   updates memory **after** commit. Both orderings mean a failure leaves the
   two sides consistent.
4. `/privacy off` deliberately keeps the in-progress conversation in memory and
   says so in its reply. Wiping it mid-chat would read as a bug.
5. `get_context` returns the cached list; `bot.py` immediately copies it via
   `trim_history`, which always returns a new list. Assigning it directly would
   corrupt the cache every turn.
6. `is_persistence_enabled` returns `False` on error **without caching it**, so
   a transient DB fault cannot latch a chat into the wrong state.
