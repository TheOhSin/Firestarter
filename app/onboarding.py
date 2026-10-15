import asyncio
import re

import discord


USERNAME_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]{3,32}$"
)


class UsernameModal(discord.ui.Modal):
    def __init__(self, app):
        super().__init__(
            title=f"{app.public_service_name} Setup"
        )

        self.app = app

        self.username = discord.ui.TextInput(
            label="Choose your username",
            placeholder="Example: coolplayer",
            min_length=3,
            max_length=32,
            required=True,
        )

        self.add_item(self.username)

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):
        await interaction.response.defer()

        member = await self.app.resolve_member(
            interaction.user.id
        )

        if member is None:
            await interaction.followup.send(
                "I couldn't verify your Discord membership."
            )
            return

        if not self.app.member_has_onboarding_role(
            member
        ):
            await interaction.followup.send(
                "You no longer have the required "
                "Discord role. Ask an administrator "
                "to reassign it."
            )
            return

        username = str(
            self.username.value
        ).strip()

        if not USERNAME_PATTERN.fullmatch(
            username
        ):
            await interaction.followup.send(
                "Username must be 3-32 characters "
                "and may contain only letters, numbers, "
                "`_`, `-`, and `.`."
            )
            return

        lock = (
            self.app.registration_locks.setdefault(
                interaction.user.id,
                asyncio.Lock(),
            )
        )

        if lock.locked():
            await interaction.followup.send(
                "Your registration is already "
                "being processed."
            )
            return

        async with lock:
            await self.app.handle_registration(
                interaction,
                member,
                username,
            )


class StartOnboardingView(
    discord.ui.View
):
    def __init__(self, app):
        super().__init__(
            timeout=None
        )

        self.app = app

    @discord.ui.button(
        label="Create Clips Account",
        style=discord.ButtonStyle.primary,
        custom_id=(
            "firestarter:create_account"
        ),
    )
    async def create_account(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        member = await self.app.resolve_member(
            interaction.user.id
        )

        if member is None:
            await interaction.response.send_message(
                "I couldn't verify your "
                "Discord membership."
            )
            return

        if not self.app.member_has_onboarding_role(
            member
        ):
            await interaction.response.send_message(
                "You no longer have the required "
                "Discord role."
            )
            return

        await interaction.response.send_modal(
            UsernameModal(self.app)
        )


class ResetAccountView(
    discord.ui.View
):
    def __init__(self, app):
        super().__init__(
            timeout=None
        )

        self.app = app

    @discord.ui.button(
        label="Get Password Setup Link",
        style=discord.ButtonStyle.secondary,
        custom_id=(
            "firestarter:reset_account"
        ),
    )
    async def reset_account(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer()

        await self.app.send_reset_link(
            interaction
        )
