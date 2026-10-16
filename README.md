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
- Prebuilt `linux/amd64` and `linux/arm64` container images

## Deployment Model

Normal users do **not** need to clone this repository or build Firestarter from source.

Firestarter is published as a prebuilt container image on GitHub Container Registry:

```text
ghcr.io/theohsin/firestarter:1.0.0-rc2
```

You can use that image from your own Docker Compose file, Portainer stack, or another container-management platform.

The GitHub repository contains the source code and a reference Compose configuration, but the repository itself is not required to run Firestarter.

## Quick Start

Create a directory for Firestarter:

```bash
mkdir firestarter
cd firestarter
```

Create a `compose.yml` with the following example:

```yaml
services:
  firestarter:
    image: ghcr.io/theohsin/firestarter:1.0.0-rc2
    container_name: firestarter
    restart: unless-stopped

    ports:
      - "8090:8090"

    environment:
      DATABASE_PATH: /data/bot.db
      WEB_PORT: 8090
      WEB_SECURE_COOKIES: "false"

    volumes:
      - firestarter-data:/data

    extra_hosts:
      - "host.docker.internal:host-gateway"

    read_only: true

    tmpfs:
      - /tmp

    cap_drop:
      - ALL

    security_opt:
      - no-new-privileges:true

volumes:
  firestarter-data:
```

Start Firestarter:

```bash
docker compose up -d
```

Docker will automatically pull the Firestarter image from GHCR if it is not already present locally.

Check the container:

```bash
docker compose ps
```

Then open:

```text
http://YOUR_SERVER_IP:8090
```

The first-run WebUI will guide you through the remaining configuration.

## Port Binding

The example above publishes Firestarter on all host interfaces:

```yaml
ports:
  - "8090:8090"
```

To bind Firestarter to a specific LAN address instead:

```yaml
ports:
  - "192.168.1.50:8090:8090"
```

For reverse-proxy-only or local-host access:

```yaml
ports:
  - "127.0.0.1:8090:8090"
```

## Image Versions

For the current release candidate, use:

```yaml
image: ghcr.io/theohsin/firestarter:1.0.0-rc2
```

Pinning a version is recommended for predictable deployments.

After the first stable release, `latest` will point to the current stable Firestarter release. Release-candidate tags will not become `latest`.

## First-Run Setup

Firestarter configures application settings through the WebUI rather than requiring Discord or Fireshare credentials in Docker Compose.

The setup wizard covers:

1. Web administrator account
2. Instance name
3. Discord bot
4. Discord server
5. Onboarding role
6. Fireshare administrator connection
7. Public Fireshare URL

Discord and Fireshare credentials are stored by Firestarter and are not required in the Compose file.

On a brand-new installation, the first person who can access the WebUI can create the Firestarter administrator account. Do not expose an unconfigured Firestarter instance to the public Internet.

## Your Discord Bot

Firestarter does **not** provide a shared Discord bot.

Each Firestarter installation should use its own Discord application and bot.

During setup, Firestarter validates the bot token you enter and generates an invite link for that specific bot.

That link adds **your bot** to **your Discord server**.

### Discord Requirements

Create a Discord application and bot in the Discord Developer Portal.

Enable:

```text
Server Members Intent
```

under:

```text
Bot → Privileged Gateway Intents
```

Message Content Intent is not required for the normal Firestarter onboarding flow.

## Fireshare Setup

Create a dedicated Fireshare administrator account for Firestarter.

Firestarter uses it to:

- create contributor accounts
- generate setup links
- inspect account state
- enable accounts
- disable accounts

Contributor accounts created by Firestarter use:

```text
view_private
upload
edit_own
delete_own
manage_tags
```

## Compose Options

The minimum useful environment configuration is:

```yaml
environment:
  DATABASE_PATH: /data/bot.db
  WEB_PORT: 8090
  WEB_SECURE_COOKIES: "false"
```

### `DATABASE_PATH`

Default:

```text
/data/bot.db
```

This should normally remain inside `/data` so it persists in the Docker volume.

### `WEB_PORT`

Default:

```text
8090
```

If you change the internal WebUI port, update the Compose port mapping as well.

### `WEB_SECURE_COOKIES`

Use:

```yaml
WEB_SECURE_COOKIES: "false"
```

for direct LAN HTTP access.

When the Firestarter WebUI itself is served through HTTPS, use:

```yaml
WEB_SECURE_COOKIES: "true"
```

### Fireshare on the Docker Host

The reference Compose includes:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

This gives the container a reliable hostname for reaching services on the Docker host.

If your Fireshare deployment uses another Docker network or remote host, configure connectivity appropriately for your environment.

## Persistent Data

Persistent state is stored inside the container at `/data`.

The recommended Compose configuration uses a Docker named volume:

```yaml
volumes:
  - firestarter-data:/data
```

with:

```yaml
volumes:
  firestarter-data:
```

Using a named volume avoids host-directory ownership problems during first-run setup.

The volume may contain:

```text
bot.db
.firestarter-key
.web-session-secret
```

Do not delete the volume unless you intentionally want to reset Firestarter.

Never publish Firestarter's persistent data or encryption keys.

## Security

The recommended Firestarter container configuration:

- runs as a non-root user
- uses a read-only root filesystem
- drops Linux capabilities
- enables `no-new-privileges`
- stores writable state only in `/data`
- does not require Docker socket access

Sensitive runtime configuration is encrypted at rest using a locally generated key stored in the persistent data volume.

Do not expose the Firestarter WebUI directly to the public Internet without HTTPS and appropriate access controls.

## Updating

If you pin a specific Firestarter version, update the image tag in your Compose file and run:

```bash
docker compose pull
docker compose up -d
```

For example:

```yaml
image: ghcr.io/theohsin/firestarter:1.0.0-rc2
```

## Health and Logs

Check Firestarter:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f firestarter
```

## Building from Source

Building from source is intended for development or contributors. It is **not** required for normal installation.

```bash
git clone https://github.com/TheOhSin/Firestarter.git
cd Firestarter
docker build -t firestarter-dev .
```

Normal users should use the published GHCR image instead.

## Release Status

Current release candidate:

```text
1.0.0-rc2
```

A clean installation using the published multi-architecture GHCR image and Docker-managed persistent volume has been successfully tested.
