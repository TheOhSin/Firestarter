import logging
import os
from pathlib import Path

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken


logger = logging.getLogger("firestarter.config")


SECRET_KEYS = {
    "discord_token",
    "fireshare_password",
}


ENV_IMPORT_MAP = {
    "APP_NAME": "app_name",
    "PUBLIC_SERVICE_NAME": "public_service_name",
    "DISCORD_TOKEN": "discord_token",
    "DISCORD_GUILD_ID": "discord_guild_id",
    "ONBOARDING_ROLE_ID": "onboarding_role_id",
    "FIRESHARE_URL": "fireshare_url",
    "PUBLIC_FIRESHARE_URL": "public_fireshare_url",
    "FIRESHARE_USERNAME": "fireshare_username",
    "FIRESHARE_PASSWORD": "fireshare_password",
}


class ConfigStore:
    def __init__(
        self,
        database_path,
        key_path="/data/.firestarter-key",
    ):
        self.database_path = database_path
        self.key_path = Path(key_path)
        self.fernet = None

    def _load_or_create_key(self):
        self.key_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        try:
            fd = os.open(
                self.key_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL,
                0o600,
            )

            key = Fernet.generate_key()

            with os.fdopen(
                fd,
                "wb",
            ) as handle:
                handle.write(key)

            logger.info(
                "Created Firestarter encryption key"
            )

        except FileExistsError:
            key = self.key_path.read_bytes().strip()

        self.fernet = Fernet(key)

    async def initialize(self):
        self._load_or_create_key()

        async with aiosqlite.connect(
            self.database_path
        ) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    encrypted INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.commit()

    async def has(
        self,
        key,
    ):
        async with aiosqlite.connect(
            self.database_path
        ) as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM settings
                WHERE key = ?
                """,
                (key,),
            )

            return (
                await cursor.fetchone()
            ) is not None

    async def set(
        self,
        key,
        value,
        secret=None,
    ):
        if value is None:
            value = ""

        value = str(value)

        if secret is None:
            secret = key in SECRET_KEYS

        stored = value

        if secret and value:
            stored = self.fernet.encrypt(
                value.encode("utf-8")
            ).decode("ascii")

        async with aiosqlite.connect(
            self.database_path
        ) as db:
            await db.execute("""
                INSERT INTO settings (
                    key,
                    value,
                    encrypted,
                    updated_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    CURRENT_TIMESTAMP
                )

                ON CONFLICT(key)
                DO UPDATE SET
                    value = excluded.value,
                    encrypted = excluded.encrypted,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                key,
                stored,
                1 if secret else 0,
            ))

            await db.commit()

    async def get(
        self,
        key,
        default=None,
    ):
        async with aiosqlite.connect(
            self.database_path
        ) as db:
            cursor = await db.execute("""
                SELECT
                    value,
                    encrypted
                FROM settings
                WHERE key = ?
            """, (
                key,
            ))

            row = await cursor.fetchone()

        if row is None:
            return default

        value, encrypted = row

        if not encrypted:
            return value

        if not value:
            return ""

        try:
            return self.fernet.decrypt(
                value.encode("ascii")
            ).decode("utf-8")

        except InvalidToken:
            logger.error(
                "Unable to decrypt setting %s",
                key,
            )
            raise RuntimeError(
                f"Unable to decrypt Firestarter setting: {key}"
            )

    async def get_public_settings(self):
        async with aiosqlite.connect(
            self.database_path
        ) as db:
            cursor = await db.execute("""
                SELECT
                    key,
                    value,
                    encrypted,
                    updated_at
                FROM settings
                ORDER BY key
            """)

            rows = await cursor.fetchall()

        output = {}

        for (
            key,
            value,
            encrypted,
            updated_at,
        ) in rows:

            if encrypted:
                output[key] = {
                    "configured": bool(value),
                    "value": None,
                    "updated_at": updated_at,
                }

            else:
                output[key] = {
                    "configured": bool(value),
                    "value": value,
                    "updated_at": updated_at,
                }

        return output

    async def import_environment(
        self,
    ):
        imported = []

        for (
            env_name,
            setting_name,
        ) in ENV_IMPORT_MAP.items():

            value = os.environ.get(
                env_name
            )

            if value is None:
                continue

            if await self.has(
                setting_name
            ):
                continue

            await self.set(
                setting_name,
                value,
                secret=(
                    setting_name
                    in SECRET_KEYS
                ),
            )

            imported.append(
                setting_name
            )

        if imported:
            logger.info(
                "Imported %s existing settings "
                "from environment",
                len(imported),
            )

        return imported
