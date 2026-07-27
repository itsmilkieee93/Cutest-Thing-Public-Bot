import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
from datetime import datetime
import re

class NewsPaginator(discord.ui.View):
    def __init__(self, news_items, lang_name):
        super().__init__(timeout=86400)
        self.news = news_items
        self.lang_name = lang_name
        self.current_page = 0

    def create_embed(self):
        item = self.news[self.current_page]
        
        # Clean text
        raw_text = item.get('story', 'No details available.')
        clean_text = re.sub(r'\[[0-9a-zA-Z\s,]+\]', '', raw_text)
        clean_text = re.sub(r'<[^<]+?>', '', clean_text).strip()
        
        embed = discord.Embed(
            title=f"🌍 Wikipedia News: {self.lang_name}",
            description=clean_text,
            color=0x2ecc71
        )
        
        if item.get('links'):
            article = item['links'][0]
            embed.url = article.get('content_urls', {}).get('desktop', {}).get('page')
            
            img_url = article.get('originalimage', {}).get('source') or article.get('thumbnail', {}).get('source')
            if img_url:
                embed.set_image(url=img_url)

        embed.set_footer(text=f"Headline {self.current_page + 1}/{len(self.news)} ")
        return embed

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page - 1) % len(self.news)
        await interaction.response.edit_message(embed=self.create_embed())

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page + 1) % len(self.news)
        await interaction.response.edit_message(embed=self.create_embed())

class NewsModule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Your S24 Ultra Headers
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.64 Mobile Safari/537.36",
            "X-Requested-With": "com.android.chrome"
        }

    @app_commands.command(name="wiki-news", description="Get world news via Wikipedia! 📰")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(language="Select the language edition of Wikipedia! 😊❤️")
    @app_commands.choices(language=[
        app_commands.Choice(name="English 🇺🇸", value="en"),
        app_commands.Choice(name="Indonesian 🇮🇩", value="id"),
        app_commands.Choice(name="Korean 🇰🇷", value="ko"),
        app_commands.Choice(name="Japanese 🇯🇵", value="ja"),
        app_commands.Choice(name="German 🇩🇪", value="de"),
        app_commands.Choice(name="French 🇫🇷", value="fr")
    ])
    async def news(self, interaction: discord.Interaction, language: str = "en"):
        await interaction.response.defer(thinking=True)
        
        # 🌈 Smart Permission Check
        perms = interaction.app_permissions
        if not perms.embed_links:
            return await interaction.followup.send("I need the **Embed Links** permission to show you the news beautifully! 🥺🙏")
        
        # Friendly names for the footer/title
        lang_names = {"en": "English", "id": "Bahasa Indonesia", "ko": "Korean", "ja": "Japanese", "de": "German", "fr": "French"}
        display_name = lang_names.get(language, "Unknown")

        now = datetime.now()
        url = f"https://{language}.wikipedia.org/api/rest_v1/feed/featured/{now.year}/{now.strftime('%m')}/{now.strftime('%d')}"
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url) as resp:
                    # Some languages don't support the 'featured' feed API properly
                    if resp.status != 200:
                        return await interaction.followup.send(f"⚠️ The **{display_name}** Wikipedia doesn't have a featured news feed for today.")
                    
                    data = await resp.json()
                    news_items = data.get('news', [])
                    
                    if not news_items:
                        # Fallback: some languages put news under 'onthisday' instead
                        return await interaction.followup.send(f"📰 No breaking news reported in **{display_name}** today. They might not use this section.")

                    view = NewsPaginator(news_items, display_name)
                    await interaction.followup.send(embed=view.create_embed(), view=view)
                    
        except Exception as e:
            await interaction.followup.send(f"⚠️ Connection Error: {e}")

async def setup(bot):
    await bot.add_cog(NewsModule(bot))
