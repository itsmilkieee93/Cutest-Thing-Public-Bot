import base64
import os
import aiosqlite
import discord
from discord import app_commands, ui
from discord.ext import commands


# 🌸 SERVER PERSONA COG — lets "Cutest Thing" wear a different nickname,
# avatar, banner, bio, AND Display Name Style (font/effect/colors) per
# server, using Discord's "Modify Current Member" endpoint
# (PATCH /guilds/{guild_id}/members/@me). Bots have been able to set nick
# this way forever; avatar + bio support was added by Discord on
# 2025-09-10. Note: there's no such thing as a per-guild USERNAME for
# bots — only nickname. If you want the bot's global @username to change
# too, that's a separate call (bot.user.edit(username=...)) and applies
# everywhere, not per-server.
#
# Uses bot.http (discord.py's internal HTTPClient) directly via a raw
# Route instead of any high-level wrapper, so this works regardless of
# whether your installed discord.py version has caught up with the API
# additions yet.
#

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # 🌸 8 MB cap on avatar/banner uploads
BIO_MAX_LENGTH = 190  # 🌸 Discord's member bio length cap
DB_PATH_TEMPLATE = "interactions/persona/{guild_id}/custom_{guild_id}.db"  # 🌸 one SQLite file per guild

# 🌸 DM personality overrides — one db file PER DM CHANNEL, matching the
# per-guild pattern exactly (each guild gets its own file; each DM
# channel gets its own file too). Keyed by channel_id, not user_id —
# in a 1:1 bot DM these end up being the same person either way, but
# channel_id is the correct scope key since it mirrors guild_id's role
# (the container the override belongs to), not who's inside it.
DM_DB_PATH_TEMPLATE = "interactions/persona/dm/{channel_id}/dm_{channel_id}.db"

# 🌸 PRESET PERSONAS — {key: (label, instructions)}. `instructions` gets
# dropped straight into the system prompt in place of the default
# PERSONALITY_TEMPLATE body (personality.py handles the actual splice).
# Keep each one self-contained (doesn't reference "the template") since
# it fully REPLACES the default rather than layering on top of it.
PRESET_PERSONAS: dict[str, tuple[str, str]] = {
    "kawaii": (
        "🌸 Kawaii (default)",
        "keep replies short and casual, like texting a friend. sprinkle in "
        "kaomoji and cutesy energy (☆ﾟ.*･｡ﾟ vibes) without overdoing it. "
        "sweet and a little bubbly, but still has real opinions instead of "
        "just being agreeable.",
    ),
    "tsundere": (
        "😤 Tsundere",
        "act a little cold/annoyed on the surface but obviously still cares — "
        "classic 'it's not like I wanted to help you or anything' energy. "
        "short, blunt replies, occasional huffing (hmph), but comes through "
        "for people anyway. never actually mean.",
    ),
    "chill": (
        "😎 Chill",
        "laid-back, low-key, unbothered tone. short replies, dry humor, "
        "doesn't get worked up about much. talks like a chill friend who's "
        "seen it all, not a hype machine.",
    ),
    "genki": (
        "🎉 Genki (hyper)",
        "big energy, enthusiastic, lots of exclamation points and excitement "
        "about everything. hypes people up, celebrates small wins loudly, "
        "very 'let's gooo' energy. still keep individual replies short.",
    ),
    "calm": (
        "🕊️ Calm & mature",
        "soft-spoken, thoughtful, a little more formal but still warm. "
        "takes a beat before responding, gives measured/grounded replies "
        "instead of reacting instantly. reassuring presence, not stiff or "
        "corporate.",
    ),
}
DEFAULT_PRESET_KEY = "kawaii"

# 🌸 Font IDs 1-16, straight from the spec. Label text mirrors Discord's
# own modal wording where known (11 = gg sans, 12 = Tempo, etc.).
FONT_OPTIONS: list[tuple[int, str]] = [
    (1, "Catberry"),
    (2, "Whisper"),
    (3, "Sakura"),
    (4, "Fjalla One"),
    (5, "Bangers"),
    (6, "Boldonse"),
    (7, "Splash"),
    (8, "8Bit"),
    (9, "Marker Felt"),
    (10, "Vampyre"),
    (11, "gg sans"),
    (12, "Tempo"),  # aka Zilla Slab in some builds
    (13, "MedievalSharp"),
    (14, "Monument"),
    (15, "Ojuju"),
    (16, "Playpen Sans"),
]

# 🌸 Effect IDs 1-7, per spec.
EFFECT_OPTIONS: list[tuple[int, str]] = [
    (1, "Solid"),
    (2, "Gradient"),
    (3, "Neon"),
    (4, "Toon"),
    (5, "Pop"),
    (6, "Gummy"),
    (7, "Prism"),
]


def _hex_pair_to_ints(raw: str) -> list[int]:
    """🌸 Parses the manual 'ffffff-000000' hyphenated hex input into a
    list of up to 2 decimal ints. Strips stray '#' if someone pastes
    with it. Raises ValueError with a user-friendly message on bad
    input so the caller can just show it back to them."""
    raw = raw.strip()
    if not raw:
        return []

    parts = [p.strip().lstrip("#") for p in raw.split("-") if p.strip()]
    if len(parts) > 2:
        raise ValueError("Only 1-2 colors allowed (format: `ffffff-000000`).")

    ints: list[int] = []
    for part in parts:
        if len(part) != 6 or any(c not in "0123456789abcdefABCDEF" for c in part):
            raise ValueError(f"`{part}` isn't a valid 6-digit hex code.")
        ints.append(int(part, 16))
    return ints


def _ints_to_hex_pair(ints: list[int] | None) -> str:
    """🌸 Reverse of the above, for pre-filling the modal field."""
    if not ints:
        return ""
    return "-".join(f"{n:06x}" for n in ints)


class ServerPersonaProfileModal(ui.Modal, title="🌸 Profile Settings"):
    """🌸 Modal for nickname, bio, avatar, and banner (4 components)."""

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
        if current_nick:
            self.nickname.component.default = current_nick
        if current_bio:
            self.bio.component.default = current_bio

    async def on_submit(self, interaction: discord.Interaction):
        nickname = self.nickname.component.value.strip() or None
        bio = self.bio.component.value.strip() or None
        avatar_file = self.avatar.component.values[0] if self.avatar.component.values else None
        banner_file = self.banner.component.values[0] if self.banner.component.values else None

        if nickname is None and bio is None and avatar_file is None and banner_file is None:
            await interaction.response.send_message(
                "Gimme at least one of nickname / avatar / banner / bio to change! 🌸",
                ephemeral=True,
            )
            return

        for label, attachment in (("avatar", avatar_file), ("banner", banner_file)):
            if attachment is not None:
                size_error = self.cog._check_size(attachment)
                if size_error:
                    await interaction.response.send_message(size_error, ephemeral=True)
                    return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if bio is not None:
            db = await self.cog._get_db(interaction.guild_id)
            await db.execute(
                """
                INSERT INTO server_bios (guild_id, bot_id, bio)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    bio = COALESCE(excluded.bio, server_bios.bio),
                    bot_id = excluded.bot_id
                """,
                (interaction.guild_id, interaction.client.user.id, bio),
            )
            await db.commit()

        payload: dict = {}
        if nickname is not None:
            payload["nick"] = nickname
        if bio is not None:
            payload["bio"] = bio
        if avatar_file is not None:
            try:
                payload["avatar"] = await self.cog._to_data_uri(avatar_file)
            except Exception as e:
                await interaction.followup.send(f"⚠️ Couldn't read avatar image: {e}", ephemeral=True)
                return
        if banner_file is not None:
            try:
                payload["banner"] = await self.cog._to_data_uri(banner_file)
            except Exception as e:
                await interaction.followup.send(f"⚠️ Couldn't read banner image: {e}", ephemeral=True)
                return

        try:
            await self.cog._patch_current_member(interaction.guild_id, **payload)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Discord rejected that update: `{e.text if hasattr(e, 'text') else e}`",
                ephemeral=True,
            )
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


class ServerPersonaStyleModal(ui.Modal, title="✨ Display Name Style"):
    """🌸 Modal for font, effect, and color styling (3 components)."""

    font = ui.Label(
        text="Font",
        description="The display name's font.",
        component=ui.Select(
            options=[
                discord.SelectOption(label=label, value=str(font_id))
                for font_id, label in FONT_OPTIONS
            ],
            required=False,
            min_values=0,
            max_values=1,
        ),
    )
    effect = ui.Label(
        text="Effect",
        description="The display name's effect.",
        component=ui.Select(
            options=[
                discord.SelectOption(label=label, value=str(effect_id))
                for effect_id, label in EFFECT_OPTIONS
            ],
            required=False,
            min_values=0,
            max_values=1,
        ),
    )
    colors = ui.Label(
        text="Color(s)",
        description="Format: ffffff-000000 (Primary-Accent)",
        component=ui.TextInput(required=False, max_length=15, placeholder="ffffff-000000"),
    )

    def __init__(
        self,
        cog: "ServerPersona",
        current_font_id: int | None = None,
        current_effect_id: int | None = None,
        current_colors: list[int] | None = None,
    ):
        super().__init__()
        self.cog = cog
        if current_font_id is not None:
            for option in self.font.component.options:
                option.default = option.value == str(current_font_id)
        if current_effect_id is not None:
            for option in self.effect.component.options:
                option.default = option.value == str(current_effect_id)
        if current_colors:
            self.colors.component.default = _ints_to_hex_pair(current_colors)

    async def on_submit(self, interaction: discord.Interaction):
        font_id = int(self.font.component.values[0]) if self.font.component.values else None
        effect_id = int(self.effect.component.values[0]) if self.effect.component.values else None

        raw_colors = self.colors.component.value.strip()
        try:
            color_ints = _hex_pair_to_ints(raw_colors)
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return

        if font_id is None and effect_id is None and not color_ints:
            await interaction.response.send_message(
                "Pick at least a font, effect, or color pair to update! ✨",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        db = await self.cog._get_db(interaction.guild_id)
        await db.execute(
            """
            INSERT INTO server_bios (guild_id, bot_id, font_id, effect_id, primary_color, accent_color)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                bot_id = excluded.bot_id,
                font_id = COALESCE(excluded.font_id, server_bios.font_id),
                effect_id = COALESCE(excluded.effect_id, server_bios.effect_id),
                primary_color = COALESCE(excluded.primary_color, server_bios.primary_color),
                accent_color = COALESCE(excluded.accent_color, server_bios.accent_color)
            """,
            (
                interaction.guild_id,
                interaction.client.user.id,
                font_id,
                effect_id,
                color_ints[0] if len(color_ints) > 0 else None,
                color_ints[1] if len(color_ints) > 1 else None,
            ),
        )
        await db.commit()

        payload: dict = {}
        if font_id is not None:
            payload["display_name_font_id"] = font_id
        if effect_id is not None:
            payload["display_name_effect_id"] = effect_id
        if color_ints:
            payload["display_name_colors"] = color_ints

        try:
            await self.cog._patch_current_member(interaction.guild_id, **payload)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Discord rejected that style update: `{e.text if hasattr(e, 'text') else e}`",
                ephemeral=True,
            )
            return

        await interaction.followup.send("✅ Updated my Display Name Style! 🎨✨", ephemeral=True)


class ConfirmResetView(ui.View):
    """🌸 Confirm-before-you-wreck-it buttons for the reset commands."""

    def __init__(self, cog: "ServerPersona", reset_type: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.reset_type = reset_type  # 'profile' or 'style'

    @ui.button(label="Reset", style=discord.ButtonStyle.danger, emoji="🔄")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # 1. Prepare the payload based on what we are resetting
        payload = {}
        if self.reset_type == "profile":
            payload = {"nick": None, "avatar": None, "banner": None, "bio": None}
        elif self.reset_type == "style":
            payload = {
                "display_name_font_id": None,
                "display_name_effect_id": None,
                "display_name_colors": None
            }     

        # 2. Send the reset payload to Discord
        try:
            await self.cog._patch_current_member(interaction.guild_id, **payload)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Discord rejected the reset: `{e.text if hasattr(e, 'text') else e}`",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.followup.send(f"⚠️ Something went wrong: {e}", ephemeral=True)
            return

        # 3. Wipe ONLY the relevant fields from our database
        db = await self.cog._get_db(interaction.guild_id)
        if self.reset_type == "profile":
            await db.execute(
                "UPDATE server_bios SET bio = NULL WHERE guild_id = ?",
                (interaction.guild_id,),
            )
        elif self.reset_type == "style":
            await db.execute(
                """
                UPDATE server_bios 
                SET font_id = NULL, effect_id = NULL, primary_color = NULL, accent_color = NULL 
                WHERE guild_id = ?
                """,
                (interaction.guild_id,),
            )
        await db.commit()

        # 4. Overwrite the ORIGINAL confirm message with the success
        # text and remove the buttons entirely (view=None) — instead of
        # disabling them in place and sending a separate followup,
        # which left the "are you sure?" message sitting there stale
        # underneath a new confirmation. Matches the same fix applied
        # to persona-set/persona-view/persona-reset below.
        if self.reset_type == "profile":
            msg = "🔄 Reset my profile (nick/avatar/banner/bio) back to global defaults! 🌸"
        else:
            msg = "🔄 Reset my Display Name Style (font/effect/colors) back to normal! ✨"

        await interaction.edit_original_response(content=msg, view=None)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="Okay, left everything as-is! 🌸", view=None)
        self.stop()


class PersonaCustomTextModal(ui.Modal, title="✍️ Custom AI Personality"):
    """🌸 Free-text modal for a fully custom personality, used by both the
    guild (/persona-set custom) and DM (/persona-set custom) flows. The
    caller passes an on_submit callback since what happens after differs
    (save to guild db vs. save to DM db)."""

    instructions = ui.Label(
        text="Personality instructions",
        description="Describe how the bot should act/talk. Replaces the default personality entirely.",
        component=ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1500,
            placeholder="e.g. speak like a pirate, always offer snack recommendations, never use emoji...",
        ),
    )

    def __init__(self, on_submit_callback, current_text: str | None = None):
        super().__init__()
        self._on_submit_callback = on_submit_callback
        if current_text:
            self.instructions.component.default = current_text

    async def on_submit(self, interaction: discord.Interaction):
        text = self.instructions.component.value.strip()
        await self._on_submit_callback(interaction, text)


class PersonaPresetSelect(ui.Select):
    """🌸 Dropdown of preset personas + a 'Custom...' entry. Shared by both
    the guild and DM /persona-set flows via the on_pick callback."""

    def __init__(self, on_pick_callback, current_preset_key: str | None):
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                default=(key == current_preset_key),
            )
            for key, (label, _instructions) in PRESET_PERSONAS.items()
        ]
        options.append(
            discord.SelectOption(
                label="✍️ Custom...",
                value="custom",
                default=(current_preset_key == "custom"),
                description="Write your own personality from scratch",
            )
        )
        super().__init__(placeholder="Choose a personality...", options=options)
        self._on_pick_callback = on_pick_callback

    async def callback(self, interaction: discord.Interaction):
        await self._on_pick_callback(interaction, self.values[0])


class PersonaPickView(ui.View):
    """🌸 Wraps PersonaPresetSelect so /persona-set can send one message
    with a dropdown instead of needing a subcommand per preset."""

    def __init__(self, on_pick_callback, current_preset_key: str | None):
        super().__init__(timeout=120)
        self.add_item(PersonaPresetSelect(on_pick_callback, current_preset_key))


class ConfirmResetPersonaView(ui.View):
    """🌸 Same confirm/cancel shape as ConfirmResetView above, but for the
    AI personality reset (guild or DM) instead of profile/style."""

    def __init__(self, on_confirm_callback):
        super().__init__(timeout=60)
        self._on_confirm_callback = on_confirm_callback

    @ui.button(label="Reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True
        await self._on_confirm_callback(interaction)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Okay, left the personality as-is! 🌸", view=self)
        self.stop()


class PersonaEditButtonView(ui.View):
    """🌸 Single "✏️ Edit" button shown under /persona-view's reply when
    the current personality is custom text — opens the same modal as
    /persona-set's "Custom..." option, pre-filled with the text pulled
    straight from the db, so tweaking wording doesn't require going
    back through the dropdown flow. Guild edits still enforce the
    Moderator permission check on click (not just on the original
    /persona-view call) since the button can sit in chat a while."""

    def __init__(self, on_edit_callback, require_moderator: bool):
        super().__init__(timeout=180)
        self._on_edit_callback = on_edit_callback
        self._require_moderator = require_moderator

    @ui.button(label="✏️ Edit", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: ui.Button):
        if self._require_moderator:
            member = interaction.user
            if not isinstance(member, discord.Member) or not member.guild_permissions.moderate_members:
                await interaction.response.send_message(
                    "Nahhhh, you need **Moderator** permission (Timeout Members) to do that!!! 😭🙏",
                    ephemeral=True,
                )
                return
        await self._on_edit_callback(interaction)


class ServerPersona(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 🌸 One aiosqlite connection per guild, opened lazily and cached
        # here since each guild gets its own db file on disk.
        self._db_cache: dict[int, aiosqlite.Connection] = {}
        # 🌸 One aiosqlite connection per DM CHANNEL, same shape as
        # _db_cache above — each DM channel gets its own db file too.
        self._dm_db_cache: dict[int, aiosqlite.Connection] = {}

    async def cog_load(self):
        # 🌸 Nothing to open up front — connections are per-guild/DM and
        # opened on first use via _get_db()/_get_dm_db().
        pass

    async def cog_unload(self):
        # 🌸 Close every connection we've opened so we don't leak handles
        # on cog reload/unload.
        for conn in self._db_cache.values():
            await conn.close()
        self._db_cache.clear()
        for conn in self._dm_db_cache.values():
            await conn.close()
        self._dm_db_cache.clear()

    async def _get_db(self, guild_id: int) -> aiosqlite.Connection:
        """🌸 Returns the aiosqlite connection for this guild's own db
        file, opening it (and creating the folder + table if needed) the
        first time it's touched."""
        if guild_id in self._db_cache:
            return self._db_cache[guild_id]

        db_path = DB_PATH_TEMPLATE.format(guild_id=guild_id)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        conn = await aiosqlite.connect(db_path)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS server_bios (
                guild_id INTEGER PRIMARY KEY,
                bot_id INTEGER NOT NULL,
                bio TEXT,
                font_id INTEGER,
                effect_id INTEGER,
                primary_color INTEGER,
                accent_color INTEGER
            )
            """
        )
        # 🌸 Best-effort migration for existing DBs created before the
        # name-style columns existed. Ignored if they're already there.
        for column, coltype in (
            ("font_id", "INTEGER"),
            ("effect_id", "INTEGER"),
            ("primary_color", "INTEGER"),
            ("accent_color", "INTEGER"),
        ):
            try:
                await conn.execute(f"ALTER TABLE server_bios ADD COLUMN {column} {coltype}")
            except aiosqlite.OperationalError:
                pass  # 🌸 column already exists

        # 🌸 AI personality override for this guild — separate table, same
        # db file. preset_key is one of PRESET_PERSONAS or "custom"; when
        # "custom", custom_text holds the actual instructions. NULL/no row
        # means "use the default template" (personality.py's own fallback).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_persona (
                guild_id INTEGER PRIMARY KEY,
                preset_key TEXT NOT NULL,
                custom_text TEXT,
                set_by INTEGER
            )
            """
        )
        await conn.commit()

        self._db_cache[guild_id] = conn
        return conn

    async def _get_dm_db(self, channel_id: int) -> aiosqlite.Connection:
        """🌸 Returns the aiosqlite connection for this DM channel's own
        db file — mirrors _get_db(guild_id) above exactly, just scoped
        to channel_id instead of guild_id. Opens/creates it (folder +
        table) the first time this channel is touched."""
        if channel_id in self._dm_db_cache:
            return self._dm_db_cache[channel_id]

        db_path = DM_DB_PATH_TEMPLATE.format(channel_id=channel_id)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        conn = await aiosqlite.connect(db_path)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_ai_persona (
                channel_id INTEGER PRIMARY KEY,
                preset_key TEXT NOT NULL,
                custom_text TEXT
            )
            """
        )
        await conn.commit()
        self._dm_db_cache[channel_id] = conn
        return conn

    # ── AI personality resolve/set/reset (guild + DM) ───────────────────
    async def get_guild_ai_persona(self, guild_id: int) -> str | None:
        """🌸 Returns the assembled instructions text for this guild's AI
        personality override, or None if no override is set (caller should
        fall back to the default template). Called from personality.py."""
        conn = await self._get_db(guild_id)
        async with conn.execute(
            "SELECT preset_key, custom_text FROM ai_persona WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        preset_key, custom_text = row
        if preset_key == "custom":
            return custom_text
        preset = PRESET_PERSONAS.get(preset_key)
        return preset[1] if preset else None

    async def get_dm_ai_persona(self, channel_id: int) -> str | None:
        """🌸 Same as get_guild_ai_persona but for a DM channel's own
        override. Returns None if this channel hasn't set one."""
        conn = await self._get_dm_db(channel_id)
        async with conn.execute(
            "SELECT preset_key, custom_text FROM dm_ai_persona WHERE channel_id = ?",
            (channel_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        preset_key, custom_text = row
        if preset_key == "custom":
            return custom_text
        preset = PRESET_PERSONAS.get(preset_key)
        return preset[1] if preset else None

    async def set_guild_ai_persona(
        self, guild_id: int, set_by: int, preset_key: str, custom_text: str | None = None
    ) -> None:
        conn = await self._get_db(guild_id)
        await conn.execute(
            """
            INSERT INTO ai_persona (guild_id, preset_key, custom_text, set_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                preset_key = excluded.preset_key,
                custom_text = excluded.custom_text,
                set_by = excluded.set_by
            """,
            (guild_id, preset_key, custom_text, set_by),
        )
        await conn.commit()

    async def set_dm_ai_persona(
        self, channel_id: int, preset_key: str, custom_text: str | None = None
    ) -> None:
        conn = await self._get_dm_db(channel_id)
        await conn.execute(
            """
            INSERT INTO dm_ai_persona (channel_id, preset_key, custom_text)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                preset_key = excluded.preset_key,
                custom_text = excluded.custom_text
            """,
            (channel_id, preset_key, custom_text),
        )
        await conn.commit()

    async def reset_guild_ai_persona(self, guild_id: int) -> None:
        conn = await self._get_db(guild_id)
        await conn.execute("DELETE FROM ai_persona WHERE guild_id = ?", (guild_id,))
        await conn.commit()

    async def reset_dm_ai_persona(self, channel_id: int) -> None:
        conn = await self._get_dm_db(channel_id)
        await conn.execute("DELETE FROM dm_ai_persona WHERE channel_id = ?", (channel_id,))
        await conn.commit()

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

    async def _current_persona(
        self, guild: discord.Guild | None
    ) -> tuple[str | None, str | None, int | None, int | None, list[int] | None]:
        """🌸 Best-effort read of the bot's current per-guild nick/bio/
        name-style. Nick comes from discord.py's Member cache; the rest
        isn't cached there at all, so we pull our own stored values from
        server_bios instead."""
        if guild is None:
            return None, None, None, None, None
        me = guild.me
        nick = me.nick if me else None

        bio = None
        font_id = None
        effect_id = None
        colors: list[int] | None = None
        db = await self._get_db(guild.id)
        async with db.execute(
            "SELECT bio, font_id, effect_id, primary_color, accent_color FROM server_bios WHERE guild_id = ?",
            (guild.id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is not None:
                bio, font_id, effect_id, primary_color, accent_color = row
                if primary_color is not None or accent_color is not None:
                    colors = [c for c in (primary_color, accent_color) if c is not None]

        return nick, bio, font_id, effect_id, colors

    async def get_persona_colors(self, guild_id: int) -> list[int] | None:
        """🌸 Public helper for your bio-card renderer (or anything else
        that wants to match the member's chosen Display Name Style
        colors) — reads the same cached values the modal pre-fills from,
        without going through Discord's API. Returns None if no colors
        are set for this guild."""
        db = await self._get_db(guild_id)
        async with db.execute(
            "SELECT primary_color, accent_color FROM server_bios WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            primary_color, accent_color = row
            colors = [c for c in (primary_color, accent_color) if c is not None]
            return colors or None

    # ── /server-persona-profile ─────────────────────────────────────────
    @app_commands.command(
        name="server-persona-profile",
        description="🌸 Set nickname, avatar, banner, or bio for THIS server",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def server_persona_profile(self, interaction: discord.Interaction):
        current_nick, current_bio, _, _, _ = await self._current_persona(interaction.guild)
        modal = ServerPersonaProfileModal(self, current_nick, current_bio)
        await interaction.response.send_modal(modal)

    # ── /server-persona-style ───────────────────────────────────────────
    @app_commands.command(
        name="server-persona-style",
        description="✨ Set display name font, effect, and colors for THIS server",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def server_persona_style(self, interaction: discord.Interaction):
        _, _, current_font, current_effect, current_colors = await self._current_persona(interaction.guild)
        modal = ServerPersonaStyleModal(self, current_font, current_effect, current_colors)
        await interaction.response.send_modal(modal)

    # ── /server-persona-reset-profile ───────────────────────────────────
    @app_commands.command(
        name="server-persona-reset-profile",
        description="🌸 Reset the bot's nickname/avatar/banner/bio in THIS server back to defaults",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def server_persona_reset_profile(self, interaction: discord.Interaction):
        view = ConfirmResetView(self, reset_type="profile")
        await interaction.response.send_message(
            "Reset my nickname, avatar, banner, and bio back to global defaults for this server?",
            view=view,
            ephemeral=True,
        )

    # ── /server-persona-reset-style ─────────────────────────────────────
    @app_commands.command(
        name="server-persona-reset-style",
        description="✨ Reset the bot's font, effect, and colors in THIS server back to normal",
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def server_persona_reset_style(self, interaction: discord.Interaction):
        view = ConfirmResetView(self, reset_type="style")
        await interaction.response.send_message(
            "Reset my display name font, effect, and colors back to normal for this server?",
            view=view,
            ephemeral=True,
        )

    @server_persona_profile.error
    @server_persona_style.error
    @server_persona_reset_profile.error
    @server_persona_reset_style.error
    async def _persona_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Nahhhh, you need **Moderator** permission (Timeout Members) to do that!!! 😭🙏"
        else:
            msg = f"⚠️ Unexpected error: {error}"

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ── AI personality commands (guild: admin-only / DM: self-serve) ────
    # 🌸 Same three commands work in BOTH contexts — interaction.guild is
    # None in a DM, so each handler branches on that to decide which
    # store (guild ai_persona table vs. dm_ai_persona table) to hit and
    # whether to enforce the moderate_members permission check. Discord
    # doesn't let a single @app_commands.command mix a guild-only perm
    # check with DM availability via decorator alone, so the permission
    # gate is done manually inside the handler instead of via
    # @app_commands.checks.has_permissions (which would block DM usage
    # entirely since that check assumes a guild context).

    @app_commands.command(
        name="persona-set",
        description="🌸 Change the bot's AI personality (server admins in a server, anyone in DMs)",
    )
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    async def persona_set(self, interaction: discord.Interaction):
        if interaction.guild is not None:
            # 🌸 Guild context — admin-only, same permission bar as the
            # other /server-persona-* commands.
            member = interaction.user
            if not isinstance(member, discord.Member) or not member.guild_permissions.moderate_members:
                await interaction.response.send_message(
                    "Nahhhh, you need **Moderator** permission (Timeout Members) to do that!!! 😭🙏",
                    ephemeral=True,
                )
                return

            conn = await self._get_db(interaction.guild.id)
            async with conn.execute(
                "SELECT preset_key, custom_text FROM ai_persona WHERE guild_id = ?", (interaction.guild.id,)
            ) as cur:
                row = await cur.fetchone()
            current_key = row[0] if row else DEFAULT_PRESET_KEY
            current_custom_text = row[1] if row and row[0] == "custom" else None

            async def on_pick(pick_interaction: discord.Interaction, preset_key: str):
                if preset_key == "custom":
                    async def on_custom_submit(modal_interaction: discord.Interaction, text: str):
                        await self.set_guild_ai_persona(
                            interaction.guild.id, modal_interaction.user.id, "custom", text
                        )
                        # 🌸 Overwrite the ORIGINAL "Pick a personality..."
                        # message (via pick_interaction) with a plain
                        # success confirmation and drop the view — same
                        # pattern as persona-view's Edit flow and the
                        # profile/style reset confirms, instead of
                        # leaving the dropdown message stale underneath
                        # a new one.
                        await pick_interaction.edit_original_response(
                            content="Done! Custom AI personality set for this server 🌸",
                            view=None,
                        )
                        await modal_interaction.response.defer()

                    # 🌸 Pre-fill with the existing custom text (if any) so
                    # picking "Custom..." again to tweak wording EDITS it
                    # instead of forcing a blank rewrite from scratch.
                    await pick_interaction.response.send_modal(
                        PersonaCustomTextModal(on_custom_submit, current_text=current_custom_text)
                    )
                else:
                    await self.set_guild_ai_persona(interaction.guild.id, pick_interaction.user.id, preset_key)
                    label = PRESET_PERSONAS[preset_key][0]
                    await pick_interaction.response.edit_message(
                        content=f"Done! This server's AI personality is now **{label}** 🌸", view=None
                    )

            await interaction.response.send_message(
                "Pick a personality for this server:",
                view=PersonaPickView(on_pick, current_key),
                ephemeral=True,
            )
        else:
            # 🌸 DM context — scoped to THIS DM channel (channel_id), not
            # the user, so it mirrors the guild's per-guild_id scoping.
            # In a 1:1 DM these end up the same person either way, but
            # channel_id is the structurally correct key.
            conn = await self._get_dm_db(interaction.channel.id)
            async with conn.execute(
                "SELECT preset_key, custom_text FROM dm_ai_persona WHERE channel_id = ?",
                (interaction.channel.id,),
            ) as cur:
                row = await cur.fetchone()
            current_key = row[0] if row else DEFAULT_PRESET_KEY
            current_custom_text = row[1] if row and row[0] == "custom" else None
            dm_channel_id = interaction.channel.id

            async def on_pick(pick_interaction: discord.Interaction, preset_key: str):
                if preset_key == "custom":
                    async def on_custom_submit(modal_interaction: discord.Interaction, text: str):
                        await self.set_dm_ai_persona(dm_channel_id, "custom", text)
                        # 🌸 Same pattern as the guild branch above —
                        # overwrite the original dropdown message with a
                        # plain confirmation, no leftover text/view.
                        await pick_interaction.edit_original_response(
                            content="Done! Your custom AI personality is set for our DMs 🌸",
                            view=None,
                        )
                        await modal_interaction.response.defer()

                    # 🌸 Pre-fill with the existing custom text (if any) so
                    # picking "Custom..." again to tweak wording EDITS it
                    # instead of forcing a blank rewrite from scratch.
                    await pick_interaction.response.send_modal(
                        PersonaCustomTextModal(on_custom_submit, current_text=current_custom_text)
                    )
                else:
                    await self.set_dm_ai_persona(dm_channel_id, preset_key)
                    label = PRESET_PERSONAS[preset_key][0]
                    await pick_interaction.response.edit_message(
                        content=f"Done! I'll be **{label}** in our DMs now 🌸", view=None
                    )

            await interaction.response.send_message(
                "Pick a personality for our DMs:",
                view=PersonaPickView(on_pick, current_key),
                ephemeral=True,
            )

    @app_commands.command(
        name="persona-view",
        description="🌸 See the current AI personality (this server, or your DM setting)",
    )
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    async def persona_view(self, interaction: discord.Interaction):
        is_guild = interaction.guild is not None
        if is_guild:
            scope_id = interaction.guild.id
            conn = await self._get_db(scope_id)
            async with conn.execute(
                "SELECT preset_key, custom_text FROM ai_persona WHERE guild_id = ?",
                (scope_id,),
            ) as cur:
                row = await cur.fetchone()
            scope = "this server's"
        else:
            scope_id = interaction.channel.id
            conn = await self._get_dm_db(scope_id)
            async with conn.execute(
                "SELECT preset_key, custom_text FROM dm_ai_persona WHERE channel_id = ?",
                (scope_id,),
            ) as cur:
                row = await cur.fetchone()
            scope = "your DM"

        view = None
        if row is None:
            msg = f"{scope.capitalize()} AI personality is currently the **default** 🌸"
        else:
            preset_key, custom_text = row
            if preset_key == "custom":
                msg = f"{scope.capitalize()} AI personality is **custom**:\n> {custom_text}"

                # 🌸 Custom text exists — offer a one-tap Edit button that
                # opens the SAME modal /persona-set's "Custom..." option
                # uses, pre-filled straight from the db row we just read
                # (custom_text), so it's a genuine edit-in-place instead
                # of a blank rewrite.
                async def on_edit(edit_interaction: discord.Interaction):
                    async def on_custom_submit(modal_interaction: discord.Interaction, text: str):
                        if is_guild:
                            await self.set_guild_ai_persona(
                                scope_id, modal_interaction.user.id, "custom", text
                            )
                        else:
                            await self.set_dm_ai_persona(scope_id, "custom", text)

                        # 🌸 Overwrite the ORIGINAL /persona-view message
                        # entirely with a plain success confirmation and
                        # drop the Edit button (view=None) — rather than
                        # showing the updated text with the button still
                        # live, which invites tapping Edit again on a
                        # message that's already "done". edit_interaction
                        # is the button click that opened this modal, so
                        # its original_response IS the /persona-view
                        # message.
                        done_msg = "Done! Personality updated 🌸"
                        await edit_interaction.edit_original_response(content=done_msg, view=None)
                        await modal_interaction.response.defer()

                    await edit_interaction.response.send_modal(
                        PersonaCustomTextModal(on_custom_submit, current_text=custom_text)
                    )

                view = PersonaEditButtonView(on_edit, require_moderator=is_guild)
            else:
                label = PRESET_PERSONAS.get(preset_key, (preset_key, ""))[0]
                msg = f"{scope.capitalize()} AI personality is currently **{label}** 🌸"

        await interaction.response.send_message(msg, view=view, ephemeral=True)

    @app_commands.command(
        name="persona-reset",
        description="🌸 Reset the AI personality back to default (this server, or your DM setting)",
    )
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    async def persona_reset(self, interaction: discord.Interaction):
        if interaction.guild is not None:
            member = interaction.user
            if not isinstance(member, discord.Member) or not member.guild_permissions.moderate_members:
                await interaction.response.send_message(
                    "Nahhhh, you need **Moderator** permission (Timeout Members) to do that!!! 😭🙏",
                    ephemeral=True,
                )
                return

            async def on_confirm(confirm_interaction: discord.Interaction):
                await self.reset_guild_ai_persona(interaction.guild.id)
                await confirm_interaction.response.edit_message(
                    content="Reset! This server's AI personality is back to default 🌸", view=None
                )

            await interaction.response.send_message(
                "Reset this server's AI personality back to default?",
                view=ConfirmResetPersonaView(on_confirm),
                ephemeral=True,
            )
        else:
            dm_channel_id = interaction.channel.id

            async def on_confirm(confirm_interaction: discord.Interaction):
                await self.reset_dm_ai_persona(dm_channel_id)
                await confirm_interaction.response.edit_message(
                    content="Reset! I'm back to my default personality in our DMs 🌸", view=None
                )

            await interaction.response.send_message(
                "Reset your DM AI personality back to default?",
                view=ConfirmResetPersonaView(on_confirm),
                ephemeral=True,
            )

