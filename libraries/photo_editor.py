"""
🖼️ photo_editor.py
Discord cog for the /editimage slash command.
Mirrors the loading-embed pattern from summarize.py.
"""

import io
import logging
import os
import random
import sys
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


# Ensure photo_editor logs go to a file regardless of other config
_photo_editor_handler = logging.FileHandler("photo_editor_debug.log", encoding="utf-8")
_photo_editor_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger = logging.getLogger(__name__)
logger.addHandler(_photo_editor_handler)
logger.setLevel(logging.INFO)
# ─────────────────────────────────────────────────────────────────────────────
# The compiled native extension lives in libraries/clang/ (a subfolder next
# to this file), not alongside photo_editor.py itself — add it to sys.path
# so `import photo_editor_native` can find it there.
# ─────────────────────────────────────────────────────────────────────────────
_NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clang")

if os.path.isdir(_NATIVE_DIR):
    _so_files = [f for f in os.listdir(_NATIVE_DIR) if f.startswith("photo_editor_native") and f.endswith(".so")]
    if _so_files:
        logger.info("photo_editor: found native .so in %s: %s", _NATIVE_DIR, ", ".join(_so_files))
    else:
        logger.warning("photo_editor: %s exists but no photo_editor_native*.so found in it", _NATIVE_DIR)
else:
    logger.warning("photo_editor: native dir %s does not exist — will fall back to photo_editor_s", _NATIVE_DIR)

if _NATIVE_DIR not in sys.path:
    sys.path.insert(0, _NATIVE_DIR)

# ─────────────────────────────────────────────────────────────────────────────
# Prefer the compiled pybind11 extension (photo_editor_native) for speed;
# fall back to the pure-Python implementation (photo_editor_s) if the
# native module is missing, fails to import (wrong ABI/platform), or
# doesn't expose the expected PhotoEditor interface.
# ─────────────────────────────────────────────────────────────────────────────
USING_NATIVE_BACKEND = False

try:
    from photo_editor_native import PhotoEditor as _PhotoEditorNative  # type: ignore

    # Sanity-check the native module exposes the methods we call before
    # committing to it — a partial/mismatched build shouldn't crash later
    # at request time, it should fall back now instead.
    # Note: the native PhotoEditor has no FILTERS class attribute (only
    # photo_editor_s does) — the module-level filter list below is
    # hardcoded instead of read off PhotoEditor.FILTERS for that reason.
    _required = (
        "apply_filter", "adjust_temperature", "adjust_brightness",
        "adjust_contrast", "adjust_saturation", "adjust_sharpness",
        "resize", "crop_square", "rotate", "flip_horizontal",
        "flip_vertical", "to_bytes", "to_bytes_under_limit",
    )
    _missing = [a for a in _required if not hasattr(_PhotoEditorNative, a)]
    if _missing:
        raise ImportError(
            "photo_editor_native.PhotoEditor is missing expected methods: "
            + ", ".join(_missing)
        )

    USING_NATIVE_BACKEND = True
    _native_module = sys.modules.get("photo_editor_native")
    _native_path = getattr(_native_module, "__file__", "unknown location")
    logger.info("photo_editor: using native (pybind11) backend, loaded from %s", _native_path)
    
    # Wrap the native backend to normalize the resize() interface:
    # C++ expects resize(width, height=-1, keep_aspect=True) where -1 means auto-scale.
    # Python expects resize(width, height=None, keep_aspect=True) where None means auto-scale.
    # Translate height=None to -1 for C++, and vice versa for Python.
    class PhotoEditor(_PhotoEditorNative):
        def resize(self, width: int, height=None, keep_aspect: bool = True):
            """Normalize height: None (Python convention) → -1 (C++ convention)."""
            if height is None:
                height = -1
            return super().resize(width, height, keep_aspect)

except Exception as e:
    logger.warning(
        "photo_editor: native backend unavailable (%s: %s) — falling back to photo_editor_s",
        type(e).__name__,
        e,
    )
    from photo_editor_s import PhotoEditor  # pure-Python fallback

# ─────────────────────────────────────────────────────────────────────────────
# Supported filter names, shown as slash-command choices.
# Hardcoded (rather than read from PhotoEditor.FILTERS) because the native
# backend doesn't expose a FILTERS attribute — only photo_editor_s does.
# Keep this list in sync with FILTERS in photo_editor_s.py and the filter
# names documented in bindings.cpp / photo_editor.hpp.
# ─────────────────────────────────────────────────────────────────────────────
FILTER_NAMES = [
    "blur", "sharpen", "contour", "emboss", "edge_enhance", "smooth",
    "detail", "grayscale", "invert", "sepia", "posterize", "solarize",
    "vignette",
]

# ─────────────────────────────────────────────────────────────────────────────
# Pastel palette & loading GIFs  (from summarize.py / chatting_fun.py)
# ─────────────────────────────────────────────────────────────────────────────
PASTEL_COLORS = [
    0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xAEC6CF, 0xB5EAD7,
    0xFFDAB9, 0xFFF0A0, 0xC9C0D3, 0xFFB7CE, 0xA8D8EA,
    0xFDFD96, 0xE0BBE4, 0x957DAD, 0xD4F0F0, 0xFFE5B4,
    0xE2F0CB, 0xFFCCF9, 0xC5E1A5, 0xF4978E, 0xB8E1FF,
]

LOADING_GIFS = [
    "https://c.tenor.com/knwWU-EgRmMAAAAC/tenor.gif",
    "https://c.tenor.com/J9mOaXMbKygAAAAC/tenor.gif",
    "https://c.tenor.com/plvrL3peoBIAAAAC/tenor.gif",
    "https://c.tenor.com/Yo4Vo-XCgqEAAAAC/tenor.gif",
    "https://c.tenor.com/ts-81PaXp3AAAAAC/tenor.gif",
    "https://c.tenor.com/Ly_w3cT7B04AAAAC/tenor.gif",
]

FILTER_CHOICES = [
    app_commands.Choice(name=name.replace("_", " ").title(), value=name)
    for name in FILTER_NAMES
]

# ─────────────────────────────────────────────────────────────────────────────
# Discord upload limits by server boost tier (bytes)
# https://discord.com/developers/docs/resources/guild#guild-object-premium-tier
# ─────────────────────────────────────────────────────────────────────────────
BOOST_TIER_LIMITS = {
    0: 10 * 1024 * 1024,   # No boost / Tier 0 — 10 MB
    1: 10 * 1024 * 1024,   # Tier 1 — still 10 MB
    2: 50 * 1024 * 1024,   # Tier 2 — 50 MB
    3: 100 * 1024 * 1024,  # Tier 3 — 100 MB
}
DEFAULT_LIMIT = BOOST_TIER_LIMITS[0]
# DM interactions have no guild, so fall back to the base 10 MB limit.


def get_upload_limit(interaction: discord.Interaction) -> int:
    """Resolve the max attachment size for this context based on server boost tier."""
    guild = interaction.guild
    if guild is None:
        return DEFAULT_LIMIT
    tier = getattr(guild, "premium_tier", 0) or 0
    return BOOST_TIER_LIMITS.get(tier, DEFAULT_LIMIT)


class PhotoEditorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── embed helpers ─────────────────────────────────────────────────────────
    async def _send_loading_embed(self, interaction: discord.Interaction) -> discord.WebhookMessage:
        embed = discord.Embed(
            title="🖼️ Editing your image...",
            description="Applying your edits and touching things up! ✨\n\nHang tight... 💕",
            color=random.choice(PASTEL_COLORS),
        )
        embed.set_thumbnail(url=random.choice(LOADING_GIFS))
        return await interaction.followup.send(embed=embed)

    async def _edit_to_result_embed(
        self,
        loading_msg: discord.WebhookMessage,
        interaction: discord.Interaction,
        file: discord.File,
        applied: list[str],
        filename: str,
        was_compressed: bool,
    ):
        embed = discord.Embed(
            title="🌸 Edited Image",
            description=", ".join(applied) if applied else "No edits applied",
            color=random.choice(PASTEL_COLORS),
        )
        if was_compressed:
            embed.add_field(
                name="📦 Note",
                value="Image was compressed/resized to fit this server's upload limit.",
                inline=False,
            )
        embed.set_image(url=f"attachment://{filename}")
        embed.set_footer(
            text=f"Requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        await loading_msg.edit(embed=embed, attachments=[file])

    # ── /editimage ───────────────────────────────────────────────────────────
    @app_commands.command(name="edit-image", description="Edit an image 🌸✨")
    @app_commands.describe(
        image="The image to edit",
        filter="Optional filter to apply",
        temperature="-100 (cool/blue) to 100 (warm/orange)",
        brightness="1.0 = unchanged, e.g. 1.2 = brighter, 0.8 = darker",
        contrast="1.0 = unchanged",
        saturation="1.0 = unchanged, 0 = grayscale",
        sharpness="1.0 = unchanged",
        width="Resize width in pixels",
        height="Resize height in pixels (omit to keep aspect ratio)",
        rotate="Degrees to rotate",
        flip_horizontal="Mirror the image left-right",
        flip_vertical="Mirror the image top-bottom",
        square="Crop to a centered square",
    )
    @app_commands.choices(filter=FILTER_CHOICES)
    async def editimage(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        filter: app_commands.Choice[str] = None,
        temperature: app_commands.Range[int, -100, 100] = 0,
        brightness: app_commands.Range[float, 0.0, 3.0] = 1.0,
        contrast: app_commands.Range[float, 0.0, 3.0] = 1.0,
        saturation: app_commands.Range[float, 0.0, 3.0] = 1.0,
        sharpness: app_commands.Range[float, 0.0, 3.0] = 1.0,
        width: app_commands.Range[int, 16, 4096] = None,
        height: app_commands.Range[int, 16, 4096] = None,
        rotate: app_commands.Range[float, -360, 360] = 0,
        flip_horizontal: bool = False,
        flip_vertical: bool = False,
        square: bool = False,
    ):
        if not image.content_type or not image.content_type.startswith("image/"):
            await interaction.response.send_message("⚠️ That attachment isn't an image~", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        loading_msg = await self._send_loading_embed(interaction)

        try:
            raw = await image.read()
            editor = PhotoEditor(raw)

            if filter:
                editor.apply_filter(filter.value)
            if temperature:
                editor.adjust_temperature(temperature)
            if brightness != 1.0:
                editor.adjust_brightness(brightness)
            if contrast != 1.0:
                editor.adjust_contrast(contrast)
            if saturation != 1.0:
                editor.adjust_saturation(saturation)
            if sharpness != 1.0:
                editor.adjust_sharpness(sharpness)
            if square:
                editor.crop_square()
            if width:
                editor.resize(width, height)
            if rotate:
                editor.rotate(rotate)
            if flip_horizontal:
                editor.flip_horizontal()
            if flip_vertical:
                editor.flip_vertical()

            # Leave a little headroom below the hard cap for multipart overhead
            limit = get_upload_limit(interaction)
            safe_limit = int(limit * 0.97)

            out_bytes, ext = editor.to_bytes_under_limit(safe_limit, fmt="PNG")
            was_compressed = ext != "png" or len(out_bytes) < len(editor.to_bytes("PNG"))

            if len(out_bytes) > limit:
                await loading_msg.edit(
                    embed=discord.Embed(
                        title="⚠️ Still too large",
                        description=(
                            f"Even after compression the result is "
                            f"{len(out_bytes) / 1024 / 1024:.1f} MB, over this server's "
                            f"{limit / 1024 / 1024:.0f} MB upload limit. Try a smaller "
                            f"source image or lower resolution."
                        ),
                        color=random.choice(PASTEL_COLORS),
                    )
                )
                return

        except Exception as e:
            backend = "native" if USING_NATIVE_BACKEND else "fallback (photo_editor_s)"
            await loading_msg.edit(
                embed=discord.Embed(
                    title="⚠️ Edit failed",
                    description=f"`{str(e)[:300]}`",
                    color=random.choice(PASTEL_COLORS),
                ).set_footer(text=f"backend: {backend}")
            )
            return

        filename = f"edited.{ext}"
        file = discord.File(io.BytesIO(out_bytes), filename=filename)

        applied = []
        if filter:
            applied.append(f"filter: {filter.name}")
        if temperature:
            applied.append(f"temp: {temperature:+d}")
        if brightness != 1.0:
            applied.append(f"brightness: {brightness}")
        if contrast != 1.0:
            applied.append(f"contrast: {contrast}")
        if saturation != 1.0:
            applied.append(f"saturation: {saturation}")
        if sharpness != 1.0:
            applied.append(f"sharpness: {sharpness}")
        if width:
            applied.append(f"resized: {width}x{height or '(auto)'}")
        if rotate:
            applied.append(f"rotate: {rotate}°")
        if square:
            applied.append("cropped: square")
        if flip_horizontal:
            applied.append("flipped: horizontal")
        if flip_vertical:
            applied.append("flipped: vertical")

        await self._edit_to_result_embed(
            loading_msg, interaction, file, applied, filename, was_compressed
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PhotoEditorCog(bot))

