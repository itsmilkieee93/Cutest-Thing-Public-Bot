import discord
from discord import app_commands
import asyncio
from resources.shared import bridge_log, load_gemini_memory, save_gemini_memory, reply_to_autocomplete, _load_file

# =====================================================================
# 🛰️ BRIDGE AI COMMANDS
# =====================================================================

@app_commands.command(name="gemini", description="Consult Gemini AI with Memory 🧠")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(model=[
    app_commands.Choice(name="Gemini 3.1 Flash-Lite (Fast/New)", value="gemini-3.1-flash-lite-preview"),
    app_commands.Choice(name="Gemini 2.5 Flash (Stable)",        value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 2.5 Flash Lite (Stable)",   value="gemini-2.5-flash-lite"),
])
async def gemini_cmd(interaction: discord.Interaction, model: str = "gemini-2.5-flash", *, prompt: str):
    await interaction.response.defer()
    try:
        user    = interaction.user
        history = load_gemini_memory(model, user.name, user.id)
        history.append({"role": "user", "content": prompt})

        response_text = await asyncio.to_thread(interaction.client.ai.get_ai_response, prompt, model)

        history.append({"role": "assistant", "content": response_text})
        save_gemini_memory(model, user.name, user.id, history[-90:])

        await bridge_log(interaction, "gemini", prompt, response_text)

        if len(response_text) > 2000:
            chunks = [response_text[i:i+1900] for i in range(0, len(response_text), 1900)]
            await interaction.followup.send(f"{user.mention}\n{chunks[0]}")
            for chunk in chunks[1:]:
                await interaction.channel.send(chunk)
        else:
            await interaction.followup.send(f"{user.mention}\n{response_text}")
    except Exception as e:
        await interaction.followup.send(f"⚠️ **Error:** `{str(e)[:100]}`", ephemeral=True)


@app_commands.command(name="gemini-reply", description="Make Gemini reply to a specific message! 🎯")
@app_commands.autocomplete(message_id=reply_to_autocomplete)
@app_commands.choices(model=[
    app_commands.Choice(name="Gemini 2.5 Flash (Stable)",          value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 2.5 Flash Lite",              value="gemini-2.5-flash-lite"),
    app_commands.Choice(name="Gemini 3.1 Flash-Lite (Unstable)",   value="gemini-3.1-flash-lite-preview"),
])
async def ai_reply_cmd(
    interaction: discord.Interaction,
    message_id:  str,
    instruction: str = "Reply naturally.",
    model:       str = "gemini-2.5-flash-lite"
):
    await interaction.response.defer()
    try:
        if not message_id or not message_id.isdigit():
            await interaction.followup.send("⚠️ Valid Message ID required.", ephemeral=True)
            return

        target_message = await interaction.channel.fetch_message(int(message_id))
        personality    = _load_file("gemini/configuration/personality.txt") or "You are a helpful AI."

        ai_prompt = (
            f"SYSTEM_INSTRUCTIONS:\n{personality}\n\n"
            f"CONTEXT: Replying to {target_message.author.name}: \"{target_message.content}\"\n"
            f"USER DIRECTION: {instruction}"
        )
        response_text = await asyncio.to_thread(interaction.client.ai.get_ai_response, ai_prompt, model)

        await bridge_log(interaction, "ai-reply", f"ID: {message_id} | Instr: {instruction}", response_text)

        if len(response_text) > 2000:
            chunks = [response_text[i:i+1950] for i in range(0, len(response_text), 1950)]
            await target_message.reply(content=chunks[0])
            for chunk in chunks[1:]:
                await interaction.channel.send(content=chunk)
        else:
            await target_message.reply(content=response_text)

        await interaction.followup.send("✅ Sent!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(content=f"❌ Error: `{str(e)[:100]}`", ephemeral=True)


# =====================================================================
# 🛠️ SETUP
# =====================================================================
def setup_commands(bot):
    bot.tree.add_command(gemini_cmd)
    bot.tree.add_command(ai_reply_cmd)
