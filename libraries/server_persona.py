import base64
import discord
from discord import app_commands
from discord.ext import commands


# 🌸 SERVER PERSONA COG — lets "Cutest Thing" wear a different nickname,
# avatar, and bio per server, using Discord's "Modify Current Member"
# endpoint (PATCH /guilds/{guild_id}/members/@me). Bots have been able to
# set nick this way forever; avatar + bio support was added by Discord on
# 2025-09-10. Note: there's no such thing as a per-guild USERNAME for
# bots — only nickname. If you want the bot's global @username to change
# too, that's a separate call (bot.user.edit(username=...)) and applies
# everywhere, not per-server.
#
# Uses bot.http (discord.py's internal HTTPClient) directly via a raw
# Route instead of any high-level wrapper, so this works regardless of
# whether your installed discord.py version has caught up with the 2025
# API addition yet.

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # 🌸 8 MB cap on avatar/banner uploads


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

    # ── /server-persona set ─────────────────────────────────────────────
    @app_commands.command(
        name="server-persona-set",
        description="🌸 Give the bot a custom nickname/avatar/banner/bio for THIS server only",
    )
    @app_commands.describe(
        nickname="New nickname for this server (leave blank to keep current)",
        avatar="New avatar image for this server, max 8MB (leave blank to keep current)",
        banner="New banner image for this server, max 8MB (leave blank to keep current)",
        bio="New bio/about-me for this server (leave blank to keep current)",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def server_persona_set(
        self,
        interaction: discord.Interaction,
        nickname: str | None = None,
        avatar: discord.Attachment | None = None,
        banner: discord.Attachment | None = None,
        bio: str | None = None,
    ):
        if nickname is None and avatar is None and banner is None and bio is None:
            await interaction.response.send_message(
                "Gimme at least one of nickname / avatar / banner / bio to change! 🌸",
                ephemeral=True,
            )
            return

        # 🌸 Check attachment sizes before doing any work
        for attachment in (avatar, banner):
            if attachment is not None:
                size_error = self._check_size(attachment)
                if size_error:
                    await interaction.response.send_message(size_error, ephemeral=True)
                    return

        await interaction.response.defer(ephemeral=True, thinking=True)

        payload: dict = {}
        if nickname is not None:
            payload["nick"] = nickname
        if bio is not None:
            payload["bio"] = bio
        if avatar is not None:
            try:
                payload["avatar"] = await self._to_data_uri(avatar)
            except Exception as e:
                await interaction.followup.send(f"⚠️ Couldn't read that avatar image: {e}", ephemeral=True)
                return
        if banner is not None:
            try:
                payload["banner"] = await self._to_data_uri(banner)
            except Exception as e:
                await interaction.followup.send(f"⚠️ Couldn't read that banner image: {e}", ephemeral=True)
                return

        try:
            await self._patch_current_member(interaction.guild_id, **payload)
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
                ("avatar", avatar),
                ("banner", banner),
                ("bio", bio),
            )
            if val is not None
        )
        await interaction.followup.send(f"✅ Updated my {changed} for this server! 🎀✨", ephemeral=True)

    # ── /server-persona reset ───────────────────────────────────────────
    @app_commands.command(
        name="server-persona-reset",
        description="🌸 Reset the bot's nickname/avatar/banner/bio in THIS server back to global defaults",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def server_persona_reset(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            # 🌸 Setting each field to None clears the per-guild override
            # so the member profile falls back to the bot's global
            # username/avatar/banner/bio.
            await self._patch_current_member(
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

        await interaction.followup.send("🔄 Reset back to my global look for this server! 🌸", ephemeral=True)

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
