---
name: deploy-bot
description: Build and package eXpress bot for offline deployment. Use when building, packaging, or preparing the bot for transfer to a server without internet access.
---
# Offline Deploy Bot

## Context

Target server has NO internet access. Deployment is done by transferring
pre-built Docker images as `.tar` files via an engineer.

## Prerequisites

- Docker and Docker Compose installed locally (build machine with internet)
- Docker and Docker Compose installed on target server (no internet)
- Bot registered in eXpress admin panel
- Environment variables prepared (see `.env-example`)

## Build & Package Workflow

### Step 1: Ensure Changes Are Ready

```bash
# Check status
git status

# Commit changes
git add .
git commit -m "feat: description of changes"
```

### Step 2: Build Deploy Package

```bash
# Build images and create deploy archive
./build.sh
```

This creates `dist/kids_ai-deploy.tar.gz` containing:
- `kids_ai_bot.tar` — bot Docker image
- `postgres.tar` — PostgreSQL image
- `redis.tar` — Redis image
- `docker-compose.yml` — compose configuration
- `.env-example` — environment template
- `DEPLOY.md` — installation guide for engineer

### Step 3: Transfer to Engineer

Send `dist/kids_ai-deploy.tar.gz` to the lead engineer.
They follow the instructions in `DEPLOY.md`.

## Server-Side Installation (Engineer)

```bash
# Unpack
mkdir -p /opt/kids_ai && cd /opt/kids_ai
tar xzf kids_ai-deploy.tar.gz

# Load images
docker load -i dist/kids_ai_bot.tar
docker load -i dist/redis.tar
docker load -i dist/postgres.tar

# Configure
cp .env-example .env
nano .env

# Start the stack: postgres + redis + bot
docker compose up -d
```

## Environment Configuration

### Required Variables (.env)

```env
# Bot credentials (from eXpress admin panel)
BOT_ID=uuid-of-your-bot
CTS_URL=https://your-cts.example.com
BOT_SECRET_KEY=your-secret-key
ADMIN_HUID=admin-uuid

# Database (постгрес из docker-compose, named volume pgdata)
DB_HOST=172.20.0.3
DB_PORT=5432
DB_NAME=kids_ai
DB_USER=postgres
DB_PASSWORD=secure_password

# Redis FSM (контейнер из docker-compose, AOF на named volume redisdata)
REDIS_URL=redis://172.20.0.4:6379/0

# Server
SERVER_PORT=8000
DEBUG=False
```

## Update Procedure

1. Make changes locally, commit
2. Run `./build.sh`
3. Transfer new `dist/kids_ai-deploy.tar.gz`
4. On server:

```bash
docker compose down
docker load -i dist/kids_ai_bot.tar
docker compose up -d
```

## Rollback Procedure

Keep previous `.tar` files with version labels:

```bash
# Before update (on server)
cp dist/kids_ai_bot.tar dist/kids_ai_bot_v1.tar

# Rollback
docker compose down
docker load -i dist/kids_ai_bot_v1.tar
docker compose up -d
```

## Health Check

After deployment, verify:

1. `curl http://localhost:8000/healthz` returns `{"status":"healthy"}`
2. Bot responds to commands in eXpress chat
3. No errors in logs: `docker compose logs -f bot`

```bash
# Quick check
docker compose ps
curl http://localhost:8000/healthz
docker compose logs --tail=50 bot
```

## Troubleshooting

| Issue | Solution |
|---|---|
| Container won't start | `docker compose logs bot` |
| Database connection error | Check `DB_HOST`, `DB_PASSWORD` in `.env` |
| Bot not responding | Check `CTS_URL`, `BOT_ID`, `BOT_SECRET_KEY` |
| Image not found | Verify `docker load` completed successfully |
| PostgreSQL not starting | `docker compose logs postgres` |
| Redis not starting | `docker compose logs redis` |
| Port conflict | Change `SERVER_PORT` in `.env` |
