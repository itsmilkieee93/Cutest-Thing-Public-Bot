import discord
from discord import app_commands
from discord.ext import commands
from ytmusicapi import YTMusic
import asyncio
import sys

# Initialize YTMusic globally
ytm = YTMusic()

# 🔑 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

YT_API_KEY = (key_config.YOUTUBE_API_KEY or "").strip() or None

class YTMPaginator(discord.ui.View):
    def __init__(self, tracks, user):
        super().__init__(timeout=10800) # Long timeout for your 24/7 service 🎀
        self.tracks = tracks
        self.user = user
        self.index = 0

    async def create_content(self):
        """Generates the embed with high-precision stats, day names, and live clocks 🌸✨"""
        track = self.tracks[self.index]
        video_id = track.get('videoId')
        
        # 🔗 Links
        short_url = f"https://youtu.be/{video_id}" if video_id else "https://youtube.com"
        music_url = f"https://music.youtube.com/watch?v={video_id}"
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        
        # 📊 Stats & Detailed Date Fetching
        try:
            import aiohttp
            from datetime import datetime
            
            # ✨ Using cached API key loaded at startup 🌸
            if not YT_API_KEY:
                raise Exception("YT API key not loaded")
            
            sc_url = f"https://api.socialcounts.org/youtube-video-live-view-count/{video_id}"
            yt_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet&id={video_id}&key={YT_API_KEY}"
            
            async with aiohttp.ClientSession() as session:
                # 1. Fetch Live Stats (SocialCounts) 📈
                async with session.get(sc_url) as sc_resp:
                    sc_data = await sc_resp.json()
                    api_stats = sc_data.get('counters', {}).get('api', {})
                    views_formatted = f"{int(api_stats.get('viewCount', 0)):,}"
                    likes_formatted = f"{int(api_stats.get('likeCount', 0)):,}"
                
                # 2. Fetch Exact Date & Time (YouTube API) 📅
                async with session.get(yt_url) as yt_resp:
                    yt_data = await yt_resp.json()
                    if 'items' in yt_data and yt_data['items']:
                        # YouTube provides ISO format: 2026-05-10T19:01:04Z
                        raw_published = yt_data['items'][0]['snippet']['publishedAt']
                        
                        # Formatting to: Sunday, 10 May 2026 | 19:01:04 (UTC+0) 🎀
                        date_obj = datetime.strptime(raw_published, "%Y-%m-%dT%H:%M:%SZ")
                        pub_date = date_obj.strftime("%A, %d %B %Y | %H:%M:%S (UTC+0)")
                    else:
                        pub_date = "Unknown Date"
                        
        except Exception as e:
            print(f"Metadata Error: {e}")
            views_formatted = "N/A"
            likes_formatted = "N/A"
            pub_date = "Unknown Date"

        artist_name = track['artists'][0]['name'] if track.get('artists') else 'Unknown Artist'
        song_title = track.get('title', 'Unknown Title')
        duration = track.get('duration', 'N/A')
        
        description = (
            f"**🏞 Album:** {track.get('album', {}).get('name', 'Single')}\n"
            f"**👤 Publisher:** {artist_name} \n"
            f"**📅 Date Published:** {pub_date} \n"
            f"**⏱️ Duration:** {duration} \n"
            f"**👀 Views:** {views_formatted} ️\n"
            f"**👍 Likes:** {likes_formatted} ️\n\n"
            f"**🔗 Link:** [Open in Browser]({short_url}) ✨"
        )
        
        embed = discord.Embed(
            title=f"🎶 {song_title}",
            description=description,
            color=0xFFB6C1 # Signature Pink 🎀
        )
        
        if track.get('thumbnails'):
            embed.set_thumbnail(url=track['thumbnails'][-1]['url'])
        
        icon_link = "https://upload.wikimedia.org/wikipedia/commons/thumb/6/6a/Youtube_Music_icon.svg/1280px-Youtube_Music_icon.svg.png"
        
        embed.set_footer(
            text=f"YouTube Music | Result {self.index + 1} of {len(self.tracks)} ✨ ",
            icon_url=icon_link
        )
        return embed, music_url, video_url


    async def update_view(self, interaction: discord.Interaction):
        """Updates the buttons and embed during pagination 🔄"""
        # ✨ Added the await right here!
        embed, music_url, video_url = await self.create_content() 
        
        # Updates the link buttons to match the current song
        self.children[2].url = music_url
        self.children[3].url = video_url
        
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("This isn't your search! 😭🙏 But you can run it by typing /music! 🙂✨️", ephemeral=True)
        self.index = (self.index - 1) % len(self.tracks)
        await self.update_view(interaction)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("This isn't your search! 😭🙏 But you can run it by typing /music! 🙂✨️", ephemeral=True)
        self.index = (self.index + 1) % len(self.tracks)
        await self.update_view(interaction)

class MusicModule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._trending_cache = []
        self._last_update = 0

    async def get_trending_songs(self):
        """Fetches trending songs and saves them for 1 hour 🕒"""
        current_time = asyncio.get_event_loop().time()
        
        # If we have a cache and it's less than an hour old, use it!
        if self._trending_cache and (current_time - self._last_update < 3600):
            return self._trending_cache
        
        try:
            # Fetch fresh charts in the background
            charts = await asyncio.to_thread(ytm.get_charts)
            songs = charts.get('songs', {}).get('items', [])[:10]
            
            choices = []
            for track in songs:
                artist = track['artists'][0]['name'] if track.get('artists') else 'Trending'
                title = track['title']
                choices.append(app_commands.Choice(name=f"🔥 {artist} — {title}"[:100], value=f"{artist} {title}"[:100]))
            
            self._trending_cache = choices
            self._last_update = current_time
            return choices
        except Exception as e:
            print(f"Trending Error: {e}")
            return []

    async def music_autocomplete(self, interaction: discord.Interaction, current: str):
        """Now super fast with caching! ⚡"""
        try:
            # ✨ If empty, return the cached trending songs (Instant!)
            if not current:
                return await self.get_trending_songs()

            # ✨ If typing, perform the search as usual
            search_results = await asyncio.to_thread(ytm.search, current, filter="songs", limit=10)
            choices = []
            for track in search_results:
                artist = track['artists'][0]['name'] if track.get('artists') else 'Unknown'
                title = track.get('title', 'Unknown Title')
                choices.append(app_commands.Choice(name=f"{artist} — {title}"[:100], value=f"{artist} {title}"[:100]))
            return choices

        except Exception:
            return []

    @app_commands.command(name="music", description="Search YouTube Music! 🎵💞")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Check out trends or search for a song! ✨")
    @app_commands.autocomplete(query=music_autocomplete)
    async def music(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)
        
        # Final search to fetch full details/thumbnails
        results = await asyncio.to_thread(ytm.search, query, filter="songs", limit=5)
        
        if not results:
            return await interaction.followup.send("❌ I couldn't find that! 🥺 Maybe it's hiding? 👉👈")

        view = YTMPaginator(results, interaction.user)
        
        # ✨ FIXED LINE: Added 'await' because create_content is now async!
        embed, music_url, video_url = await view.create_content() 
        
        # 🎧 Button 1: Music Interface
        view.add_item(discord.ui.Button(label="YT Music", url=music_url, emoji="🎧"))
        
        # 📲 Button 2: Main YouTube App
        view.add_item(discord.ui.Button(label="YouTube", url=video_url, emoji="📲"))
        
        await interaction.followup.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(MusicModule(bot))
