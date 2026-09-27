# Migrating to OpenRouter

This release sends every LLM request through [OpenRouter](https://openrouter.ai)
instead of calling the Anthropic API directly. Any model OpenRouter lists can
now be used, and **each chat can run on its own model**.

This guide lists what **you** have to do. The code changes are already in the
branch. Your stored conversations need no conversion: history stays in the same
format, and it is translated for whichever model a chat uses.

**TL;DR**

1. Create an OpenRouter key and load credits.
2. In the VPS `.env`, replace `ANTHROPIC_API_KEY` with `OPENROUTER_API_KEY` and add `LLM_MODEL`.
3. Back up the DB, then run the migration **before** starting the new image. It adds `chat_settings.model`.
4. Check it with `/model` and a normal chat message.

---

## 1. OpenRouter account (one-time)

- [ ] Sign up at <https://openrouter.ai> and **buy credits** at
      <https://openrouter.ai/settings/credits>. An account with no credits
      answers with HTTP 402 and the bot replies "Sorry, something went wrong".
- [ ] Create an API key at <https://openrouter.ai/settings/keys>. Give it
      a **credit limit** (for example $10/month). The tool-use loop is not
      bounded yet (OPEN_POINTS 1.2), so the limit is your cost cap.
- [ ] **Privacy (recommended).** Family grocery conversations will go to whichever
      provider serves the model. Under <https://openrouter.ai/settings/privacy>,
      turn off endpoints that may train on your prompts. You can also
      restrict routing to zero-data-retention endpoints.
- [ ] *Optional, BYOK:* to keep Anthropic billing and rate limits, add your
      existing Anthropic key under **Settings → Integrations** in OpenRouter.
      Requests still go through OpenRouter, but Anthropic bills you.
      Check OpenRouter's current BYOK fee.
- [ ] *Optional:* once the switch has run cleanly for a few days, revoke the old
      Anthropic key at console.anthropic.com.

## 2. Environment variables

On the VPS, edit `~/picnic-bot/.env`:

```diff
-ANTHROPIC_API_KEY=sk-ant-...
+OPENROUTER_API_KEY=sk-or-v1-...
+LLM_MODEL=anthropic/claude-sonnet-4.6
```

`LLM_MODEL` is optional. `anthropic/claude-sonnet-4.6` is already the default
and is the same model the bot used before, so behaviour is unchanged until you
choose another model.

Optional settings:

| Variable | Default | Use |
|---|---|---|
| `LLM_MAX_TOKENS` | `4096` | Output cap per request (same as before) |
| `LLM_ALLOWED_MODELS` | *(empty = any)* | Comma-separated allowlist. Every per-chat model must be in it. Set it before you let users pick models |
| `OPENROUTER_APP_NAME` | `Picnic Like a Pro` | The name shown on your OpenRouter activity page |
| `OPENROUTER_APP_URL` | — | Optional `HTTP-Referer` attribution header |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Change only if you route through a proxy/gateway |

Repeat the key swap anywhere else you keep an env file:

- [ ] `.env.mock` (mock/demo bot): `OPENROUTER_API_KEY=...`
- [ ] Your local `.env` / shell exports, if you run the bot locally
      (`export OPENROUTER_API_KEY=...`)

**No GitHub secrets change.** CI never calls the LLM, and the deploy workflow
reads the key from the VPS `.env`.

If `ANTHROPIC_API_KEY` is still set and `OPENROUTER_API_KEY` is not, the
preflight script tells you so.

## 3. Database migration (required)

Migration `0003` adds one nullable column, `chat_settings.model`. It is purely
additive. Existing rows get `NULL`, which means "use `LLM_MODEL`".

> **Do not skip this.** `init_db()` at startup creates missing *tables* but
> cannot add a column to an existing one. Without the migration, reading chat
> settings fails. Persistence then fails closed, so **conversations silently
> stop being saved**, and every chat falls back to the default model. The bot
> logs `Database schema is behind the code (missing chat_settings.model)` at
> startup if this happens.

The deploy workflow only runs `docker compose pull && up -d`. It does **not**
migrate, so do this by hand once, on the VPS, in this order:

```bash
cd ~/picnic-bot
docker compose pull                      # fetch the new image first
docker compose stop picnic-bot           # no writes during the migration

# 3a. Back up the database (inside the volume)
docker compose run --rm picnic-bot python -c "import sqlite3; sqlite3.connect('data/picnic.db').backup(sqlite3.connect('data/picnic.pre-openrouter.db'))"

# 3b. Find out which state the DB is in
docker compose run --rm picnic-bot python -c "import sqlite3; c=sqlite3.connect('data/picnic.db'); t={r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")}; print('alembic_version:', c.execute('select version_num from alembic_version').fetchall() if 'alembic_version' in t else 'NONE'); print('has conversation tables:', {'conversation_messages','chat_settings'} <= t)"
```

Then apply the matching case:

| `3b` printed | Meaning | Run |
|---|---|---|
| `alembic_version: [('0002',)]` (or `0001`) | Already under Alembic | `docker compose run --rm picnic-bot alembic upgrade head` |
| `alembic_version: NONE`, conversation tables **True** | Created by `init_db()` with the conversation-history code | `docker compose run --rm picnic-bot alembic stamp 0002` then `... alembic upgrade head` |
| `alembic_version: NONE`, conversation tables **False** | Created by `init_db()` before conversation history | `docker compose run --rm picnic-bot alembic stamp 0001` then `... alembic upgrade head` |

> **Don't `alembic stamp head`** on an unmanaged DB this time. `stamp head`
> would record `0003` as applied without adding the column. The older advice in
> CLAUDE.md applies only when the code and the DB were created at the same
> revision.

Then start the bot:

```bash
docker compose up -d
docker compose logs --tail=30 picnic-bot
```

You should see this line, and **no** `schema is behind` error:

```
Default LLM model (via OpenRouter): anthropic/claude-sonnet-4.6
```

*Recommended follow-up:* add
`docker compose run --rm picnic-bot alembic upgrade head` before
`docker compose up -d` in the `script:` of `.github/workflows/deploy.yml`.
Future schema changes will then apply on every deploy. Do it only after the
one-time stamp above; an unstamped DB would make that step fail.

## 4. Verify

- [ ] Optional, before or after deploying:
      `docker compose run --rm picnic-bot python scripts/preflight.py`.
      `openrouter api` must read `key accepted` and `llm model` must read
      `... is available`. Both checks are free: neither generates tokens.
      The script also FAILs on a missing column.
- [ ] In Telegram, send `/model`. Expect `This chat uses anthropic/claude-sonnet-4.6 (the default).`
- [ ] Send `/cart`. This exercises tool calling end to end.
- [ ] Ask a follow-up that needs the previous answer. This proves your old,
      pre-migration history was translated correctly.
- [ ] Check <https://openrouter.ai/activity>. The requests should appear
      under the app name.

## 5. Switching models (backend)

Change the **default for everyone** by setting `LLM_MODEL` in `.env`, then
`docker compose up -d`. This moves every chat that has not pinned its own model.

Change **one chat** without a restart. The change applies from that chat's next message:

```bash
docker compose run --rm picnic-bot python scripts/chat_model.py list
docker compose run --rm picnic-bot python scripts/chat_model.py set <chat_id> openai/gpt-5
docker compose run --rm picnic-bot python scripts/chat_model.py reset <chat_id>   # back to LLM_MODEL
```

The `chat_id` values are in the `list` output.

Choosing a model:

- Model ids come from <https://openrouter.ai/models>. Copy the id exactly,
  e.g. `anthropic/claude-sonnet-4.6` or `google/gemini-2.5-pro`.
- **The model must support tool calling.** Filter the models page by *Tools*.
  A model without it fails with "No endpoints found that support tool use",
  and the user sees the generic error.
- Tool-use quality varies a lot between models. Try a new model on your own
  chat (`set <your chat_id> ...`) before switching the default.
- A chat can switch model mid-conversation and keeps its history.

## 6. Letting users pick a model (later)

The backend is ready for this:

- `llm.set_chat_model(chat_id, model)` stores the choice and enforces
  `LLM_ALLOWED_MODELS`. Set that variable **before** exposing a picker, so
  users can't pick arbitrarily expensive models.
- `/model` is currently **read-only**. A picker could add `/model <id>`
  or an inline keyboard over `llm.allowed_models()` that calls
  `set_chat_model`.

## 7. Rollback

1. Redeploy the previous image tag (`ghcr.io/...:<previous sha>`).
2. Put `ANTHROPIC_API_KEY` back into `.env`.

You do **not** need to downgrade the database. The old code ignores the extra
nullable column. To remove it anyway:
`docker compose run --rm picnic-bot alembic downgrade 0002`. If something went
badly wrong, the backup from step 3a is `data/picnic.pre-openrouter.db` in
the volume.

## What changed, for reference

- `llm.py` (new) holds the OpenRouter client (the `openai` SDK against
  OpenRouter's OpenAI-compatible API), the translation between stored
  history and OpenAI messages, and per-chat model resolution.
- The `anthropic` dependency was removed and `openai` added.
- `chat_settings.model` is a new column (migration `0003`).
- `/model` shows the chat's model. `scripts/chat_model.py` switches it.
- `preflight.py` now checks the OpenRouter key, the model id and schema drift.
  The bot logs schema drift at startup.
- Behaviour notes: failures now surface as `openai.*Error`, not
  `anthropic.*Error`. The user still sees the same friendly message. No
  prompt caching was used before, and none is used now.
