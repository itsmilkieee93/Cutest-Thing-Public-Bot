import discord
from discord import app_commands
from discord.ext import commands
import os
from datetime import datetime

class WeatherModule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_key_path = "auth/weather_api"

    def _load_api_key(self):
        try:
            if os.path.exists(self.api_key_path):
                with open(self.api_key_path, "r") as f:
                    return f.read().strip()
            return None
        except:
            return None

    # ✨ Optimized Autocomplete
    async def city_autocomplete(self, interaction: discord.Interaction, current: str):
        # 🎀 Threshold: Only search after 3 letters to reduce lag
        if not current or len(current) < 3:
            return []
            
        api_key = self._load_api_key()
        if not api_key: return []

        # 🚀 Using the bot's global session is MUCH faster!
        # We pass 'q' as a parameter so aiohttp handles spaces for us
        geo_url = "http://api.openweathermap.org/geo/1.0/direct"
        params = {'q': current, 'limit': 5, 'appid': api_key}
        
        try:
            async with self.bot.session.get(geo_url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                
                choices = []
                for loc in data:
                    state = f", {loc.get('state')}" if loc.get('state') else ""
                    # Example: "Jakarta, Special Capital Region of Jakarta, ID"
                    name = f"{loc['name']}{state}, {loc['country']}"
                    
                    # Discord limits choice names to 100 characters
                    choices.append(app_commands.Choice(name=name[:100], value=name[:100]))
                return choices
        except:
            return []

    @app_commands.command(name="weather", description="Get detailed weather from a city! 🌡️")
    @app_commands.describe(city="Start typing a city (e.g., Jakarta)...", unit="Celsius or Fahrenheit")
    @app_commands.choices(unit=[
        app_commands.Choice(name="Celsius (°C)", value="metric"),
        app_commands.Choice(name="Fahrenheit (°F)", value="imperial")
    ])
    @app_commands.autocomplete(city=city_autocomplete)
    async def weather(self, interaction: discord.Interaction, city: str, unit: app_commands.Choice[str]):
        await interaction.response.defer()
        
        api_key = self._load_api_key()
        if not api_key:
            return await interaction.followup.send("❌ API Key missing! 🥺")
        
        # 🚀 Using the global session here too
        url = "http://api.openweathermap.org/data/2.5/weather"
        params = {'q': city, 'appid': api_key, 'units': unit.value}
        
        try:
            async with self.bot.session.get(url, params=params) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(f"❌ Could not fetch data for **{city}**. 🥺")
                
                data = await resp.json()
                
                sym = "°C" if unit.value == "metric" else "°F"
                dist_unit = "km" if unit.value == "metric" else "mi"
                speed_unit = "m/s" if unit.value == "metric" else "mph"
                
                # Atmosphere Data
                temp = data['main']['temp']
                feels = data['main']['feels_like']
                min_t = data['main']['temp_min']
                max_t = data['main']['temp_max']
                hum = data['main']['humidity']
                pres = data['main']['pressure']
                vis = data.get('visibility', 0) / (1000 if unit.value == "metric" else 1609)
                
                # Sunrise/Sunset
                sunrise = datetime.fromtimestamp(data['sys']['sunrise']).strftime('%H:%M')
                sunset = datetime.fromtimestamp(data['sys']['sunset']).strftime('%H:%M')
                
                embed = discord.Embed(
                    title=f"Weather in {data['name']}, {data['sys']['country']} 🌍",
                    description=f"✨ **{data['weather'][0]['description'].title()}**",
                    color=0x03fff7
                )
                embed.set_thumbnail(url=f"http://openweathermap.org/img/wn/{data['weather'][0]['icon']}@4x.png")
                
                embed.add_field(name="🌡️ Temperature", value=f"**{temp}{sym}**\n(Feels like {feels}{sym})", inline=True)
                embed.add_field(name="📈 Range", value=f"Low: {min_t}{sym}\nHigh: {max_t}{sym}", inline=True)
                embed.add_field(name="💧 Humidity", value=f"{hum}%", inline=True)
                
                embed.add_field(name="🌀 Pressure", value=f"{pres} hPa", inline=True)
                embed.add_field(name="💨 Wind Speed", value=f"{data['wind']['speed']} {speed_unit}", inline=True)
                embed.add_field(name="👁️ Visibility", value=f"{vis:.1f} {dist_unit}", inline=True)
                
                embed.add_field(name="🌅 Sunrise", value=sunrise, inline=True)
                embed.add_field(name="🌇 Sunset", value=sunset, inline=True)
                embed.add_field(name="☁️ Clouds", value=f"{data['clouds']['all']}%", inline=True)

                embed.set_footer(text=f"Requested for {city} • Stay cozy! 😊❤️")
                await interaction.followup.send(embed=embed)
                    
        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{str(e)[:50]}`")

async def setup(bot):
    await bot.add_cog(WeatherModule(bot))
