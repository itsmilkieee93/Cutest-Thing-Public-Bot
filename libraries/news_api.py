import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import sys

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

class CurrentsPaginator(discord.ui.View):
    def __init__(self, news_items):
        super().__init__(timeout=86400)
        self.news = news_items
        self.current_page = 0

    def create_embed(self):
        article = self.news[self.current_page]
        
        title = article.get('title', 'No Title')
        desc = article.get('description', 'No description available.')
        url = article.get('url')
        img = article.get('image')
        author = article.get('author', 'Unknown Source')
        published = article.get('published', '')
        
        embed = discord.Embed(
            title=title[:250],
            description=desc[:1000],
            color=0x3498db, 
            url=url
        )
        
        if img and img != "None":
            embed.set_image(url=img)

        embed.set_footer(text=f"📰 {author} • {published} • Article {self.current_page + 1}/{len(self.news)} ")
        return embed

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page - 1) % len(self.news)
        await interaction.response.edit_message(embed=self.create_embed())

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page + 1) % len(self.news)
        await interaction.response.edit_message(embed=self.create_embed())

class CurrentsNewsModule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_key = self._load_key()
        self.cached_categories = []
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.64 Mobile Safari/537.36"
        }

    def _load_key(self):
        key = (key_config.NEWS_API_KEY or "").strip()
        return key or None

    async def cog_load(self):
        """Fetches all available categories on startup for the autocomplete"""
        if not self.api_key:
            return

        async with aiohttp.ClientSession() as session:
            url = f"https://api.currentsapi.services/v1/available/categories?apiKey={self.api_key}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.cached_categories = data.get('categories', [])

    async def category_autocomplete(self, interaction: discord.Interaction, current: str):
        """Dynamic category suggestions as you type"""
        return [
            app_commands.Choice(name=cat.title(), value=cat)
            for cat in self.cached_categories if current.lower() in cat.lower()
        ][:25]

    @app_commands.command(name="news", description="Browse top news by category! 🌍")
    @app_commands.describe(
        category="Filter by category (Start typing to see all options!)",
        language="Select your preferred language"
    )
    @app_commands.autocomplete(category=category_autocomplete)
    @app_commands.choices(language=[
        app_commands.Choice(name="English 🇺🇸", value="en"),
        app_commands.Choice(name="Indonesian 🇮🇩", value="id"),
        app_commands.Choice(name="Korean 🇰🇷", value="ko"),
        app_commands.Choice(name="Japanese 🇯🇵", value="ja"),
        app_commands.Choice(name="French 🇫🇷", value="fr")
    ])
    async def currents(self, interaction: discord.Interaction, 
                        category: str = None, 
                        language: str = "en"):
        await interaction.response.defer(thinking=True)
        
        if not self.api_key:
            return await interaction.followup.send("❌ Key missing in `key_config.NEWS_API_KEY`!")

        # Using the latest-news endpoint for better category results
        url = f"https://api.currentsapi.services/v1/latest-news?language={language}&apiKey={self.api_key}"
        if category:
            url += f"&category={category}"

        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url) as resp:
                    data = await resp.json()
                    articles = data.get('news', [])
                    
                    if not articles:
                        return await interaction.followup.send(f"📰 No recent headlines found for **{category or 'General'}**.")

                    view = CurrentsPaginator(articles)
                    await interaction.followup.send(embed=view.create_embed(), view=view)
        except Exception as e:
            await interaction.followup.send(f"⚠️  Error: {e}")

async def setup(bot):
    await bot.add_cog(CurrentsNewsModule(bot))
