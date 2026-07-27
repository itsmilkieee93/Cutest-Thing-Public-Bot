# encoding: utf-8
import discord
from discord import app_commands
from discord.ext import commands
import speedtest
import asyncio
import random
from datetime import datetime

# ─── Pastel Colors ────────────────────────────────────────────────────────────
PASTEL_COLORS = [
    0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xAEC6CF, 0xB5EAD7,
    0xFFDAB9, 0xFFF0A0, 0xC9C0D3, 0xFFB7CE, 0xA8D8EA,
    0xFDFD96, 0xE0BBE4, 0x957DAD, 0xD4F0F0, 0xFFE5B4,
    0xE2F0CB, 0xFFCCF9, 0xC5E1A5, 0xF4978E, 0xB8E1FF,
]

# ─── Loading GIFs ─────────────────────────────────────────────────────────────
LOADING_GIFS = [
    "https://c.tenor.com/knwWU-EgRmMAAAAC/tenor.gif",
    "https://c.tenor.com/J9mOaXMbKygAAAAC/tenor.gif",
    "https://c.tenor.com/plvrL3peoBIAAAAC/tenor.gif",
    "https://c.tenor.com/Yo4Vo-XCgqEAAAAC/tenor.gif",
    "https://c.tenor.com/ts-81PaXp3AAAAAC/tenor.gif",
    "https://c.tenor.com/Ly_w3cT7B04AAAAC/tenor.gif",
]

# ─── Rating Helpers ───────────────────────────────────────────────────────────
def _dl_rating(mbps: float) -> str:
    if mbps >= 300: return "🚀 Ultra Fast"
    if mbps >= 100: return "⚡ Very Fast"
    if mbps >= 50:  return "💨 Fast"
    if mbps >= 20:  return "✅ Good"
    if mbps >= 5:   return "🐢 Slow"
    return "🔴 Very Slow"

def _ul_rating(mbps: float) -> str:
    if mbps >= 100: return "🚀 Ultra Fast"
    if mbps >= 50:  return "⚡ Very Fast"
    if mbps >= 20:  return "💨 Fast"
    if mbps >= 10:  return "✅ Good"
    if mbps >= 3:   return "🐢 Slow"
    return "🔴 Very Slow"

def _ping_rating(ms: float) -> str:
    if ms <= 20:  return "🟢 Excellent"
    if ms <= 60:  return "🟡 Good"
    if ms <= 120: return "🟠 Fair"
    return "🔴 High"

def _speed_bar(mbps: float, max_mbps: float = 200) -> str:
    """Visual bar scaled to max_mbps."""
    filled = min(int((mbps / max_mbps) * 12), 12)
    return f"`[{'█' * filled}{'░' * (12 - filled)}]`"


# ══════════════════════════════════════════════════════════════════════════════
# 🛜 Speed Test Cog
# ══════════════════════════════════════════════════════════════════════════════
class SpeedTestModule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="speed-test",
        description="Check my owner's internet speed in real-time! 🚀💨"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def speed_test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        color = random.choice(PASTEL_COLORS)

        # ── Loading embed ──────────────────────────────────────────────────────
        loading_embed = discord.Embed(
            title="🛜 Running Speed Test...",
            description=(
                "Scanning the nearest server and measuring signals! 📡\n"
                "This usually takes **20–40 seconds** — hang tight! ☕✨"
            ),
            color=color,
            timestamp=datetime.now()
        )
        loading_embed.add_field(name="📥 Download", value="`Measuring...`", inline=True)
        loading_embed.add_field(name="📤 Upload",   value="`Measuring...`", inline=True)
        loading_embed.add_field(name="🛰️ Ping",    value="`Measuring...`", inline=True)
        loading_embed.set_thumbnail(url=random.choice(LOADING_GIFS))
        loading_embed.set_footer(text="Cutest Thing 🌸✨  •  Please wait...")
        loading_msg = await interaction.followup.send(embed=loading_embed)

        try:
            # ── Run test in thread ─────────────────────────────────────────────
            # Fix: use threads=4 for both download & upload to match real speeds
            def run_test():
                st = speedtest.Speedtest(secure=True)
                st.get_servers()
                st.get_best_server()
                st.download(threads=4)   # ← multi-thread for accuracy
                st.upload(threads=4, pre_allocate=False)  # ← fix low upload bug
                return st.results.dict()

            results = await asyncio.to_thread(run_test)

            # ── Parse results ──────────────────────────────────────────────────
            dl_mbps  = results['download'] / 1_000_000
            ul_mbps  = results['upload']   / 1_000_000
            ping     = results['ping']
            isp      = results['client']['isp']
            server   = results['server']
            srv_name = f"{server['sponsor']} — {server['name']}, {server['country']}"

            dl_bar  = _speed_bar(dl_mbps)
            ul_bar  = _speed_bar(ul_mbps)
            dl_rate = _dl_rating(dl_mbps)
            ul_rate = _ul_rating(ul_mbps)
            pg_rate = _ping_rating(ping)

            # ── Result embed ───────────────────────────────────────────────────
            result_color = random.choice(PASTEL_COLORS)
            embed = discord.Embed(
                title="🌐 Internet Speed Results ✨",
                color=result_color,
                timestamp=datetime.now()
            )
            embed.description = (
                f"**🏢 ISP:** {isp}\n"
                f"**📍 Server:** {srv_name}"
            )

            embed.add_field(
                name="📥 Download",
                value=(
                    f"**{dl_mbps:.2f} Mbps**\n"
                    f"{dl_bar}"
                ),
                inline=True
            )
            embed.add_field(
                name="📤 Upload",
                value=(
                    f"**{ul_mbps:.2f} Mbps**\n"
                    f"{ul_bar}"
                ),
                inline=True
            )
            embed.add_field(
                name="🛰️ Ping",
                value=(
                    f"**{ping:.1f} ms**\n"
                ),
                inline=True
            )    
            embed.add_field(
                name="🌟 Connection Rate",
                value=(
                    f"{pg_rate}"
                ),
                inline=True                
                
            )

            # ── Summary line ───────────────────────────────────────────────────
            total = dl_mbps + ul_mbps
            if total >= 200:   summary = "🔥 Blazing connection!"
            elif total >= 100: summary = "🌟 Excellent connection!"
            elif total >= 50: summary = "⚡ Great connection!"
            elif total >= 20:  summary = "✅ Solid connection!"
            elif total >= 5:  summary = "🟡 Decent connection."
            else:              summary = "🐢 Connection is slow."

            embed.add_field(
                name="📊 Overall",
                value=summary,
                inline=False
            )

            embed.set_footer(
                text="Cutest Thing 🌸✨ • Test Complete",
                icon_url=self.bot.user.display_avatar.url
            )

            await loading_msg.edit(embed=embed)

        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Speed Test Failed",
                description=(
                    "The signals got a bit tangled! 🥺\n"
                    f"```{str(e)[:200]}```"
                ),
                color=0xFF6B6B,
                timestamp=datetime.now()
            )
            error_embed.set_footer(text="Please try again in a moment 😊")
            try:
                await loading_msg.edit(embed=error_embed)
            except Exception:
                await interaction.followup.send(embed=error_embed)


async def setup(bot):
    await bot.add_cog(SpeedTestModule(bot))
    print("✅ SpeedTestModule is ready to zoom! 🏎️💨")
