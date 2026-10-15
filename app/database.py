import aiosqlite
from datetime import datetime, timezone


class Database:
    def __init__(self, path):
        self.path = path

    async def initialize(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    discord_id INTEGER PRIMARY KEY,
                    discord_username TEXT NOT NULL,
                    fireshare_username TEXT NOT NULL UNIQUE,
                    fireshare_user_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS onboarding (
                    discord_id INTEGER PRIMARY KEY,
                    generation INTEGER NOT NULL DEFAULT 0,
                    last_role_added_at TEXT,
                    last_prompt_sent_at TEXT,
                    last_prompt_status TEXT,
                    registration_state TEXT NOT NULL DEFAULT 'none'
                )
            """)

            await db.commit()

    async def get_by_discord_id(self, discord_id):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT
                    discord_id,
                    discord_username,
                    fireshare_username,
                    fireshare_user_id,
                    created_at
                FROM users
                WHERE discord_id = ?
            """, (discord_id,))
            return await cur.fetchone()


    async def list_linked_users(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT
                    u.discord_id,
                    u.discord_username,
                    u.fireshare_username,
                    u.fireshare_user_id,
                    u.created_at,
                    COALESCE(
                        o.registration_state,
                        'none'
                    )
                FROM users AS u
                LEFT JOIN onboarding AS o
                    ON o.discord_id = u.discord_id
                ORDER BY
                    LOWER(u.fireshare_username)
            """)

            return await cur.fetchall()


    async def add_user(
        self,
        discord_id,
        discord_username,
        fireshare_username,
        fireshare_user_id,
    ):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO users (
                    discord_id,
                    discord_username,
                    fireshare_username,
                    fireshare_user_id
                )
                VALUES (?, ?, ?, ?)
            """, (
                discord_id,
                discord_username,
                fireshare_username,
                fireshare_user_id,
            ))

            await db.execute("""
                INSERT INTO onboarding (
                    discord_id,
                    registration_state
                )
                VALUES (?, 'registered')
                ON CONFLICT(discord_id)
                DO UPDATE SET registration_state='registered'
            """, (discord_id,))

            await db.commit()

    async def delete_by_discord_id(self, discord_id):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM users WHERE discord_id = ?",
                (discord_id,),
            )

            await db.execute("""
                INSERT INTO onboarding (
                    discord_id,
                    registration_state
                )
                VALUES (?, 'none')
                ON CONFLICT(discord_id)
                DO UPDATE SET registration_state='none'
            """, (discord_id,))

            await db.commit()
            return cur.rowcount


    async def list_onboarding(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT
                    o.discord_id,
                    COALESCE(
                        u.discord_username,
                        ''
                    ),
                    COALESCE(
                        u.fireshare_username,
                        ''
                    ),
                    o.generation,
                    o.last_role_added_at,
                    o.last_prompt_sent_at,
                    o.last_prompt_status,
                    o.registration_state
                FROM onboarding AS o
                LEFT JOIN users AS u
                    ON u.discord_id = o.discord_id
                ORDER BY
                    COALESCE(
                        o.last_role_added_at,
                        o.last_prompt_sent_at,
                        ''
                    ) DESC
            """)

            return await cur.fetchall()



    async def record_role_added(self, discord_id):
        now = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO onboarding (
                    discord_id,
                    generation,
                    last_role_added_at,
                    registration_state
                )
                VALUES (?, 1, ?, 'prompt_pending')

                ON CONFLICT(discord_id)
                DO UPDATE SET
                    generation = onboarding.generation + 1,
                    last_role_added_at = excluded.last_role_added_at,
                    registration_state='prompt_pending'
            """, (discord_id, now))

            await db.commit()

            cur = await db.execute("""
                SELECT generation
                FROM onboarding
                WHERE discord_id = ?
            """, (discord_id,))

            row = await cur.fetchone()
            return row[0]

    async def record_prompt_result(
        self,
        discord_id,
        status,
    ):
        now = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                UPDATE onboarding
                SET
                    last_prompt_sent_at = ?,
                    last_prompt_status = ?,
                    registration_state = CASE
                        WHEN ? = 'sent'
                            THEN 'prompted'
                        ELSE registration_state
                    END
                WHERE discord_id = ?
            """, (
                now,
                status,
                status,
                discord_id,
            ))

            await db.commit()

    async def set_registration_state(
        self,
        discord_id,
        state,
    ):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO onboarding (
                    discord_id,
                    registration_state
                )
                VALUES (?, ?)

                ON CONFLICT(discord_id)
                DO UPDATE SET
                    registration_state =
                        excluded.registration_state
            """, (
                discord_id,
                state,
            ))

            await db.commit()
