# encoding: utf-8
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import urllib.parse
import textwrap
import re
import os
import json
import asyncio
from datetime import datetime

COMMON_LANGS = {
    "English":    "en", "Indonesian": "id", "Japanese":   "ja",
    "Spanish":    "es", "German":     "de", "French":     "fr",
    "Russian":    "ru", "Chinese":    "zh", "Portuguese": "pt",
    "Italian":    "it", "Arabic":     "ar", "Korean":     "ko",
    "Vietnamese": "vi", "Thai":       "th", "Hindi":      "hi",
}

# ─── Wikipedia paginator registry ─────────────────────────────────────────────
# Stores sections + current page per message_id so Back/Next buttons survive
# a bot restart without showing "Interaction Failed".
WIKI_REGISTRY_DIR  = "interactions"
WIKI_REGISTRY_FILE = "interactions/wikipedia_registry.json"
os.makedirs(WIKI_REGISTRY_DIR, exist_ok=True)


def _save_wiki_record(message_id: str, record: dict) -> None:
    """Persist paginator state for a given message. Atomic write."""
    data: dict = {}
    if os.path.exists(WIKI_REGISTRY_FILE):
        try:
            with open(WIKI_REGISTRY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
    data[str(message_id)] = record
    tmp = WIKI_REGISTRY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)   # no indent — keeps file smaller
        os.replace(tmp, WIKI_REGISTRY_FILE)
    except OSError:
        try: os.remove(tmp)
        except OSError: pass


def _get_wiki_record(message_id: str) -> dict | None:
    """Load paginator state for a given message."""
    if not os.path.exists(WIKI_REGISTRY_FILE):
        return None
    try:
        with open(WIKI_REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(str(message_id))
    except (json.JSONDecodeError, OSError):
        return None


def _update_wiki_page(message_id: str, new_page: int) -> None:
    """Update only the current_page field without rewriting all sections."""
    record = _get_wiki_record(message_id)
    if record is not None:
        record["current_page"] = new_page
        _save_wiki_record(message_id, record)


# ══════════════════════════════════════════════════════════════════════════════
# 📖 Wikipedia Paginator — PERSISTENT (survives bot restarts)
# ══════════════════════════════════════════════════════════════════════════════
# Rules applied here:
#   1. timeout=None  (required for persistent views)
#   2. Every button has a hard-coded unique custom_id
#   3. bot.add_view(WikipediaPaginator()) registered once in cog_load
#   4. All page data (title, sections, url, current_page) is stored in
#      interactions/wikipedia_registry.json, keyed by message_id
#   5. On every button press, data is loaded fresh from disk — works even
#      after a full bot restart
# ──────────────────────────────────────────────────────────────────────────────
class WikipediaPaginator(discord.ui.View):
    def __init__(self, title="", sections=None, url="", current_page=0):
        super().__init__(timeout=None)
        self.title        = title
        self.sections     = sections or []
        self.url          = url
        self.current_page = current_page

    def create_embed(self):
        section = self.sections[self.current_page]
        embed   = discord.Embed(
            title=f"📚 {self.title}",
            description=section["text"],
            color=0xfc03f4,
            url=self.url
        )
        if section["image"]:
            embed.set_image(url=section["image"])
        embed.set_footer(
            text=f"Section {self.current_page + 1}/{len(self.sections)} • Wikipedia Bridge 🌸"
        )
        return embed

    def _build_embed_from_record(self, record: dict, page: int) -> discord.Embed:
        """Build an embed directly from a registry record (used after restart)."""
        sections = record["sections"]
        section  = sections[page]
        embed    = discord.Embed(
            title=f"📚 {record['title']}",
            description=section["text"],
            color=0xfc03f4,
            url=record["url"]
        )
        if section["image"]:
            embed.set_image(url=section["image"])
        embed.set_footer(
            text=f"Section {page + 1}/{len(sections)} • Wikipedia Bridge 🌸"
        )
        return embed

    @discord.ui.button(
        label="◀️ Back",
        style=discord.ButtonStyle.primary,
        disabled=True,
        custom_id="wiki:back"          # ← static custom_id
    )
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = await asyncio.to_thread(_get_wiki_record, str(interaction.message.id))
        if not record:
            return await interaction.response.send_message(
                "⚠️ Session data not found. Please run `/wikipedia` again! 🥺", ephemeral=True
            )
        new_page = record["current_page"] - 1
        sections = record["sections"]

        self.children[0].disabled = (new_page == 0)
        self.children[1].disabled = False

        await asyncio.to_thread(_update_wiki_page, str(interaction.message.id), new_page)
        await interaction.response.edit_message(
            embed=self._build_embed_from_record(record, new_page), view=self
        )

    @discord.ui.button(
        label="Next ▶️",
        style=discord.ButtonStyle.danger,
        custom_id="wiki:next"          # ← static custom_id
    )
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = await asyncio.to_thread(_get_wiki_record, str(interaction.message.id))
        if not record:
            return await interaction.response.send_message(
                "⚠️ Session data not found. Please run `/wikipedia` again! 🥺", ephemeral=True
            )
        new_page = record["current_page"] + 1
        sections = record["sections"]

        self.children[0].disabled = False
        self.children[1].disabled = (new_page == len(sections) - 1)

        await asyncio.to_thread(_update_wiki_page, str(interaction.message.id), new_page)
        await interaction.response.edit_message(
            embed=self._build_embed_from_record(record, new_page), view=self
        )


# ══════════════════════════════════════════════════════════════════════════════
# 🧹 JUNK BLOCK STRIPPER
# Removes every non-prose element before text extraction:
#   • <table>  — infoboxes, orbital/climate/data tables
#   • <figure> — thumbnail captions ("Earth as seen from Meteosat-12…")
#   • hatnote  — "Planet Earth redirects here. For other uses, see…"
#   • thumb    — floating image divs
#   • navbox   — navigation boxes
#   • toc      — table of contents
#   • reflist  — reference/footnote lists
# Tables are stripped in 4 passes to handle one level of nesting.
# ══════════════════════════════════════════════════════════════════════════════
def _strip_junk_blocks(html: str) -> str:
    # ── Tables (multi-pass for nesting) ──────────────────────────────────────
    for _ in range(4):
        html = re.sub(
            r'<table[^>]*>[\s\S]*?</table>',
            '', html, flags=re.IGNORECASE
        )

    # ── <figure> / image thumbnail blocks ────────────────────────────────────
    html = re.sub(r'<figure[^>]*>[\s\S]*?</figure>', '', html, flags=re.IGNORECASE)

    # ── Specific <div> classes that leak non-article text ────────────────────
    _BAD_CLASSES = [
        "hatnote",           # "X redirects here…"
        "thumb",             # floating image boxes
        "thumbinner",
        "thumbcaption",
        "navbox",            # navigation/category boxes
        "toc",               # table of contents
        "mw-references",     # reference lists
        "reflist",
        "side-box",
        "sistersitebox",
        "noprint",
        "mbox",              # maintenance / stub tags
        "ambox",
        "tmbox",
        "ombox",
        "cmbox",
        "fmbox",
        "dmbox",
        "plainlist",
        "infobox",           # extra safety net
    ]
    for cls in _BAD_CLASSES:
        html = re.sub(
            rf'<div[^>]+class="[^"]*{re.escape(cls)}[^"]*"[^>]*>[\s\S]*?</div>',
            '', html, flags=re.IGNORECASE
        )

    # ── <ul class="gallery …"> image galleries ───────────────────────────────
    html = re.sub(
        r'<ul[^>]+class="[^"]*gallery[^"]*"[^>]*>[\s\S]*?</ul>',
        '', html, flags=re.IGNORECASE
    )

    return html


# ══════════════════════════════════════════════════════════════════════════════
class CutestThing(commands.Cog):
    def __init__(self, bot):
        self.bot            = bot
        self.headers        = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S928B) Chrome/124.0.0.0"}
        self.log_channel_id = 1439421632056655983
        self.log_path       = "logs/command_track.log"

    async def cog_load(self):
        # ✨ Re-register the persistent paginator on every startup so old
        # Back/Next buttons still work after a restart — no more "Interaction Failed"!
        self.bot.add_view(WikipediaPaginator())

    async def log_bridge(self, interaction, query, status, pages=0, lang="en"):
        if not os.path.exists("logs"):
            os.makedirs("logs")
        now      = datetime.now()
        cmd_name = f"/{interaction.command.name}" if interaction.command else "/unknown"
        guild_id = interaction.guild_id or "DM"

        log_entry = (
            f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] | "
            f"User: {interaction.user.id} | Server: {guild_id} | "
            f"Channel: {interaction.channel_id} | Cmd: {cmd_name} | "
            f"Query: {query} | Lang: {lang} | Status: {status}\n"
        )
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(log_entry)

        log_channel = self.bot.get_channel(self.log_channel_id)
        if log_channel:
            embed = discord.Embed(
                title="Command Tracked! 🌟",
                color=0xfc03f4 if "SUCCESS" in status else 0xff0000,
                timestamp=now
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.add_field(
                name="👤 User Information",
                value=(
                    f"**Name:** {interaction.user} ({interaction.user.mention})\n"
                    f"**User ID:** `{interaction.user.id}`\n"
                    f"**Server ID:** `{guild_id}`"
                ),
                inline=False
            )
            embed.add_field(name="⌨️ Command", value=f"`{cmd_name}`", inline=True)
            embed.add_field(name="🌐 Lang",    value=f"`{lang}`",     inline=True)
            embed.add_field(name="🔍 Query",   value=f"`{query}`",    inline=False)
            embed.add_field(
                name="📊 Result",
                value=f"Status: `{status}` | Pages: `{pages}`",
                inline=True
            )
            embed.set_footer(
                text=f"Requested by {interaction.user.name}",
                icon_url=interaction.user.display_avatar.url
            )
            try:
                await log_channel.send(embed=embed)
            except:
                pass

    async def language_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=n, value=c)
            for n, c in COMMON_LANGS.items() if current.lower() in n.lower()
        ][:25]

    async def wikipedia_autocomplete(self, interaction: discord.Interaction, current: str):
        lang = interaction.namespace.language or "en"
        if not current:
            return []
        url = (
            f"https://{lang}.wikipedia.org/w/api.php"
            f"?action=opensearch&format=json"
            f"&search={urllib.parse.quote(current)}&limit=10"
        )
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=2) as resp:
                    data = await resp.json()
                    return [app_commands.Choice(name=t, value=t) for t in data[1]]
        except:
            return []

    # ── /wikipedia ─────────────────────────────────────────────────────────────
    @app_commands.command(name="wikipedia", description="Read Wikipedia Pages! 📖")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="What would you like to search for? 😊✨️",
        language="Language code (en, id, ja)"
    )
    @app_commands.autocomplete(query=wikipedia_autocomplete, language=language_autocomplete)
    async def wikipedia(
        self,
        interaction: discord.Interaction,
        query: str,
        language: str = "en"
    ):
        try:
            await interaction.response.defer(thinking=True)
        except:
            return

        if not interaction.app_permissions.embed_links:
            return await interaction.followup.send(
                "❌ **Permission Error:** I need the **Embed Links** permission to show Wikipedia articles beautifully! 🥺",
                ephemeral=True
            )

        lang = language.lower().strip()

        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:

                # ── STEP 1: RESOLVE TITLE ─────────────────────────────────────
                resolve_url = (
                    f"https://{lang}.wikipedia.org/w/api.php"
                    f"?action=query&titles={urllib.parse.quote(query)}&redirects=1&format=json"
                )
                async with session.get(resolve_url, timeout=10) as r_resp:
                    r_data      = await r_resp.json()
                    pages       = r_data.get("query", {}).get("pages", {})
                    page_id     = list(pages.keys())[0]

                    if page_id == "-1":
                        await self.log_bridge(interaction, query, "NOT_FOUND", lang=lang)
                        return await interaction.followup.send("❌ Article not found!")
                    exact_title = pages[page_id].get("title")

                # ── STEP 2: FETCH FULL CONTENT ────────────────────────────────
                parse_url = (
                    f"https://{lang}.wikipedia.org/w/api.php"
                    f"?action=parse&page={urllib.parse.quote(exact_title)}"
                    f"&prop=text|images|displaytitle&format=json&redirects=1&disabletoc=1"
                )
                async with session.get(parse_url, timeout=15) as p_resp:
                    p_data = await p_resp.json()
                    if "parse" not in p_data:
                        return await interaction.followup.send("❌ Could not parse article.")

                    raw_html      = p_data["parse"]["text"]["*"]
                    display_title = p_data["parse"]["displaytitle"]
                    clean_title   = re.sub('<[^<]+?>', '', display_title)
                    article_url   = (
                        f"https://{lang}.wikipedia.org/wiki/"
                        f"{exact_title.replace(' ', '_')}"
                    )

                    # Grab first real article image as fallback thumbnail
                    main_img_match = re.search(
                        r'src="(//upload\.wikimedia\.org/.*?)"', raw_html
                    )
                    main_img = f"https:{main_img_match.group(1)}" if main_img_match else None

                    # ── STEP 3: SPLIT BY SECTION ──────────────────────────────
                    sections      = re.split(r'<h[23].*?>(.*?)</h[23]>', raw_html)
                    sections_list = []

                    # ─────────────────────────────────────────────────────────
                    def add_content(header, html_content):
                        # ✨ Grab image before junk stripper eats it!
                        img_match   = re.search(
                            r'src="(//upload\.wikimedia\.org/.*?)"', html_content
                        )
                        section_img = f"https:{img_match.group(1)}" if img_match else None

                        # ══ STRIP JUNK ════════════════════════════════════════
                        html_content = _strip_junk_blocks(html_content)

                        # ══ FORMATTING ════════════════════════════════════════
                        html_content = re.sub(
                            r'<(br\s*/?|/p|/tr|/li|/div|/h[1-6])[^>]*>',
                            '\n', html_content, flags=re.IGNORECASE
                        )
                        html_content = re.sub(
                            r'<(/td|/th|/span)[^>]*>',
                            ' ', html_content, flags=re.IGNORECASE
                        )
                        html_content = re.sub(
                            r'<(style|script)[^>]*>[\s\S]*?</\1>',
                            '', html_content, flags=re.IGNORECASE
                        )

                        text = re.sub(r'<[^<]+?>', '', html_content).strip()
                        text = re.sub(r'\[[0-9a-zA-Z\s,]+\]', '', text)
                        text = re.sub(r'\.mw-parser-output[\s\S]*?\{[\s\S]*?\}', '', text, flags=re.DOTALL)
                        text = re.sub(r'\{[^\}]*?\}', '', text)

                        for word in ["Tampilkan globe", "Tampilkan peta", "Bendera", "Lambang negara", "Semboyan:"]:
                            text = text.replace(word, "")

                        text = re.sub(r'[ \t]+', ' ', text)
                        text = re.sub(r'\n\s*\n+', '\n\n', text)
                        text = text.strip()

                        if len(text) < 30:
                            return

                        # ══ SPLIT INTO CHUNKS ═════════════════════════════════
                        chunks = textwrap.wrap(
                            text, 1500,
                            break_long_words=False,
                            replace_whitespace=False
                        )
                        for i, chunk in enumerate(chunks):
                            prefix = f"### {header}\n\n" if i == 0 and header else ""
                            sections_list.append({
                                "text":  prefix + chunk.strip(),
                                # ✨ Set the image for the first chunk of the section
                                "image": section_img if i == 0 else None
                            })
                    # ─────────────────────────────────────────────────────────

                    add_content(None, sections[0])
                    for i in range(1, len(sections), 2):
                        header_name  = re.sub('<[^<]+?>', '', sections[i])
                        content_body = sections[i + 1]
                        add_content(header_name, content_body)

                    # ── ✨ SMART IMAGE PASS ✨ ───────────────────────────────
                    # This logic makes sure an image "stays" until a NEW one appears!
                    # It also makes sure page 1 always has the main article photo.
                    current_img = main_img
                    for entry in sections_list:
                        if entry["image"] is not None:
                            current_img = entry["image"] # Update if we found a new section pic
                        else:
                            entry["image"] = current_img # Carry the old pic forward!

                    await self.log_bridge(
                        interaction, exact_title,
                        "SUCCESS_DEEP", len(sections_list), lang=lang
                    )

                    if not sections_list:
                        return await interaction.followup.send("⚠️ This article is empty.")

                    view = WikipediaPaginator(clean_title, sections_list, article_url)
                    msg  = await interaction.followup.send(embed=view.create_embed(), view=view)

                    # ── Persist paginator state for post-restart button support ─
                    await asyncio.to_thread(_save_wiki_record, str(msg.id), {
                        "title":        clean_title,
                        "sections":     sections_list,
                        "url":          article_url,
                        "current_page": 0,
                    })

        except Exception as e:
            await self.log_bridge(interaction, query, f"ERROR: {str(e)[:50]}", lang=lang)
            await interaction.followup.send(f"⚠️ Error: {e}")


async def setup(bot):
    await bot.add_cog(CutestThing(bot))
