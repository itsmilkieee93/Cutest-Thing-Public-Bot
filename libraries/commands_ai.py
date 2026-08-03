import random
import discord
from discord import app_commands
import asyncio
from resources.shared import bridge_log, load_gemini_memory, save_gemini_memory, reply_to_autocomplete

# =====================================================================
# 🛰️ BRIDGE AI COMMANDS
# =====================================================================

# ─────────────────────────────────────────────────────────────────────────────
# Pastel palette & loading GIFs  (from summarize.py / photo_editor.py)
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

# ─────────────────────────────────────────────────────────────────────────────
# Model choices — Gemma 4 (open, via Gemini API) + Gemini 3.1 / 3.5 Lite tiers
# ─────────────────────────────────────────────────────────────────────────────
MODEL_CHOICES = [
    app_commands.Choice(name="Gemma 4 26B A4B (Fast/MoE)",        value="gemma-4-26b-a4b-it"),
    app_commands.Choice(name="Gemma 4 31B (Dense/Quality)",       value="gemma-4-31b-it"),
    app_commands.Choice(name="Gemini 3.1 Flash-Lite",             value="gemini-3.1-flash-lite"),
    app_commands.Choice(name="Gemini 3.5 Flash-Lite",             value="gemini-3.5-flash-lite"),
]
DEFAULT_MODEL = "gemma-4-26b-a4b-it"


def _loading_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=random.choice(PASTEL_COLORS),
    )
    embed.set_thumbnail(url=random.choice(LOADING_GIFS))
    return embed


def _result_embed(
    interaction: discord.Interaction,
    title: str,
    response_text: str,
    footer_note: str = "",
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=response_text,
        color=random.choice(PASTEL_COLORS),
    )
    footer_text = f"Requested by {interaction.user.display_name}"
    if footer_note:
        footer_text += f" • {footer_note}"
    embed.set_footer(
        text=footer_text,
        icon_url=interaction.user.display_avatar.url,
    )
    return embed


def _error_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="⚠️ Something went wrong",
        description=message,
        color=0xF4978E,
    )


@app_commands.command(name="gemini", description="Consult Gemini AI with Memory 🧠")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(model=MODEL_CHOICES)
@app_commands.describe(
    thinking="Enable thinking mode (only applied if the chosen model supports it) 🧠",
    no_personality="Disable the bot's personality for this reply (neutral, no character) 🎭",
)
async def gemini_cmd(
    interaction: discord.Interaction,
    model: str = DEFAULT_MODEL,
    thinking: bool = False,
    no_personality: bool = False,
    *,
    prompt: str,
):
    await interaction.response.defer()

    # 🌸 Let the user know upfront if thinking was requested but the model
    # they picked doesn't actually support it, instead of silently ignoring it.
    thinking_supported = interaction.client.ai.model_supports_thinking(model)
    loading_desc = "Consulting the AI, one moment! ✨\n\nHang tight... 💕"
    if thinking and not thinking_supported:
        loading_desc += f"\n\n⚠️ *Thinking mode isn't supported on `{model}` — continuing without it.*"

    loading_msg = await interaction.followup.send(
        embed=_loading_embed("🧠 Thinking...", loading_desc),
        wait=True,
    )
    try:
        user    = interaction.user
        history = load_gemini_memory(model, user.name, user.id)
        history.append({"role": "user", "content": prompt})

        response_text = await asyncio.to_thread(
            interaction.client.ai.get_ai_response,
            prompt,
            interaction.guild_id,
            model,
            None,           # personality_override
            thinking,       # enable_thinking
            no_personality, # disable_personality
        )

        history.append({"role": "assistant", "content": response_text})
        save_gemini_memory(model, user.name, user.id, history[-90:])

        await bridge_log(interaction, "gemini", prompt, response_text)

        footer_bits = []
        if thinking and thinking_supported:
            footer_bits.append("🧠 Thinking on")
        if no_personality:
            footer_bits.append("🎭 Personality off")
        footer_note = " • ".join(footer_bits)

        if len(response_text) > 4096:
            # Embed descriptions cap at 4096 chars — chunk into multiple embeds
            chunks = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
            await loading_msg.edit(
                embed=_result_embed(
                    interaction, "🌸 Gemini Response (1/{})".format(len(chunks)), chunks[0], footer_note
                )
            )
            for idx, chunk in enumerate(chunks[1:], start=2):
                await interaction.channel.send(
                    embed=_result_embed(interaction, f"🌸 Gemini Response ({idx}/{len(chunks)})", chunk, footer_note)
                )
        else:
            await loading_msg.edit(embed=_result_embed(interaction, "🌸 Gemini Response", response_text, footer_note))
    except Exception as e:
        await loading_msg.edit(embed=_error_embed(f"`{str(e)[:200]}`"))


@app_commands.command(name="gemini-reply", description="Make Gemini reply to a specific message! 🎯")
@app_commands.autocomplete(message_id=reply_to_autocomplete)
@app_commands.choices(model=MODEL_CHOICES)
@app_commands.describe(
    thinking="Enable thinking mode (only applied if the chosen model supports it) 🧠",
    no_personality="Disable the bot's personality for this reply (neutral, no character) 🎭",
)
async def ai_reply_cmd(
    interaction: discord.Interaction,
    message_id:  str,
    instruction: str = "Reply naturally.",
    model:       str = DEFAULT_MODEL,
    thinking:    bool = False,
    no_personality: bool = False,
):
    await interaction.response.defer(ephemeral=True)

    thinking_supported = interaction.client.ai.model_supports_thinking(model)
    loading_desc = "Reading the message and drafting a reply! ✨\n\nHang tight... 💕"
    if thinking and not thinking_supported:
        loading_desc += f"\n\n⚠️ *Thinking mode isn't supported on `{model}` — continuing without it.*"

    loading_msg = await interaction.followup.send(
        embed=_loading_embed("🎯 Preparing reply...", loading_desc),
        wait=True,
        ephemeral=True,
    )
    try:
        if not message_id or not message_id.isdigit():
            await loading_msg.edit(embed=_error_embed("Valid Message ID required."))
            return

        target_message = await interaction.channel.fetch_message(int(message_id))

        # 🌸 No need to load/inject personality manually here — get_ai_response
        # already builds it dynamically (from the bot's current per-guild
        # nickname, see personality.py) and sets it as the system_instruction —
        # unless no_personality is set, in which case it's skipped entirely.
        ai_prompt = (
            f"CONTEXT: Replying to {target_message.author.name}: \"{target_message.content}\"\n"
            f"USER DIRECTION: {instruction}"
        )
        response_text = await asyncio.to_thread(
            interaction.client.ai.get_ai_response,
            ai_prompt,
            interaction.guild_id,
            model,
            None,           # personality_override
            thinking,       # enable_thinking
            no_personality, # disable_personality
        )

        await bridge_log(interaction, "ai-reply", f"ID: {message_id} | Instr: {instruction}", response_text)

        footer_bits = []
        if thinking and thinking_supported:
            footer_bits.append("🧠 Thinking on")
        if no_personality:
            footer_bits.append("🎭 Personality off")
        footer_note = " • ".join(footer_bits)

        if len(response_text) > 4096:
            chunks = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
            await target_message.reply(embed=_result_embed(interaction, "🌸 Reply (1/{})".format(len(chunks)), chunks[0], footer_note))
            for idx, chunk in enumerate(chunks[1:], start=2):
                await interaction.channel.send(
                    embed=_result_embed(interaction, f"🌸 Reply ({idx}/{len(chunks)})", chunk, footer_note)
                )
        else:
            await target_message.reply(embed=_result_embed(interaction, "🌸 Reply", response_text, footer_note))

        await loading_msg.edit(embed=_result_embed(interaction, "✅ Sent!", "Your reply has been posted! 💕"))
    except Exception as e:
        await loading_msg.edit(embed=_error_embed(f"`{str(e)[:200]}`"))


# =====================================================================
# 🛠️ SETUP
# =====================================================================
def setup_commands(bot):
    bot.tree.add_command(gemini_cmd)
    bot.tree.add_command(ai_reply_cmd)
