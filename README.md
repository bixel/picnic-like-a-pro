# Picnic Like a Pro

A family meal planner and grocery assistant Telegram bot powered by an LLM of your choice via OpenRouter (Claude Sonnet by default). It connects to your shared [Picnic](https://picnic.app) grocery account and helps you plan meals, manage your cart, track order history, and forecast what you'll need to reorder.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Create a Telegram Bot](#2-create-a-telegram-bot)
3. [Find your Telegram User ID](#3-find-your-telegram-user-id)
4. [Provision the VPS](#4-provision-the-vps)
5. [Install Docker on the VPS](#5-install-docker-on-the-vps)
6. [Deploy the Bot](#6-deploy-the-bot)
7. [Import Order History](#7-import-order-history)
8. [Verify It Works](#8-verify-it-works)
9. [Keeping the Bot Up to Date](#9-keeping-the-bot-up-to-date)
10. [Automated CI/CD Deployment](#10-automated-cicd-deployment)
11. [Useful Commands](#11-useful-commands)
12. [Troubleshooting](#12-troubleshooting)
13. [Order Reports (local)](#13-order-reports-local)
14. [Run the Bot Locally](#14-run-the-bot-locally)

---

## 1. Prerequisites

**What you will need before starting:**

| Requirement | Details |
|---|---|
| A VPS | Any provider works (Hetzner, DigitalOcean, Linode, etc.). A 1 vCPU / 512 MB RAM instance is sufficient. |
| OS | Ubuntu 22.04 LTS or Debian 12 recommended |
| A Picnic account | Registered in NL, DE, or BE |
| A Telegram account | To create the bot and to use it |
| An OpenRouter API key | From [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys), with credits loaded. All LLM requests go through OpenRouter, so any model it lists can be used |
| SSH access to the VPS | With a non-root user that has `sudo` privileges |

---

## 2. Create a Telegram Bot

1. Open Telegram and start a chat with **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot` and follow the prompts:
   - Enter a display name (e.g. `My Picnic Bot`)
   - Enter a username ending in `bot` (e.g. `mypicnic_bot`)
3. BotFather will reply with a **bot token** that looks like `123456789:ABCDefGhIJKlmNoPQRsTUVwxyZ`.
4. **Save this token** — you will need it as `TELEGRAM_BOT_TOKEN` later.

---

## 3. Find Your Telegram User ID

The bot only accepts messages from a list of explicitly allowed Telegram user IDs. You need to add your own ID before it will respond to you.

1. Start a chat with **[@userinfobot](https://t.me/userinfobot)** on Telegram.
2. Send any message — it will reply with your numeric user ID (e.g. `123456789`).
3. Note this number — you will need it as `ALLOWED_TELEGRAM_USER_IDS` later.
4. Repeat for every family member who should have access.

---

## 4. Provision the VPS

SSH into your VPS and create a dedicated user for the bot (optional but recommended):

```bash
# Create a user (skip if you already have a non-root user)
sudo adduser picnic
sudo usermod -aG sudo picnic
sudo usermod -aG docker picnic   # add after Docker is installed in step 5

# Switch to the new user
su - picnic
```

Create the directory where the bot will live:

```bash
mkdir -p ~/picnic-bot
cd ~/picnic-bot
```

---

## 5. Install Docker on the VPS

Run the following on your VPS to install Docker Engine and the Compose plugin:

```bash
# Remove any old Docker packages
sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

# Install dependencies
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Allow your user to run Docker without sudo
sudo usermod -aG docker $USER

# Apply the group change in the current session
newgrp docker

# Verify installation
docker --version
docker compose version
```

> **Note for Debian:** Replace `ubuntu` with `debian` in the repository URL above.

---

## 6. Deploy the Bot

### 6a. Create the environment file

Inside `~/picnic-bot`, create a `.env` file with your credentials. **This file contains secrets — never commit it to version control.**

```bash
nano ~/picnic-bot/.env
```

Paste and fill in the following:

```dotenv
# Picnic account credentials
PICNIC_USERNAME=your@email.com
PICNIC_PASSWORD=yourpassword
PICNIC_COUNTRY_CODE=NL          # NL, DE, or BE

# Telegram bot token (from BotFather)
TELEGRAM_BOT_TOKEN=123456:ABC-...

# OpenRouter API key — every LLM request goes through OpenRouter
OPENROUTER_API_KEY=sk-or-v1-...

# Default model for all chats (any id from https://openrouter.ai/models).
# Individual chats can be switched with scripts/chat_model.py.
LLM_MODEL=anthropic/claude-sonnet-4.6

# Comma-separated Telegram user IDs allowed to use the bot
# Find your ID via @userinfobot — leave empty to block everyone
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321

# Database location (inside the Docker volume — no need to change)
DB_PATH=data/picnic.db
```

Save with `Ctrl+O`, then `Ctrl+X`.

Restrict file permissions so only your user can read it:

```bash
chmod 600 ~/picnic-bot/.env
```

### 6b. Create the Docker Compose file

```bash
nano ~/picnic-bot/docker-compose.yml
```

Paste the following, replacing `YOUR_GITHUB_USERNAME/picnic-like-a-pro` with the actual GitHub repository path:

```yaml
services:
  picnic-bot:
    image: ghcr.io/YOUR_GITHUB_USERNAME/picnic-like-a-pro:latest
    container_name: picnic-bot
    restart: unless-stopped
    env_file: .env
    volumes:
      - picnic-data:/app/data

volumes:
  picnic-data:
```

Save with `Ctrl+O`, then `Ctrl+X`.

### 6c. Log in to the GitHub Container Registry

The Docker image is hosted on GitHub Container Registry (ghcr.io). To pull it you need to authenticate with a GitHub Personal Access Token (PAT).

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**.
2. Click **Generate new token (classic)**.
3. Give it a name (e.g. `vps-picnic-bot`), select the `read:packages` scope, and click **Generate token**.
4. Copy the token.

Back on your VPS:

```bash
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

You should see `Login Succeeded`.

### 6d. Pull the image and start the bot

```bash
cd ~/picnic-bot
docker compose pull
docker compose up -d
```

The bot is now running in the background. Check that the container started:

```bash
docker compose ps
```

You should see `picnic-bot` with status `Up`.

Check the logs to confirm it connected to Telegram successfully:

```bash
docker compose logs -f
```

You should see output similar to:

```
picnic-bot  | INFO  Bot started. Listening for messages...
```

Press `Ctrl+C` to stop following the logs (the bot keeps running).

---

## 7. Import Order History

On first run, the database is empty. Run the history import once to populate it with your past Picnic deliveries. This enables the order history, frequently-ordered products, and forecasting features.

```bash
cd ~/picnic-bot
docker compose run --rm picnic-bot import-history
```

The import is resumable — if it is interrupted, re-run the same command and it will continue with the deliveries that are still missing. Depending on how many past deliveries you have, this may take a few minutes (the script waits 2 seconds between batches to respect Picnic's rate limits).

Re-run it whenever you want to pick up new deliveries: each run imports only the ones that have no order in the local database yet, so it is safe to run on a schedule (for example a weekly cron job), and it also retries deliveries that were still in progress last time. Cancelled deliveries are skipped, and a run that finds nothing new writes nothing to the database. With Docker Compose:

```bash
# e.g. every Monday at 07:00
0 7 * * 1 cd ~/picnic-bot && docker compose run --rm picnic-bot import-history
```

---

## 8. Verify It Works

1. Open Telegram and find your bot by its username.
2. Send `/start` — the bot should reply with a welcome message.
3. Try `/forecast` to see products predicted to run low.
4. Try `/cart` to see your current Picnic cart.

If the bot does not respond, check the logs:

```bash
docker compose logs --tail=50 picnic-bot
```

---

## 9. Keeping the Bot Up to Date

When a new version is released, pull the latest image and restart the container:

```bash
cd ~/picnic-bot
docker compose pull
docker compose up -d
docker image prune -f   # clean up the old image to free disk space
```

The named volume `picnic-data` preserves your database across updates — your history is never lost.

---

## 10. Automated CI/CD Deployment

If you forked this repository and want GitHub Actions to build and deploy automatically:

### 10a. Set up GitHub Secrets

In your GitHub repository, go to **Settings → Secrets and variables → Actions** and add the following secrets:

| Secret name | Value |
|---|---|
| `VPS_HOST` | Your VPS IP address or hostname |
| `VPS_USER` | The SSH user on the VPS (e.g. `picnic`) |
| `VPS_SSH_KEY` | The **private** SSH key used to connect to the VPS |
| `VPS_COMPOSE_DIR` | The absolute path to the deploy directory (e.g. `/home/picnic/picnic-bot`) |

### 10b. Add your SSH public key to the VPS

If you do not already have an SSH key pair for this purpose, generate one locally:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/picnic_deploy
```

Copy the **public** key to the VPS:

```bash
ssh-copy-id -i ~/.ssh/picnic_deploy.pub picnic@YOUR_VPS_IP
```

Use the contents of `~/.ssh/picnic_deploy` (the **private** key) as the `VPS_SSH_KEY` secret.

### 10c. Trigger a deployment

The workflow is set to manual trigger only. To deploy from GitHub:

```bash
# Using the GitHub CLI
gh workflow run deploy.yml

# Or via the web UI:
# Go to Actions → Build & Deploy → Run workflow
```

The workflow will:
1. Build a fresh Docker image and push it to `ghcr.io` with two tags: `latest` and the git commit SHA.
2. SSH into the VPS and run `docker compose pull && docker compose up -d`.

---

## 11. Useful Commands

```bash
# View live logs
docker compose logs -f picnic-bot

# Stop the bot
docker compose stop picnic-bot

# Start the bot
docker compose start picnic-bot

# Restart the bot (e.g. after editing .env)
docker compose restart picnic-bot

# Remove the container (data volume is kept)
docker compose down

# Remove the container AND all data (destructive!)
docker compose down -v

# Open a shell inside the running container
docker compose exec picnic-bot bash

# Run the history import (safe to re-run; picks up new deliveries)
docker compose run --rm picnic-bot import-history

# Check how much disk the volume uses
docker system df -v | grep picnic-data
```

---

## 12. Troubleshooting

### Bot does not respond to messages

- Check the container is running: `docker compose ps`
- Check the logs for errors: `docker compose logs --tail=100 picnic-bot`
- Confirm your Telegram user ID is listed in `ALLOWED_TELEGRAM_USER_IDS` in `.env`
- Verify `TELEGRAM_BOT_TOKEN` is correct by running:
  ```bash
  curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe
  ```
  A valid token returns `{"ok":true,...}`.

### Container fails to start

- Check the logs: `docker compose logs picnic-bot`
- Common causes:
  - Missing or misspelled environment variable in `.env`
  - Wrong Picnic credentials (`PICNIC_USERNAME` / `PICNIC_PASSWORD`)
  - Invalid `OPENROUTER_API_KEY`, or an OpenRouter account without credits
  - An `LLM_MODEL` that is not a valid OpenRouter model id

### Cannot pull the Docker image

- Make sure you are logged in: `docker login ghcr.io`
- Confirm the image path in `docker-compose.yml` matches the actual GitHub repository name (case-sensitive).
- Check the package is public or that your PAT has `read:packages` scope.

### History import fails partway through

Re-run the same command — deliveries already in the database are skipped, so it continues with the ones still missing:

```bash
docker compose run --rm picnic-bot import-history
```

### The bot replies but Picnic commands fail

- Double-check `PICNIC_USERNAME`, `PICNIC_PASSWORD`, and `PICNIC_COUNTRY_CODE` in `.env`.
- If the bot says Picnic needs a login code (2FA) or the session expired, send
  `/picnic_login` (add `email` to get the code by email), then `/picnic_code <code>`.
  Or, on the server: `docker compose run --rm picnic-bot python scripts/picnic_login.py`.
  The session is stored in the database, so this is only needed when Picnic
  expires it.
- After editing `.env`, restart the container: `docker compose restart picnic-bot`.
- Confirm your Picnic account is active and can log in via the Picnic app.

---

## 13. Order Reports (local)

A small web UI shows what you spend over time: the Picnic category tree on the
left, and a chart of the selected category or product on the right. It runs on
your own machine, next to the bot, on the same database file. It is not part of
the Docker image yet.

You need [uv](https://docs.astral.sh/uv/) and Node.js 22.

```bash
uv sync
uv run alembic upgrade head            # adds the login-link table
```

```bash
cd frontend && npm ci && npm run build && cd ..
```

```bash
uv run web                             # http://localhost:8080
```

To log in, send `/report` to the bot in your private chat and open the link it
replies with on the machine that runs `uv run web`. Telegram may not make a
`localhost` address tappable; copy it into the browser instead. The link works
once and expires after 5 minutes. A session lasts 30 days; set
`WEB_SESSION_SECRET` in `.env` so it survives a restart of the web app.

The bot and the web app must use the same `DB_PATH`, and the bot must be
restarted once after pulling this change so it knows `/report`.

Category names appear once `scripts/import_history.py` has looked them up;
until then a category is shown as "Category 1234".

---

## 14. Run the Bot Locally

For development, or to try the bot without a VPS, run it straight from a
checkout. You need [uv](https://docs.astral.sh/uv/); it installs Python 3.13
itself.

```bash
git clone https://github.com/bixel/picnic-like-a-pro.git
cd picnic-like-a-pro
uv sync
cp .env.example .env
```

Fill in `.env`:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | A token from BotFather (step 2). **Use a second bot, not the deployed one** — see below. |
| `OPENROUTER_API_KEY` | Your OpenRouter key |
| `ALLOWED_TELEGRAM_USER_IDS` | Your Telegram user id (step 3) |
| `PICNIC_USERNAME`, `PICNIC_PASSWORD`, `PICNIC_COUNTRY_CODE` | Your Picnic account — or set `PICNIC_MOCK=true` instead |

Then create the database and start the bot:

```bash
uv run alembic upgrade head     # creates data/picnic.db; run again after every pull
uv run bot                      # stop with Ctrl+C
```

Send your bot a message in Telegram; the log in the terminal shows each request.

- **One process per bot token.** Telegram delivers a bot's messages to a single
  poller. If the same token also runs on the VPS, the two fight over updates
  and both log `Conflict` errors. Create a separate bot with BotFather for
  local use.
- **Try it without touching Picnic.** With `PICNIC_MOCK=true` the Picnic API is
  replaced by an in-process fake: no credentials are used and nothing can be
  ordered. The LLM calls are still real.
- **Picnic asks for a code.** With a real account, the first login may need a
  2FA code: run `uv run python scripts/picnic_login.py`, or send
  `/picnic_login` to the bot. The session is then stored in the database.
- **Order history.** `uv run python scripts/import_history.py` imports your
  deliveries; it is safe to re-run.
- **The bot refuses to start with "Database is at migration …".** The code is
  newer than the database: run `uv run alembic upgrade head`.
