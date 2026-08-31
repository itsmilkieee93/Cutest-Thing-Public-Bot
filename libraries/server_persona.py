import base64
import discord
from discord import app_commands, ui
from discord.ext import commands


# 🌸 SERVER PERSONA COG — lets "Cutest Thing" wear a different nickname,
# avatar, banner, and bio per server, using Discord's "Modify Current
# Member" endpoint (PATCH /guilds/{guild_id}/members/@me). Bots have been
# able to set nick this way forever; avatar + bio support was added by
# Discord on 2025-09-10. Note: there's no such thing as a per-guild
# USERNAME for bots — only nickname. If you want the bot's global
# @username to change too, that's a separate call
# (bot.user.edit(username=...)) and applies everywhere, not per-server.
#
# Uses bot.http (discord.py's internal HTTPClient) directly via a raw
# Route instead of any high-level wrapper, so this works regardless of
# whether your installed discord.py version has caught up with the 2025
# API addition yet.
#
# 🌸 UI NOTE (updated): everything now lives in ONE modal — nickname,
# bio, AND avatar/banner. Discord shipped a native file-upload component
# for modals (type 19, `discord.ui.FileUpload`) on 2025-09-28, so the old
# "attachments can't go in modals" limitation is gone. Requires
# discord.py >= 2.7. FileUpload items must be wrapped in `discord.ui.Label`
# (same as how the new-style TextInput fields are declared) — the file
# itself is never sent as bytes to your bot in the payload, you get back
# `discord.Attachment` objects on the resolved values and read() them
# same as before. No more slash-command Attachment options needed at all,
# so /server-persona-set now takes zero arguments and just pops the modal.

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # 🌸 8 MB cap on avatar/banner uploads
BIO_MAX_LENGTH = 190  # 🌸 Discord's member bio length cap


class ServerPersonaModal(ui.Modal, title="🌸 Server Persona"):
    """🌸 Single popup form for everything — nickname, bio, avatar, and
    banner. Pre-fills nickname/bio with the bot's current values so you
    can see what you're editing."""

    nickname = ui.Label(
        text="Nickname",
        description="Leave blank to keep current nickname",
        component=ui.TextInput(required=False, max_length=32),
    )
    bio = ui.Label(
        text="Bio / About Me",
        description="Leave blank to keep current bio",
        component=ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=BIO_MAX_LENGTH,
        ),
    )
    avatar = ui.Label(
        text="Avatar",
        description="New avatar for this server, max 8MB (leave blank to keep current)",
        component=ui.FileUpload(min_values=0, max_values=1, required=False),
    )
    banner = ui.Label(
        text="Banner",
        description="New banner for this server, max 8MB (leave blank to keep current)",
        component=ui.FileUpload(min_values=0, max_values=1, required=False),
    )

    def __init__(
        self,
        cog: "ServerPersona",
        current_nick: str | None,
        current_bio: str | None,
    ):
        super().__init__()
        self.cog = cog
        # 🌸 Pre-fill so people can see/edit what's already set instead of
        # staring at a blank box. (No pre-fill exists for file uploads —
        # Discord doesn't support that.)
        if current_nick:
            self.nickname.component.default = current_nick
        if current_bio:
            self.bio.component.default = current_bio

    async def on_submit(self, interaction: discord.Interaction):
        nickname = self.nickname.component.value.strip() or None
        bio = self.bio.component.value.strip() or None
        # 🌸 FileUpload.values is a list of discord.Attachment (empty list
        # if the user didn't attach anything, since min_values=0).
        avatar_file = self.avatar.component.values[0] if self.avatar.component.values else None
        banner_file = self.banner.component.values[0] if self.banner.component.values else None

        if nickname is None and bio is None and avatar_file is None and banner_file is None:
            await interaction.response.send_message(
                "Gimme at least one of nickname / avatar / banner / bio to change! 🌸",
                ephemeral=True,
            )
            return

        # 🌸 Size-check uploads now that we actually have them (can't check
        # before the modal opens anymore, since there's no slash-command
        # option to inspect ahead of time).
        for label, attachment in (("avatar", avatar_file), ("banner", banner_file)):
            if attachment is not None:
                size_error = self.cog._check_size(attachment)
                if size_error:
                    await interaction.response.send_message(size_error, ephemeral=True)
                    return

        await interaction.response.defer(ephemeral=True, thinking=True)

        payload: dict = {}
        if nickname is not None:
            payload["nick"] = nickname
        if bio is not None:
            payload["bio"] = bio
        if avatar_file is not None:
            try:
                payload["avatar"] = await self.cog._to_data_uri(avatar_file)
            except Exception as e:
                await interaction.followup.send(f"⚠️ Couldn't read that avatar image: {e}", ephemeral=True)
                return
        if banner_file is not None:
            try:
                payload["banner"] = await self.cog._to_data_uri(banner_file)
            except Exception as e:
                await interaction.followup.send(f"⚠️ Couldn't read that banner image: {e}", ephemeral=True)
                return

        try:
            await self.cog._patch_current_member(interaction.guild_id, **payload)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Discord rejected that update: `{e.text if hasattr(e, 'text') else e}`",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.followup.send(f"⚠️ Something went wrong: {e}", ephemeral=True)
            return

        changed = ", ".join(
            label
            for label, val in (
                ("nickname", nickname),
                ("avatar", avatar_file),
                ("banner", banner_file),
                ("bio", bio),
            )
            if val is not None
        )
        await interaction.followup.send(f"✅ Updated my {changed} for this server! 🎀✨", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        msg = f"⚠️ Something went wrong with that form: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class ServerPersonaResetView(ui.View):
    """🌸 Confirm-before-you-wreck-it buttons for the reset command."""

    def __init__(self, cog: "ServerPersona"):
        super().__init__(timeout=60)
        self.cog = cog

    @ui.button(label="Reset", style=discord.ButtonStyle.danger, emoji="🔄")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # 🌸 Setting each field to None clears the per-guild override
            # so the member profile falls back to the bot's global
            # username/avatar/banner/bio.
            await self.cog._patch_current_member(
                interaction.guild_id,
                nick=None,
                avatar=None,
                banner=None,
                bio=None,
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Discord rejected the reset: `{e.text if hasattr(e, 'text') else e}`",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.followup.send(f"⚠️ Something went wrong: {e}", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.followup.send("🔄 Reset back to my global look for this server! 🌸", ephemeral=True)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Okay, left everything as-is! 🌸", view=self)
        self.stop()


class ServerPersona(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── low-level PATCH helper ──────────────────────────────────────────
    async def _patch_current_member(self, guild_id: int, **fields) -> dict:
        """🌸 PATCH /guilds/{guild_id}/members/@me with only the given
        fields. Pass a value of None for a field to explicitly clear it
        (e.g. avatar=None resets to the global avatar)."""
        route = discord.http.Route(
            "PATCH",
            "/guilds/{guild_id}/members/@me",
            guild_id=guild_id,
        )
        return await self.bot.http.request(route, json=fields)

    @staticmethod
    def _check_size(attachment: discord.Attachment) -> str | None:
        """🌸 Returns an error message if the attachment is too big, else None."""
        if attachment.size > MAX_ATTACHMENT_BYTES:
            return (
                f"⚠️ `{attachment.filename}` is {attachment.size / 1024 / 1024:.1f}MB — "
                f"max allowed is 8MB!"
            )
        return None

    @staticmethod
    async def _to_data_uri(attachment: discord.Attachment) -> str:
        """🌸 Discord wants avatar/banner as a base64 data URI string, not
        raw bytes or a URL."""
        raw = await attachment.read()
        mime = attachment.content_type or "image/png"
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"

    def _current_persona(self, guild: discord.Guild | None) -> tuple[str | None, str | None]:
        """🌸 Best-effort read of the bot's current per-guild nick/bio from
        cache, just to pre-fill the modal. Bio isn't cached by discord.py's
        Member object, so that one stays blank unless you're tracking it
        yourself elsewhere."""
        if guild is None:
            return None, None
        me = guild.me
        return (me.nick if me else None), None

    # ── /server-persona set ─────────────────────────────────────────────
    @app_commands.command(
        name="server-persona-set",
        description="🌸 Give the bot a custom nickname/avatar/banner/bio for THIS server only",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def server_persona_set(self, interaction: discord.Interaction):
        # 🌸 No more Attachment options here — avatar/banner are collected
        # inside the modal itself now via FileUpload, so this command has
        # nothing left to validate up front. Straight to the modal.
        current_nick, current_bio = self._current_persona(interaction.guild)
        modal = ServerPersonaModal(self, current_nick, current_bio)
        await interaction.response.send_modal(modal)

    # ── /server-persona reset ───────────────────────────────────────────
    @app_commands.command(
        name="server-persona-reset",
        description="🌸 Reset the bot's nickname/avatar/banner/bio in THIS server back to global defaults",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def server_persona_reset(self, interaction: discord.Interaction):
        view = ServerPersonaResetView(self)
        await interaction.response.send_message(
            "Reset my nickname, avatar, banner, and bio back to global defaults for this server?",
            view=view,
            ephemeral=True,
        )

    @server_persona_set.error
    @server_persona_reset.error
    async def _persona_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Nahhhh, you need **Moderator** permission (Timeout Members) to do that!!! 😭🙏"
        else:
            msg = f"⚠️ Unexpected error: {error}"

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerPersona(bot))
