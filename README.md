# 🔥 Firestarter

Firestarter is a self-hosted Discord onboarding and account-management companion for Fireshare.

It connects a Discord server to a Fireshare instance so members can create and manage Fireshare contributor accounts through a controlled onboarding flow.

## Features

- Discord role-based onboarding
- Self-service Fireshare contributor account creation
- Fresh Fireshare setup links
- Discord ↔ Fireshare account mapping
- Enable and disable Fireshare accounts
- Onboarding activity dashboard
- Account-management WebUI
- Discord and Fireshare connection testing
- First-run setup wizard
- Encrypted credential storage
- Persistent SQLite database
- Built-in Docker health check
- Non-root container
- Read-only container filesystem
- No Docker socket access required

## How It Works

When a Discord member receives the configured onboarding role, Firestarter sends them a private onboarding prompt.

The member can then create a Fireshare contributor account without receiving Fireshare administrator credentials.

~~
Discord Member
      │
      │ onboarding role
      ▼
Firestarter Discord Bot
      │
      │ verifies membership
      ▼
Account Registration
      │
      │ Fireshare Admin API
      ▼
Fireshare Contributor Account
~~

## Your Discord Bot

Firestarter does **not** provide a shared Discord bot.

Each Firestarter installation should use its own Discord application and bot.

During setup, Firestarter generates an invite link for the Discord bot associated with the token you entered.

That link adds **your bot** to **your Discord server**.

## Requirements

- Docker
- Docker Compose
- A running Fireshare instance
- A Discord server
- A Discord application and bot
- A dedicated Fireshare administrator account for Firestarter

## Quick Start

Clone Firestarter:

~~bash
git clone https://github.com/YOUR_USERNAME/firestarter.git
cd firestarter
~~

Create the local environment file:

~~bash
cp .env.example .env
~~

For LAN access, edit `.env` and set:

~~env
FIRESTARTER_BIND_IP=192.168.1.10
~~

For local or reverse-proxy-only access, leave:

~~env
FIRESTARTER_BIND_IP=127.0.0.1
~~

Start Firestarter:

~~bash
docker compose up -d --build
~~

Then open:

~~
http://YOUR_SERVER_IP:8090
~~

The first-run WebUI will guide you through the rest of the configuration.

## First-Run Setup

Firestarter configures:

1. Web administrator account
2. Instance name
3. Discord bot
4. Discord server
5. Onboarding role
6. Fireshare administrator connection
7. Public Fireshare URL

Discord and Fireshare credentials are entered through the WebUI and are not stored in `.env`.

## Discord Setup

Create a Discord application and bot in the Discord Developer Portal.

Enable:

~~
Server Members Intent
~~

under:

~~
Bot → Privileged Gateway Intents
~~

Message Content Intent is not required for the normal Firestarter onboarding flow.

Paste your bot token into Firestarter during setup. Firestarter validates the token and generates an authorization link for that bot.

## Fireshare Setup

Create a dedicated Fireshare administrator account for Firestarter.

Firestarter uses it to:

- create contributor accounts
- generate setup links
- inspect account state
- enable accounts
- disable accounts

Contributor accounts created by Firestarter use:

~~
view_private
upload
edit_own
delete_own
manage_tags
~~

## Docker Configuration

The `.env` file contains deployment settings only.

Example:

~~env
APP_VERSION=1.0.0-rc1
FIRESTARTER_BIND_IP=127.0.0.1
WEB_PORT=8090
DATABASE_PATH=/data/bot.db
FIRESTARTER_DATA_DIR=./data
WEB_SECURE_COOKIES=false
FIRESHARE_HOST_ALIAS=host.docker.internal
~~

### HTTPS

When the Firestarter WebUI is served through HTTPS, set:

~~env
WEB_SECURE_COOKIES=true
~~

Leave it disabled when accessing the WebUI directly over plain HTTP.

## Persistent Data

Persistent state is stored in `/data`.

With the default Compose configuration this maps to:

~~
./data
~~

This may contain:

~~
bot.db
.firestarter-key
.web-session-secret
~~

Never commit or publish the contents of the data directory.

## Security

Firestarter:

- runs as a non-root user
- uses a read-only root filesystem
- drops Linux capabilities
- enables `no-new-privileges`
- stores writable state only in `/data`
- does not require Docker socket access

Sensitive runtime configuration is encrypted at rest using a locally generated key stored in the persistent data directory.

Do not expose the Firestarter WebUI directly to the public Internet without HTTPS and appropriate access controls.

## Health

Check Firestarter:

~~bash
docker compose ps
~~

View logs:

~~bash
docker compose logs -f firestarter
~~

## Release Status

Current release candidate:

~~
1.0.0-rc1
~~

Firestarter is currently undergoing clean-install testing before the stable `v1.0.0` release.
