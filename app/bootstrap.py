import asyncio
import logging
import os
import sys

import aiohttp
import uvicorn

from config_store import ConfigStore


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s "
        "%(levelname)s "
        "%(name)s: %(message)s"
    ),
)

logger = logging.getLogger(
    "firestarter.bootstrap"
)

DATABASE_PATH = os.environ.get(
    "DATABASE_PATH",
    "/data/bot.db",
)

WEB_PORT = int(
    os.environ.get(
        "WEB_PORT",
        "8090",
    )
)

RUNTIME_SETTINGS = {
    "app_name": "APP_NAME",
    "public_service_name": "PUBLIC_SERVICE_NAME",
    "discord_token": "DISCORD_TOKEN",
    "discord_guild_id": "DISCORD_GUILD_ID",
    "onboarding_role_id": "ONBOARDING_ROLE_ID",
    "fireshare_url": "FIRESHARE_URL",
    "public_fireshare_url": "PUBLIC_FIRESHARE_URL",
    "fireshare_username": "FIRESHARE_USERNAME",
    "fireshare_password": "FIRESHARE_PASSWORD",
}


async def prepare_store():
    store = ConfigStore(
        DATABASE_PATH
    )

    await store.initialize()

    # Compatibility migration for older installs.
    await store.import_environment()

    return store


async def missing_runtime_settings(
    store,
):
    missing = []

    for setting_name in RUNTIME_SETTINGS:
        value = await store.get(
            setting_name
        )

        if not value:
            missing.append(
                setting_name
            )

    return missing


async def validate_discord_token(
    token,
):
    timeout = aiohttp.ClientTimeout(
        total=15
    )

    try:
        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                "https://discord.com/api/v10/users/@me",
                headers={
                    "Authorization": (
                        "Bot " + token
                    ),
                    "User-Agent": (
                        "Firestarter bootstrap"
                    ),
                },
            ) as response:

                if response.status == 200:
                    return "valid"

                if response.status in (
                    401,
                    403,
                ):
                    return "invalid"

                logger.warning(
                    "Discord token validation "
                    "returned HTTP %s",
                    response.status,
                )

                return "unknown"

    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
    ) as exc:

        logger.warning(
            "Could not validate Discord token "
            "because Discord was unreachable: %s",
            type(exc).__name__,
        )

        return "unknown"


async def load_runtime_configuration(
    store,
):
    missing = await missing_runtime_settings(
        store
    )

    if missing:
        return False, missing

    for (
        setting_name,
        environment_name,
    ) in RUNTIME_SETTINGS.items():

        value = await store.get(
            setting_name
        )

        os.environ[
            environment_name
        ] = str(value)

    return True, []


async def run_setup_mode(
    store,
    reason,
):
    logger.warning(
        "Firestarter entering WebUI "
        "setup/recovery mode: %s",
        reason,
    )

    logger.info(
        "Starting Firestarter WebUI "
        "on port %s",
        WEB_PORT,
    )

    os.environ[
        "FIRESTARTER_SETUP_REASON"
    ] = reason

    from setup_app import (
        create_setup_app,
    )

    handoff_requested = False
    server = None

    def request_runtime_handoff():
        nonlocal handoff_requested

        handoff_requested = True

        logger.info(
            "Runtime handoff requested. "
            "Stopping setup WebUI."
        )

        if server is not None:
            server.should_exit = True

    app = create_setup_app(
        store=store,
        database_path=DATABASE_PATH,
        request_runtime_handoff=(
            request_runtime_handoff
        ),
    )

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=WEB_PORT,
        log_level="warning",
        access_log=False,
    )

    server = uvicorn.Server(
        config
    )

    await server.serve()

    if handoff_requested:
        logger.info(
            "Setup WebUI stopped cleanly. "
            "Re-running Firestarter bootstrap."
        )

        os.execv(
            sys.executable,
            [
                sys.executable,
                "/app/bootstrap.py",
            ],
        )

        return 1

    return 0


async def bootstrap():
    store = await prepare_store()

    ready, missing = (
        await load_runtime_configuration(
            store
        )
    )

    if not ready:
        logger.warning(
            "Firestarter configuration "
            "is incomplete."
        )

        logger.warning(
            "Missing runtime settings: %s",
            ", ".join(missing),
        )

        return await run_setup_mode(
            store,
            "configuration_incomplete",
        )

    discord_token = await store.get(
        "discord_token",
        "",
    )

    token_status = (
        await validate_discord_token(
            discord_token
        )
    )

    if token_status == "invalid":
        logger.error(
            "Stored Discord token has been "
            "rejected by Discord."
        )

        logger.error(
            "The Discord bot will NOT start."
        )

        logger.info(
            "Use the Firestarter WebUI to "
            "replace the Discord token."
        )

        return await run_setup_mode(
            store,
            "discord_token_invalid",
        )

    if token_status == "unknown":
        # Do not assume a temporary Discord/network
        # problem means the stored credential is bad.
        logger.warning(
            "Discord token could not be "
            "pre-validated. Attempting normal "
            "Firestarter startup."
        )

    logger.info(
        "Loaded %s runtime settings "
        "from Firestarter database",
        len(RUNTIME_SETTINGS),
    )

    logger.info(
        "Starting full Firestarter runtime"
    )

    os.execv(
        sys.executable,
        [
            sys.executable,
            "/app/main.py",
        ],
    )

    return 1


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            bootstrap()
        )
    )
