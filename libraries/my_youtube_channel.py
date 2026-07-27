"""
📊 YouTube Channel Analytics
  • /my-channel — fetch your channel stats via YouTube Analytics API v2

Channel token files live in auth/:
  Channel 1 → auth/yt_token.json
  Channel 2 → auth/yt_token2.json

Add more channels by editing CHANNEL_CHOICES below.
"""
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import logging
import random
import os
from datetime import datetime, timedelta, timezone
import json

# ══════════════════════════════════════════════════════════════════════════════
# 📁 Paths & API key
# ══════════════════════════════════════════════════════════════════════════════

AUTH_DIR        = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth")
API_KEY_FILE    = os.path.join(AUTH_DIR, "yt_api")
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

try:
    YOUTUBE_API_KEY = open(API_KEY_FILE).read().strip()
except FileNotFoundError:
    raise RuntimeError(
        "auth/yt_api not found. Place your YouTube Data API key there. Expected: " + API_KEY_FILE
    )

# ── Channel registry ───────────────────────────────────────────────────────────
# Choice name shown in Discord  →  token filename inside auth/
# To add a 3rd channel, append another Choice entry here.
CHANNEL_CHOICES = [
    app_commands.Choice(name="StayHalalBro🇮🇩", value="yt_token.json"),
    app_commands.Choice(name="StayHalalBro2🇮🇩", value="yt_token2.json"),
]

YT_API       = "https://www.googleapis.com/youtube/v3"
YT_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2"

logger = logging.getLogger(__name__)

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


# ══════════════════════════════════════════════════════════════════════════════
# 🔑 Auth helper — per-channel token file
# ══════════════════════════════════════════════════════════════════════════════

async def get_access_token(session: aiohttp.ClientSession, token_filename: str) -> str:
    """Read token from auth/<token_filename>, refresh if expired, save back."""
    path = os.path.join(AUTH_DIR, token_filename)
    if not os.path.exists(path):
        raise RuntimeError(
            f"Token file '{token_filename}' not found in auth/. Expected: {path}"
        )

    with open(path) as f:
        token_data = json.load(f)

    access_token = token_data.get("access_token")
    expiry_str   = token_data.get("expiry") or token_data.get("token_expiry")

    # Still valid? (60 s buffer)
    if access_token and expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < expiry - timedelta(seconds=60):
                return access_token
        except Exception:
            pass  # Can't parse expiry — fall through to refresh

    # Refresh
    refresh_token = token_data.get("refresh_token")
    client_id     = token_data.get("client_id")
    client_secret = token_data.get("client_secret")

    if not all([refresh_token, client_id, client_secret]):
        raise RuntimeError(
            f"'{token_filename}' is missing refresh_token, client_id, or client_secret."
        )

    async with session.post(OAUTH_TOKEN_URL, data={
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        refreshed = await resp.json()

    if "access_token" not in refreshed:
        raise RuntimeError(
            "Token refresh failed: " + refreshed.get("error_description", str(refreshed))
        )

    token_data["access_token"] = refreshed["access_token"]
    token_data["expiry"] = (
        datetime.now(timezone.utc) + timedelta(seconds=refreshed.get("expires_in", 3600))
    ).isoformat()
    with open(path, "w") as f:
        json.dump(token_data, f, indent=2)

    return refreshed["access_token"]


# ══════════════════════════════════════════════════════════════════════════════
# 🔧 API helpers
# ══════════════════════════════════════════════════════════════════════════════

async def yt_data_get(
    session: aiohttp.ClientSession, endpoint: str, access_token: str, extra: dict = {}
) -> dict:
    """YouTube Data API v3 — authenticated with OAuth bearer token."""
    async with session.get(
        YT_API + endpoint,
        params={"key": YOUTUBE_API_KEY, **extra},
        headers={"Authorization": "Bearer " + access_token},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        if resp.status != 200:
            raise RuntimeError("Data API: " + data.get("error", {}).get("message", str(data)))
        return data


async def yt_analytics_get(
    session: aiohttp.ClientSession, access_token: str, params: dict
) -> dict:
    """YouTube Analytics API v2 — OAuth bearer token required."""
    async with session.get(
        YT_ANALYTICS + "/reports",
        params=params,
        headers={"Authorization": "Bearer " + access_token},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        data = await resp.json()
        if resp.status != 200:
            raise RuntimeError("Analytics API: " + data.get("error", {}).get("message", str(data)))
        return data


# ══════════════════════════════════════════════════════════════════════════════
# 🔧 Parsing / formatting helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_analytics(data: dict, metric_names: list[str]) -> dict:
    """Collapse all rows into totals keyed by metric name."""
    headers = [h["name"] for h in data.get("columnHeaders", [])]
    totals  = {m: 0.0 for m in metric_names}
    for row in data.get("rows", []):
        for i, val in enumerate(row):
            col = headers[i] if i < len(headers) else None
            if col in totals and val is not None:
                totals[col] += float(val)
    return totals


def _n(n: float) -> str:
    n = int(n)
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

def _dur(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return (f"{h}h {m}m {s}s" if h else f"{m}m {s}s")

def _pct(n: float) -> str: return f"{n:.2f}%"
def _usd(n: float) -> str: return f"${n:.2f}"

def _flag(country_code: str) -> str:
    """Convert a 2-letter country code to a flag emoji."""
    code = country_code.upper()
    if len(code) != 2:
        return "🌍"
    return chr(0x1F1E6 + ord(code[0]) - ord("A")) + chr(0x1F1E6 + ord(code[1]) - ord("A"))


# ══════════════════════════════════════════════════════════════════════════════
# 🗑️  Delete button with confirmation
#     Only attached when visible=True (public messages).
#     Ephemeral messages are invisible to the bot so they can't be deleted.
# ══════════════════════════════════════════════════════════════════════════════

class DeleteConfirmView(discord.ui.View):
    """Ephemeral confirmation prompt shown after pressing 🗑️ Delete."""

    def __init__(self, target_msg: discord.Message):
        super().__init__(timeout=20)
        self.target_msg = target_msg

    async def on_timeout(self):
        self.stop()

    @discord.ui.button(label="✅ Yes, delete it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.target_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        await interaction.response.send_message("🗑️ Message deleted.", ephemeral=True, delete_after=3)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❎ Cancelled.", ephemeral=True, delete_after=3)
        self.stop()


class AnalyticsView(discord.ui.View):
    """Attached to the analytics embed when the message is public (visible=True)."""

    def __init__(self):
        super().__init__(timeout=300)  # buttons live for 5 minutes

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        confirm_view = DeleteConfirmView(interaction.message)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to delete this message?",
            view=confirm_view,
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 🤖 Cog
# ══════════════════════════════════════════════════════════════════════════════

class YouTubeAnalyticsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="my-channel",
        description="Fetch My Owner's YouTube channel analytics 📊",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        channel="Which YouTube channel to show analytics for",
        period="Time period for analytics",
        visible="Post the result publicly in chat? (default: only you can see it)",
    )
    @app_commands.choices(
        channel=CHANNEL_CHOICES,
        period=[
            app_commands.Choice(name="📅 Last 28 days", value="28"),
            app_commands.Choice(name="📅 Last 90 days", value="90"),
        ],
    )
    async def my_channel(
        self,
        interaction: discord.Interaction,
        channel: app_commands.Choice[str],
        period: app_commands.Choice[str] = None,
        visible: bool = False,
    ):
        # visible=True  → public message  → ephemeral=False → delete button shown
        # visible=False → private message → ephemeral=True  → no delete button needed
        ephemeral      = not visible
        token_filename = channel.value
        days           = int(period.value) if period else 28
        today          = datetime.now(timezone.utc).date()
        start          = today - timedelta(days=days)
        start_str      = start.strftime("%Y-%m-%d")
        end_str        = today.strftime("%Y-%m-%d")

        await interaction.response.defer(ephemeral=ephemeral)

        loading_embed = discord.Embed(
            title="📊 Fetching your analytics...",
            description="Talking to the YouTube Analytics API... ✨\n\nThis might take a moment!",
            color=random.choice(PASTEL_COLORS),
        )
        loading_embed.set_thumbnail(url=random.choice(LOADING_GIFS))
        loading_msg = await interaction.followup.send(embed=loading_embed, ephemeral=ephemeral)

        try:
            async with aiohttp.ClientSession() as session:

                # ── Auth ───────────────────────────────────────────────────────
                try:
                    access_token = await get_access_token(session, token_filename)
                except Exception as auth_err:
                    return await loading_msg.edit(embed=discord.Embed(
                        title="❌ Auth Failed",
                        description=(
                            f"Could not authenticate with `{token_filename}`:\n"
                            "`" + str(auth_err)[:200] + "`"
                        ),
                        color=0xFF6B6B,
                    ))

                # ── Channel info (lifetime totals + icon) ──────────────────────
                ch_data = await yt_data_get(session, "/channels", access_token, {
                    "part":   "snippet,statistics",
                    "mine":   "true",
                    "fields": "items(id,snippet(title,thumbnails/default/url),statistics)",
                })
                ch_items = ch_data.get("items", [])
                if not ch_items:
                    return await loading_msg.edit(embed=discord.Embed(
                        title="❌ No Channel Found",
                        description=f"No YouTube channel is linked to `{token_filename}`.",
                        color=0xFF6B6B,
                    ))

                ch           = ch_items[0]
                ch_name      = ch["snippet"]["title"]
                ch_icon      = ch["snippet"]["thumbnails"]["default"]["url"]
                ch_url       = "https://youtube.com/channel/" + ch["id"]
                total_subs   = int(ch["statistics"].get("subscriberCount", 0))
                total_views  = int(ch["statistics"].get("viewCount",       0))
                total_videos = int(ch["statistics"].get("videoCount",      0))

                # ── Core analytics metrics ─────────────────────────────────────
                # 🌸 Note: impressions, CTR, cards, annotations excluded —
                # the API doesn't allow them for basic channel-wide time queries.
                CORE = [
                    "views",
                    "estimatedMinutesWatched",
                    "averageViewDuration",
                    "averageViewPercentage",
                    "subscribersGained",
                    "subscribersLost",
                    "likes",
                    "dislikes",
                    "comments",
                    "shares",
                    "videosAddedToPlaylists",
                ]
                core_data = await yt_analytics_get(session, access_token, {
                    "ids":       "channel==MINE",
                    "startDate": start_str,
                    "endDate":   end_str,
                    "metrics":   ",".join(CORE),
                })
                m = _parse_analytics(core_data, CORE)

                # ── Revenue (skip silently if not monetised) ───────────────────
                REV = ["estimatedRevenue", "estimatedAdRevenue", "grossRevenue", "cpm", "playbackBasedCpm"]
                rev = {}
                try:
                    rev_data = await yt_analytics_get(session, access_token, {
                        "ids":       "channel==MINE",
                        "startDate": start_str,
                        "endDate":   end_str,
                        "metrics":   ",".join(REV),
                    })
                    rev = _parse_analytics(rev_data, REV)
                except Exception:
                    pass

                # ── Top 5 geographic regions by views ─────────────────────────
                geo_rows = []
                try:
                    geo_data = await yt_analytics_get(session, access_token, {
                        "ids":        "channel==MINE",
                        "startDate":  start_str,
                        "endDate":    end_str,
                        "metrics":    "views",
                        "dimensions": "country",
                        "sort":       "-views",
                        "maxResults": "5",
                    })
                    for row in geo_data.get("rows", []):
                        if len(row) >= 2:
                            geo_rows.append((str(row[0]), int(row[1])))
                except Exception:
                    pass

                # ── Build embed ────────────────────────────────────────────────
                net_subs    = int(m["subscribersGained"] - m["subscribersLost"])
                watch_hours = m["estimatedMinutesWatched"] / 60

                embed = discord.Embed(
                    title="📊 " + ch_name + " · Last " + str(days) + " days",
                    url=ch_url,
                    color=random.choice(PASTEL_COLORS),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_thumbnail(url=ch_icon)

                embed.add_field(
                    name="🌐 Channel Totals (lifetime)",
                    value=(
                        "👥 **" + _n(total_subs)   + "** subscribers\n"
                        "👁️ **" + _n(total_views)  + "** views\n"
                        "🎬 **" + _n(total_videos) + "** videos"
                    ),
                    inline=False,
                )
                embed.add_field(
                    name="👁️ Views & Watch Time",
                    value=(
                        "▸ Views: **"        + _n(m["views"])                   + "**\n"
                        "▸ Watch time: **"   + f"{watch_hours:,.1f} hrs"        + "**\n"
                        "▸ Avg duration: **" + _dur(m["averageViewDuration"])   + "**\n"
                        "▸ Avg % viewed: **" + _pct(m["averageViewPercentage"]) + "**"
                    ),
                    inline=True,
                )
                embed.add_field(
                    name="👥 Subscribers",
                    value=(
                        "▸ Gained: **+" + _n(m["subscribersGained"]) + "**\n"
                        "▸ Lost: **-"   + _n(m["subscribersLost"])   + "**\n"
                        "▸ Net: **"     + ("+" if net_subs >= 0 else "") + _n(net_subs) + "**"
                    ),
                    inline=True,
                )
                embed.add_field(
                    name="💬 Engagement",
                    value=(
                        "▸ Likes: **"              + _n(m["likes"])                  + "**\n"
                        "▸ Dislikes: **"           + _n(m["dislikes"])               + "**\n"
                        "▸ Comments: **"           + _n(m["comments"])               + "**\n"
                        "▸ Shares: **"             + _n(m["shares"])                 + "**\n"
                        "▸ Added to playlists: **" + _n(m["videosAddedToPlaylists"]) + "**"
                    ),
                    inline=True,
                )

                if rev and rev.get("estimatedRevenue", 0) > 0:
                    embed.add_field(name="\u200b", value="\u200b", inline=False)
                    embed.add_field(
                        name="💰 Revenue",
                        value=(
                            "▸ Estimated revenue: **"    + _usd(rev["estimatedRevenue"])   + "**\n"
                            "▸ Estimated ad revenue: **" + _usd(rev["estimatedAdRevenue"]) + "**\n"
                            "▸ Gross revenue: **"        + _usd(rev["grossRevenue"])        + "**\n"
                            "▸ CPM: **"                  + _usd(rev["cpm"])                 + "**\n"
                            "▸ Playback CPM: **"         + _usd(rev["playbackBasedCpm"])    + "**"
                        ),
                        inline=True,
                    )

                if geo_rows:                    
                    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                    geo_lines = [
                        f"{medals[i] if i < len(medals) else '▸'} {_flag(code)} **{code}** — **{_n(views)}** views"
                        for i, (code, views) in enumerate(geo_rows)
                    ]
                    embed.add_field(
                        name="🌍 Top 5 Geographic Regions",
                        value="\n".join(geo_lines),
                        inline=False,
                    )

                embed.set_footer(
                    text=start_str + " → " + end_str + "  ·  YouTube Analytics API v2",
                    icon_url=(
                        "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e7/"
                        "YouTube_social_white_squircle_%282024%29.svg/"
                        "1280px-YouTube_social_white_squircle_%282024%29.svg.png"
                    ),
                )

                # 🗑️ Delete button only on public messages — ephemeral ones can't be deleted by bots
                view = AnalyticsView() if not ephemeral else None
                await loading_msg.edit(embed=embed, view=view)

        except Exception as err:
            logger.error("my-channel error: " + str(err))
            await loading_msg.edit(embed=discord.Embed(
                title="❌ Analytics Error",
                description="Something went wrong:\n`" + str(err)[:150] + "`",
                color=0xFF6B6B,
            ))


async def setup(bot):
    await bot.add_cog(YouTubeAnalyticsCog(bot))
    print("✅ YouTube Analytics Module loaded successfully!")