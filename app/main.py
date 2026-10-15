import asyncio
import logging
import os
from urllib.parse import quote

import discord
from discord import app_commands

from database import Database
from config_store import ConfigStore
from fireshare import (
    FireshareClient,
    FireshareError,
)
from onboarding import (
    ResetAccountView,
    StartOnboardingView,
)

import uvicorn

from webapp import create_web_app


APP_VERSION = os.environ.get("APP_VERSION", "dev")

APP_NAME = os.environ.get(
    "APP_NAME",
    "Firestarter",
)

PUBLIC_SERVICE_NAME = os.environ.get(
    "PUBLIC_SERVICE_NAME",
    "Fireshare",
)

DISCORD_TOKEN = os.environ[
    "DISCORD_TOKEN"
]

DISCORD_GUILD_ID = int(
    os.environ["DISCORD_GUILD_ID"]
)

ONBOARDING_ROLE_ID = int(
    os.environ["ONBOARDING_ROLE_ID"]
)

FIRESHARE_URL = os.environ[
    "FIRESHARE_URL"
].rstrip("/")

PUBLIC_FIRESHARE_URL = os.environ.get(
    "PUBLIC_FIRESHARE_URL",
    FIRESHARE_URL,
).rstrip("/")

FIRESHARE_USERNAME = os.environ[
    "FIRESHARE_USERNAME"
]

FIRESHARE_PASSWORD = os.environ[
    "FIRESHARE_PASSWORD"
]

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


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s "
        "%(levelname)s "
        "%(name)s: %(message)s"
    ),
)

logger = logging.getLogger(
    "firestarter"
)


if ONBOARDING_ROLE_ID <= 0:
    raise RuntimeError(
        "ONBOARDING_ROLE_ID must be configured."
    )


db = Database(
    DATABASE_PATH
)

config_store = ConfigStore(
    DATABASE_PATH
)

fireshare = FireshareClient(
    FIRESHARE_URL,
    FIRESHARE_USERNAME,
    FIRESHARE_PASSWORD,
)


def profile_url(username):
    return (
        f"{PUBLIC_FIRESHARE_URL}"
        f"/u/{quote(username, safe='')}"
    )


class FirestarterClient(
    discord.Client
):
    def __init__(self):
        intents = discord.Intents.default()

        # Required to receive role/member updates.
        intents.members = True

        super().__init__(
            intents=intents
        )

        self.tree = app_commands.CommandTree(
            self
        )

        self.public_service_name = (
            PUBLIC_SERVICE_NAME
        )

        self.registration_locks = {}
        self.prompt_locks = {}

        self.web_server = None
        self.web_task = None

    async def setup_hook(self):
        await db.initialize()

        await config_store.initialize()
        await config_store.import_environment()

        await fireshare.start()

        web_app = create_web_app(
            self,
            db,
            fireshare,
            config_store,
        )

        web_config = uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=WEB_PORT,
            log_level="warning",
            access_log=False,
        )

        self.web_server = uvicorn.Server(
            web_config
        )

        self.web_task = asyncio.create_task(
            self.web_server.serve()
        )

        logger.info(
            "Firestarter WebUI starting "
            "on port %s",
            WEB_PORT,
        )

        # Persistent DM buttons survive bot restarts.
        self.add_view(
            StartOnboardingView(self)
        )

        self.add_view(
            ResetAccountView(self)
        )

        guild = discord.Object(
            id=DISCORD_GUILD_ID
        )

        await self.tree.sync(
            guild=guild
        )

        logger.info(
            "Firestarter started "
            "guild_id=%s onboarding_role_id=%s",
            DISCORD_GUILD_ID,
            ONBOARDING_ROLE_ID,
        )

    async def close(self):
        if self.web_server:
            self.web_server.should_exit = True

        if self.web_task:
            try:
                await asyncio.wait_for(
                    self.web_task,
                    timeout=10,
                )
            except (
                asyncio.TimeoutError,
                asyncio.CancelledError,
            ):
                self.web_task.cancel()

        await fireshare.close()
        await super().close()

    async def resolve_member(
        self,
        discord_id,
    ):
        guild = self.get_guild(
            DISCORD_GUILD_ID
        )

        if guild is None:
            return None

        member = guild.get_member(
            discord_id
        )

        if member is not None:
            return member

        try:
            return await guild.fetch_member(
                discord_id
            )
        except discord.NotFound:
            return None
        except discord.HTTPException:
            return None

    def member_has_onboarding_role(
        self,
        member,
    ):
        return any(
            role.id == ONBOARDING_ROLE_ID
            for role in member.roles
        )

    async def get_live_account(
        self,
        discord_id,
        clean_stale=True,
    ):
        mapping = await db.get_by_discord_id(
            discord_id
        )

        if not mapping:
            return None, None

        username = mapping[2]
        user_id = mapping[3]

        remote = await fireshare.get_user(
            user_id=user_id,
            username=username,
        )

        if remote:
            return mapping, remote

        if clean_stale:
            rows = (
                await db.delete_by_discord_id(
                    discord_id
                )
            )

            logger.info(
                "Removed stale mapping "
                "discord_id=%s "
                "username=%s rows=%s",
                discord_id,
                username,
                rows,
            )

        return None, None

    async def send_onboarding_prompt(
        self,
        member,
        generation,
    ):
        try:
            mapping, remote = (
                await self.get_live_account(
                    member.id
                )
            )

            if mapping and remote:
                if remote.get(
                    "disabled",
                    False,
                ):
                    embed = discord.Embed(
                        title=PUBLIC_SERVICE_NAME,
                        description=(
                            "Your Clips account exists, "
                            "but it is currently disabled.\n\n"
                            "Contact an administrator "
                            "if you believe this is a mistake."
                        ),
                    )

                    await member.send(
                        embed=embed
                    )

                else:
                    embed = discord.Embed(
                        title=PUBLIC_SERVICE_NAME,
                        description=(
                            "Your access role was assigned "
                            "again, but you already have "
                            "a Clips account."
                        ),
                    )

                    embed.add_field(
                        name="Username",
                        value=f"`{mapping[2]}`",
                        inline=False,
                    )

                    embed.add_field(
                        name="Profile",
                        value=(
                            f"[Open your profile]"
                            f"({profile_url(mapping[2])})"
                        ),
                        inline=False,
                    )

                    embed.add_field(
                        name="Need a new password?",
                        value=(
                            "Use the button below to "
                            "generate a fresh setup link."
                        ),
                        inline=False,
                    )

                    await member.send(
                        embed=embed,
                        view=ResetAccountView(
                            self
                        ),
                    )

            else:
                embed = discord.Embed(
                    title=(
                        f"Welcome to "
                        f"{PUBLIC_SERVICE_NAME}"
                    ),
                    description=(
                        "You've been given access "
                        "to the clip-sharing service.\n\n"
                        "Click below to create your "
                        "Contributor account."
                    ),
                )

                embed.add_field(
                    name="What happens next?",
                    value=(
                        "You'll choose a username, "
                        "then Fireshare will give you "
                        "a secure link to set your password."
                    ),
                    inline=False,
                )

                embed.set_footer(
                    text=(
                        "Firestarter never receives "
                        "or stores your password."
                    )
                )

                await member.send(
                    embed=embed,
                    view=StartOnboardingView(
                        self
                    ),
                )

            await db.record_prompt_result(
                member.id,
                "sent",
            )

            logger.info(
                "Onboarding DM sent "
                "discord_id=%s generation=%s",
                member.id,
                generation,
            )

        except discord.Forbidden:
            await db.record_prompt_result(
                member.id,
                "dm_blocked",
            )

            logger.warning(
                "Could not DM member "
                "discord_id=%s reason=dm_blocked",
                member.id,
            )

        except discord.HTTPException:
            await db.record_prompt_result(
                member.id,
                "discord_error",
            )

            logger.exception(
                "Discord error sending "
                "onboarding prompt discord_id=%s",
                member.id,
            )

        except FireshareError as exc:
            await db.record_prompt_result(
                member.id,
                "fireshare_error",
            )

            logger.warning(
                "Fireshare error while preparing "
                "onboarding discord_id=%s error=%s",
                member.id,
                exc,
            )

    async def handle_registration(
        self,
        interaction,
        member,
        username,
    ):
        # Check access again at final submission.
        if not self.member_has_onboarding_role(
            member
        ):
            await interaction.followup.send(
                "You no longer have the required role."
            )
            return

        try:
            mapping, remote = (
                await self.get_live_account(
                    member.id
                )
            )

            if mapping and remote:
                if remote.get(
                    "disabled",
                    False,
                ):
                    await interaction.followup.send(
                        "Your existing Clips account "
                        "is disabled. Contact an administrator."
                    )
                    return

                await interaction.followup.send(
                    (
                        "You already have a Clips account: "
                        f"`{mapping[2]}`\n"
                        f"{profile_url(mapping[2])}"
                    )
                )
                return

            result = (
                await fireshare.create_contributor(
                    username=username,
                    display_name=(
                        member.display_name
                    ),
                )
            )

            user = result["user"]

            await db.add_user(
                discord_id=member.id,
                discord_username=str(
                    member
                ),
                fireshare_username=(
                    user["username"]
                ),
                fireshare_user_id=(
                    user["id"]
                ),
            )

            await db.set_registration_state(
                member.id,
                "registered",
            )

            logger.info(
                "Fireshare account created "
                "discord_id=%s "
                "fireshare_user_id=%s "
                "username=%s",
                member.id,
                user["id"],
                user["username"],
            )

            embed = discord.Embed(
                title=(
                    f"{PUBLIC_SERVICE_NAME} "
                    "account created"
                ),
                description=(
                    f"Your Contributor account "
                    f"**{user['username']}** "
                    "is ready."
                ),
            )

            embed.add_field(
                name="Set your password",
                value=(
                    f"[Continue to Fireshare]"
                    f"({result['invite_url']})\n\n"
                    "This setup link expires "
                    "in 7 days."
                ),
                inline=False,
            )

            embed.add_field(
                name="Profile",
                value=(
                    f"[Open profile]"
                    f"({profile_url(user['username'])})"
                ),
                inline=False,
            )

            await interaction.followup.send(
                embed=embed
            )

        except FireshareError as exc:
            logger.warning(
                "Registration failed "
                "discord_id=%s reason=%s",
                member.id,
                exc,
            )

            await interaction.followup.send(
                str(exc)
            )

    async def send_reset_link(
        self,
        interaction,
    ):
        member = await self.resolve_member(
            interaction.user.id
        )

        if member is None:
            await interaction.followup.send(
                "I couldn't verify your "
                "Discord membership."
            )
            return

        # Old DM buttons cannot be used after
        # access has been removed.
        if not self.member_has_onboarding_role(
            member
        ):
            await interaction.followup.send(
                "You no longer have the required "
                "Discord role."
            )
            return

        try:
            mapping, remote = (
                await self.get_live_account(
                    member.id
                )
            )

            if not mapping:
                await interaction.followup.send(
                    "You don't currently have a "
                    "linked Clips account. Ask an "
                    "administrator to reassign the "
                    "access role."
                )
                return

            if remote.get(
                "disabled",
                False,
            ):
                await interaction.followup.send(
                    "Your Clips account is disabled."
                )
                return

            result = (
                await fireshare.create_invite(
                    remote["id"]
                )
            )

            if not result:
                await db.delete_by_discord_id(
                    member.id
                )

                await interaction.followup.send(
                    "Your old Fireshare account no "
                    "longer exists. The stale link "
                    "was removed. Reassign the role "
                    "to start onboarding again."
                )
                return

            logger.info(
                "Fresh password setup link issued "
                "discord_id=%s fireshare_user_id=%s",
                member.id,
                remote["id"],
            )

            embed = discord.Embed(
                title="Fresh password setup link",
                description=(
                    f"[Set your password]"
                    f"({result['invite_url']})"
                ),
            )

            embed.set_footer(
                text=(
                    "This link expires in 7 days."
                )
            )

            await interaction.followup.send(
                embed=embed
            )

        except FireshareError as exc:
            await interaction.followup.send(
                str(exc)
            )


client = FirestarterClient()


@client.event
async def on_ready():
    logger.info(
        "%s v%s connected as %s (%s)",
        APP_NAME,
        APP_VERSION,
        client.user,
        client.user.id,
    )


@client.event
async def on_member_update(
    before,
    after,
):
    if (
        after.guild.id
        != DISCORD_GUILD_ID
    ):
        return

    if after.bot:
        return

    before_roles = {
        role.id
        for role in before.roles
    }

    after_roles = {
        role.id
        for role in after.roles
    }

    role_was_added = (
        ONBOARDING_ROLE_ID
        not in before_roles
        and ONBOARDING_ROLE_ID
        in after_roles
    )

    if not role_was_added:
        return

    generation = (
        await db.record_role_added(
            after.id
        )
    )

    lock = (
        client.prompt_locks.setdefault(
            after.id,
            asyncio.Lock(),
        )
    )

    async with lock:
        await client.send_onboarding_prompt(
            after,
            generation,
        )


@client.tree.command(
    name="clips-profile",
    description=(
        "Show your linked Clips account."
    ),
)
@app_commands.guilds(
    discord.Object(
        id=DISCORD_GUILD_ID
    )
)
async def clips_profile(
    interaction: discord.Interaction,
):
    await interaction.response.defer(
        ephemeral=True
    )

    try:
        mapping, remote = (
            await client.get_live_account(
                interaction.user.id
            )
        )

        if not mapping:
            await interaction.followup.send(
                "You don't currently have "
                "a linked Clips account.",
                ephemeral=True,
            )
            return

        status = (
            "Disabled"
            if remote.get(
                "disabled",
                False,
            )
            else "Active"
        )

        await interaction.followup.send(
            (
                f"**Username:** `{mapping[2]}`\n"
                f"**Status:** {status}\n"
                f"**Profile:** "
                f"{profile_url(mapping[2])}"
            ),
            ephemeral=True,
        )

    except FireshareError as exc:
        await interaction.followup.send(
            str(exc),
            ephemeral=True,
        )


@client.tree.command(
    name="clips-reset",
    description=(
        "Generate a fresh Clips "
        "password setup link."
    ),
)
@app_commands.guilds(
    discord.Object(
        id=DISCORD_GUILD_ID
    )
)
async def clips_reset(
    interaction: discord.Interaction,
):
    await interaction.response.defer(
        ephemeral=True
    )

    await client.send_reset_link(
        interaction
    )



# === FIRESTARTER ADMIN COMMANDS ===

@client.tree.command(
    name="clips-onboard",
    description="Admin: resend Firestarter onboarding to a member.",
)
@app_commands.guilds(
    discord.Object(id=DISCORD_GUILD_ID)
)
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    member="Member to send a fresh onboarding DM"
)
async def clips_onboard(
    interaction: discord.Interaction,
    member: discord.Member,
):
    await interaction.response.defer(
        ephemeral=True
    )

    if member.bot:
        await interaction.followup.send(
            "Firestarter will not onboard bot accounts.",
            ephemeral=True,
        )
        return

    if not client.member_has_onboarding_role(member):
        await interaction.followup.send(
            "That member does not currently have "
            "the configured onboarding role.",
            ephemeral=True,
        )
        return

    generation = await db.record_role_added(
        member.id
    )

    await client.send_onboarding_prompt(
        member,
        generation,
    )

    logger.info(
        "Manual onboarding requested "
        "admin_id=%s target_id=%s generation=%s",
        interaction.user.id,
        member.id,
        generation,
    )

    await interaction.followup.send(
        f"Firestarter attempted a fresh onboarding "
        f"DM for {member.mention}.",
        ephemeral=True,
    )


@client.tree.command(
    name="clips-unlink",
    description="Admin: remove a Discord-to-Fireshare mapping.",
)
@app_commands.guilds(
    discord.Object(id=DISCORD_GUILD_ID)
)
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    member="Member whose local Firestarter mapping should be removed"
)
async def clips_unlink(
    interaction: discord.Interaction,
    member: discord.Member,
):
    await interaction.response.defer(
        ephemeral=True
    )

    mapping = await db.get_by_discord_id(
        member.id
    )

    if not mapping:
        await interaction.followup.send(
            f"{member.mention} has no stored "
            "Firestarter account mapping.",
            ephemeral=True,
        )
        return

    rows = await db.delete_by_discord_id(
        member.id
    )

    logger.info(
        "Mapping unlinked "
        "admin_id=%s target_id=%s "
        "fireshare_username=%s rows=%s",
        interaction.user.id,
        member.id,
        mapping[2],
        rows,
    )

    await interaction.followup.send(
        f"Unlinked {member.mention} from "
        f"`{mapping[2]}`.\n\n"
        "**The Fireshare account itself was not deleted.**",
        ephemeral=True,
    )


@client.tree.command(
    name="clips-disable",
    description="Admin: disable a member's Fireshare account.",
)
@app_commands.guilds(
    discord.Object(id=DISCORD_GUILD_ID)
)
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    member="Member whose Fireshare account should be disabled"
)
async def clips_disable(
    interaction: discord.Interaction,
    member: discord.Member,
):
    await interaction.response.defer(
        ephemeral=True
    )

    try:
        mapping, remote = await client.get_live_account(
            member.id
        )

        if not mapping:
            await interaction.followup.send(
                f"{member.mention} has no active "
                "linked Clips account.",
                ephemeral=True,
            )
            return

        if remote.get("disabled", False):
            await interaction.followup.send(
                f"`{mapping[2]}` is already disabled.",
                ephemeral=True,
            )
            return

        updated = await fireshare.set_disabled(
            remote["id"],
            True,
        )

        if not updated:
            await db.delete_by_discord_id(
                member.id
            )

            await interaction.followup.send(
                "That Fireshare account no longer exists. "
                "Its stale Firestarter mapping was removed.",
                ephemeral=True,
            )
            return

        logger.info(
            "Account disabled "
            "admin_id=%s target_id=%s "
            "fireshare_user_id=%s username=%s",
            interaction.user.id,
            member.id,
            remote["id"],
            mapping[2],
        )

        await interaction.followup.send(
            f"Disabled `{mapping[2]}` for {member.mention}.",
            ephemeral=True,
        )

    except FireshareError as exc:
        await interaction.followup.send(
            str(exc),
            ephemeral=True,
        )


@client.tree.command(
    name="clips-enable",
    description="Admin: re-enable a member's Fireshare account.",
)
@app_commands.guilds(
    discord.Object(id=DISCORD_GUILD_ID)
)
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    member="Member whose Fireshare account should be enabled"
)
async def clips_enable(
    interaction: discord.Interaction,
    member: discord.Member,
):
    await interaction.response.defer(
        ephemeral=True
    )

    try:
        mapping, remote = await client.get_live_account(
            member.id
        )

        if not mapping:
            await interaction.followup.send(
                f"{member.mention} has no active "
                "linked Clips account.",
                ephemeral=True,
            )
            return

        if not remote.get("disabled", False):
            await interaction.followup.send(
                f"`{mapping[2]}` is already enabled.",
                ephemeral=True,
            )
            return

        updated = await fireshare.set_disabled(
            remote["id"],
            False,
        )

        if not updated:
            await db.delete_by_discord_id(
                member.id
            )

            await interaction.followup.send(
                "That Fireshare account no longer exists. "
                "Its stale Firestarter mapping was removed.",
                ephemeral=True,
            )
            return

        logger.info(
            "Account enabled "
            "admin_id=%s target_id=%s "
            "fireshare_user_id=%s username=%s",
            interaction.user.id,
            member.id,
            remote["id"],
            mapping[2],
        )

        await interaction.followup.send(
            f"Enabled `{mapping[2]}` for {member.mention}.",
            ephemeral=True,
        )

    except FireshareError as exc:
        await interaction.followup.send(
            str(exc),
            ephemeral=True,
        )


@client.tree.error
async def firestarter_command_error(
    interaction,
    error,
):
    if isinstance(
        error,
        app_commands.MissingPermissions,
    ):
        if interaction.response.is_done():
            await interaction.followup.send(
                "You need Discord Administrator permission "
                "to use that command.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "You need Discord Administrator permission "
                "to use that command.",
                ephemeral=True,
            )
        return

    logger.error(
        "Unhandled command error command=%s user=%s error=%r",
        getattr(interaction.command, "name", "unknown"),
        getattr(interaction.user, "id", "unknown"),
        error,
    )


client.run(
    DISCORD_TOKEN,
    log_handler=None,
)
