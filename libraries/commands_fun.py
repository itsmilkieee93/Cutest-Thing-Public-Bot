import discord
from discord import app_commands
import random
import json
import aiohttp
from resources.shared import bridge_log, joke_emojis, quote_emojis
from config_loader import is_owner

# =====================================================================
# 🔴 GLOBAL COMMAND: /joke-unf  (needs a View, so defined at module level)
# =====================================================================

class JokeConfirmView(discord.ui.View):
    def __init__(self, original_interaction, category, public, target=None):
        super().__init__(timeout=60)
        self.original_interaction = original_interaction
        self.category = category
        self.public   = public
        self.target   = target

    @discord.ui.button(label="I UNDERSTAND, PROCEED ⚠️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="🔄 Please Wait 😅...", embed=None, view=None)

        base_url = f"https://v2.jokeapi.dev/joke/{self.category}"
        params   = {} if self.category == "Dark" else {"safe-mode": ""}

        final_content = "❌ Something went wrong."
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(base_url, params=params) as resp:
                    data = await resp.json()

                if data.get("error"):
                    final_content = "❌ API Error: Joke retrieval failed."
                else:
                    emoji   = random.choice(joke_emojis)
                    emoji_2 = random.choice(joke_emojis)
                    mention = f"{self.original_interaction.user.mention} "

                    if data["type"] == "single":
                        final_content = f"{mention}{data['joke'].strip('.')} {emoji}"
                    else:
                        setup    = data['setup'].strip(".")
                        delivery = data['delivery'].strip(".")
                        final_content = f"{mention}{setup} {emoji}\n||{delivery} {emoji_2}||"

                if self.public:
                    perms = interaction.app_permissions
                    if perms.send_messages:
                        await self.original_interaction.channel.send(final_content)
                        await interaction.edit_original_response(content="✅ Joke released via Channel.", embed=None, view=None)
                    else:
                        await self.original_interaction.followup.send(final_content, ephemeral=False)
                        await interaction.edit_original_response(content="✅ Joke released via Followup.", embed=None, view=None)
                else:
                    await interaction.edit_original_response(content=final_content, embed=None, view=None)

            except Exception as e:
                await interaction.edit_original_response(content=f"⚠️ Connection Error: `{e}`")

        await bridge_log(interaction, "joke-unf-exec", {"category": self.category, "public": str(self.public)}, final_content)


@app_commands.command(name="joke-unf", description="Allow The Bot to send unfiltered jokes (MAY BE INAPPROPRIATE! 😭)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(category="Select content type", public="Visibility setting", target="Optional: mention a user with the joke")
@app_commands.choices(category=[
    app_commands.Choice(name="Any 🎲",         value="Any"),
    app_commands.Choice(name="Programming 💻", value="Programming"),
    app_commands.Choice(name="Misc 🃏",        value="Misc"),
    app_commands.Choice(name="Dark 💀",        value="Dark"),
    app_commands.Choice(name="Pun 🎤",         value="Pun"),
])
async def joke_unf(interaction: discord.Interaction, category: str = "Any", public: bool = True, target: discord.Member = None):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return

    # 🔒 Server + channel gate — bot owner bypasses everything below.
    if not is_owner(interaction.user.id):
        guild = interaction.guild

        if guild is None:
            await interaction.followup.send(
                "🚫 **Unavailable here.** `/joke-unf` can only be used in a server marked "
                "**NSFW / Age-Restricted / Explicit**, inside an age-restricted channel.",
                ephemeral=True
            )
            return

        server_is_explicit = guild.nsfw_level in (
            discord.NSFWLevel.explicit,
            discord.NSFWLevel.age_restricted,
        )
        if not server_is_explicit:
            await interaction.followup.send(
                "🚫 **Server Not Eligible.** This server isn't marked **NSFW / Age-Restricted / Explicit** "
                "in Discord's Server Settings, so `/joke-unf` is disabled here.",
                ephemeral=True
            )
            return

        channel = interaction.channel
        if not getattr(channel, "nsfw", False):
            await interaction.followup.send(
                "🚫 **Wrong Channel.** This server qualifies, but `/joke-unf` must be run in an "
                "**age-restricted (NSFW) channel**. 🔞",
                ephemeral=True
            )
            return

    status = "🌐 PUBLIC" if public else "🔐 PRIVATE (EPHEMERAL)"
    embed  = discord.Embed(
        title="🛑 BE CAREFUL!! ⚠️",
        description=(
            "### **System Protocol: Unfiltered Output**\n"
            "You are about to request content that bypassed standard safety filters. "
            "Please acknowledge the following risks:\n\n"
            "* **Offensive Content:** The output may be highly offensive, inappropriate, or crude.\n"
            "* **Discord TOS:** Proceeding may generate content that violates Discord Terms of Service if shared maliciously.\n"
            "* **Server Rules:** Usage of this protocol must strictly adhere to your specific server guidelines.\n\n"
            "**The bot developer and server staff are not responsible for any mental or social distress caused by the resulting output.**"
        ),
        color=0xff0dd7
    )
    embed.add_field(name="📂 Category", value=f"`{category}`", inline=True)
    embed.add_field(name="👁️ Visibility", value=f"`{status}`", inline=True)
    embed.set_footer(text="⚠️ Failure to comply with server rules may result in a ban.")

    view = JokeConfirmView(interaction, category, public, target=target)
    try:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        await bridge_log(interaction, "joke-unf-prompt", {"category": category, "public": str(public)}, "Awaiting confirmation")
    except Exception as e:
        print(f"❌ Error sending joke confirmation: {e}")


# =====================================================================
# 🌸 GLOBAL COMMAND: /praise
# =====================================================================

@app_commands.command(name="praise", description="Praise Someone 🥰 (User @ or a Message (id) 🎉")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    target="The person to praise (@mention)",
    message_id="Paste a Message ID to praise the author and reply (optional)",
    ping="Choose True to ping/notify the person, False for a 'silent' blue link 🎀"
)
async def praise(interaction: discord.Interaction, target: discord.User = None, message_id: str = None, ping: bool = True):
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
    except:
        return

    if target and message_id:
        await interaction.followup.send("❌ **Error:** Too many arguments! Please use either @User OR a Message ID, not both 😭", ephemeral=True)
        return
    if not target and not message_id:
        await interaction.followup.send("❌ **Error:** You must provide a @User or a Message ID 🥺", ephemeral=True)
        return

    try:
        async with interaction.client.session.get("https://compliments-api.vercel.app/random", timeout=15) as response:
            res = await response.json()

        clean_text = res['compliment'].strip(".") + "!"
        emoji      = random.choice(["🌸", "✨", "💝", "💎", "🕊️", "😊", "💖", "🥰", "🥹", "🎉", "🎀", "🫶", "🫰"])
        content    = f"{target.mention}, {clean_text} {emoji}" if target else f"{clean_text} {emoji}"
        mentions   = discord.AllowedMentions(users=ping)
        perms      = interaction.app_permissions

        if message_id and message_id.isdigit():
            try:
                target_msg = await interaction.channel.fetch_message(int(message_id))
                if perms.send_messages:
                    await target_msg.reply(content, mention_author=ping, allowed_mentions=mentions)
                    await interaction.followup.send(f"✅ Public reply delivered! (Silent: {not ping}) 🌸", ephemeral=True)
                else:
                    await interaction.followup.send(content, ephemeral=True, allowed_mentions=mentions)
                await bridge_log(interaction, "praise", f"ID: {message_id} | Ping: {ping}", clean_text)
                return
            except:
                await interaction.followup.send("⚠️ **ID Error:** Message not found.", ephemeral=True)
                return

        if target:
            if perms.send_messages:
                await interaction.channel.send(content, allowed_mentions=mentions)
                await interaction.followup.send(f"✅ Public praise delivered! (Silent: {not ping}) 🌸", ephemeral=True)
            else:
                await interaction.followup.send(content, ephemeral=True, allowed_mentions=mentions)
            await bridge_log(interaction, "praise", f"User: {target.name} | Ping: {ping}", clean_text)

    except Exception as e:
        await interaction.followup.send("🌸 System is currently stabilizing. Please try again later 🥲.", ephemeral=True)


# =====================================================================
# 🛠️ SETUP — includes bot-tree commands as inner functions
# =====================================================================
def setup_commands(bot):
    # Register module-level global commands
    bot.tree.add_command(joke_unf)
    bot.tree.add_command(praise)

    # =====================================================================
    # 🧠 COMMAND: /fact
    # =====================================================================
    @bot.tree.command(name="fact", description="Get a random interesting fact 💡 ")
    async def fact(interaction: discord.Interaction):
        try:
            await interaction.response.defer(thinking=True)
        except:
            return
        try:
            async with interaction.client.session.get("https://uselessfacts.jsph.pl/api/v2/facts/random", timeout=15) as response:
                res = await response.json()
            fact_text = res['text']
            emoji     = random.choice(["🧐", "🧠", "📚", "🎓", "🔍", "🌍", "💡"])
            await interaction.followup.send(f"**Did you know?** {fact_text} {emoji}")
            await bridge_log(interaction, "fact", "UselessFacts API", fact_text)
        except:
            await interaction.followup.send("⚠️ The library is closed for cleaning. Try again later!")

    # =====================================================================
    # 🌟 COMMAND: /advice
    # =====================================================================
    @bot.tree.command(name="advice", description="Get a random piece of life advice 💖 ")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def advice(interaction: discord.Interaction):
        try:
            await interaction.response.defer(thinking=True)
        except:
            return
        try:
            async with interaction.client.session.get("https://api.adviceslip.com/advice", timeout=15) as response:
                res = json.loads(await response.text())
            advice_text = res['slip']['advice']
            emoji       = random.choice(["🌱", "☀️", "🌹", "🕯️", "🌊", "🤝", "💖"])
            await interaction.followup.send(f"**Advice:** {advice_text} {emoji}")
            await bridge_log(interaction, "advice", "AdviceSlip API", advice_text)
        except:
            await interaction.followup.send("⚠️ I'm out of wisdom at the moment. Drink some water! 💧")

    # =====================================================================
    # 🟢 COMMAND: /dadjoke
    # =====================================================================
    @bot.tree.command(name="dadjoke", description="Get a classic dad joke 😂 ")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def dadjoke(interaction: discord.Interaction):
        try:
            await interaction.response.defer(thinking=True)
        except discord.errors.NotFound:
            return
        try:
            async with interaction.client.session.get(
                "https://icanhazdadjoke.com/",
                headers={"Accept": "application/json"},
                timeout=5
            ) as response:
                res = await response.json()
            emoji = random.choice(joke_emojis)
            text  = f"{res['joke'].strip('.')} {emoji}"
            await interaction.followup.send(text)
            await bridge_log(interaction, "dadjoke", "Global API", text)
        except:
            try:
                await interaction.followup.send("❌ Dad is busy.")
            except:
                pass

    # =====================================================================
    # 📖 COMMAND: /quote
    # =====================================================================
    @bot.tree.command(name="quote", description="Get a quote from ZenQuotes ❤️ ")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def quote(interaction: discord.Interaction):
        try:
            await interaction.response.defer(thinking=True)
        except:
            return
        try:
            async with interaction.client.session.get("https://zenquotes.io/api/random", timeout=15) as response:
                res = await response.json()
            q, a    = res[0]['q'].strip("."), res[0]['a']
            e1, e2  = random.sample(quote_emojis, 2)
            text    = f"\u201c {q} {e1} \u201d \u2014 **{a}** {e2}"
            await interaction.followup.send(text)
            await bridge_log(interaction, "quote", "Global API", text)
        except:
            await interaction.followup.send("\u201c Stay positive! \u2728 \u201d \u2014 **System** \U0001f338")

    # =====================================================================
    # 🎲 COMMAND: /question
    # =====================================================================
    @bot.tree.command(name="question", description="Get a random trivia question")
    async def question(interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            import html
            async with interaction.client.session.get("https://opentdb.com/api.php?amount=1&type=multiple") as resp:
                data = await resp.json()
                res  = data['results'][0]
            q_text   = html.unescape(res['question'])
            category = res['category']
            answer   = html.unescape(res['correct_answer'])
            embed    = discord.Embed(
                title=f"🎲 Trivia Time!",
                description=f"**Category:** {category}\n\n{q_text}\n\n**Answer:** || {answer} ||",
                color=0x3498db
            )
            embed.set_footer(text="Tap the black box to reveal the answer!")
            await interaction.followup.send(embed=embed)
            await bridge_log(interaction, "question", "TriviaDB", f"Q: {q_text} | A: {answer}")
        except Exception as e:
            await interaction.followup.send("❌ My trivia brain is foggy. Try again later!")
