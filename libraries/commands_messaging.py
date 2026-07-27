import discord
from discord import app_commands
from typing import Union
from resources.shared import bridge_log, reply_to_autocomplete


def setup_commands(bot):

    # =====================================================================
    # 📡 COMMAND: /msg
    # =====================================================================
    @bot.tree.command(name="msg", description="Send a message! 😊")
    @app_commands.describe(
        content="Your message e.g hello world!",
        channel="Target channel (Optional in DMs/Current Channel)",
        reply_to="Select or paste a Message ID",
        ping_reply="Ping the user? (allowed mention @) 💫 (default: False)"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=True)
    @app_commands.autocomplete(reply_to=reply_to_autocomplete)
    async def msg(
        interaction: discord.Interaction,
        content:    str,
        channel:    Union[discord.TextChannel, discord.VoiceChannel, discord.Thread, discord.StageChannel] = None,
        reply_to:   str  = None,
        ping_reply: bool = False
    ):
        ALLOWED_SERVERS = [1385237763816816710, 1239881782308900874]
        OWNER_ID        = 1165555555268567040
        is_owner = interaction.user.id == OWNER_ID
        is_guild = interaction.guild_id is not None

        if not is_owner and is_guild and interaction.guild_id not in ALLOWED_SERVERS:
            return await interaction.response.send_message("🚫 Access Restricted!", ephemeral=True)

        target_dest = channel or interaction.channel
        try:
            await interaction.response.defer(ephemeral=True)
        except:
            return

        try:
            mentions = discord.AllowedMentions(
                everyone=False, roles=ping_reply, users=ping_reply, replied_user=ping_reply
            )
            log_args = {
                "dest":     getattr(target_dest, 'name', str(target_dest)),
                "reply_to": reply_to or "N/A",
                "ping":     str(ping_reply)
            }

            if reply_to and reply_to.isdigit():
                try:
                    target_msg = await target_dest.fetch_message(int(reply_to))
                    await target_msg.reply(content, allowed_mentions=mentions)
                    await interaction.followup.send("✅ **Replied** successfully! 💖", ephemeral=True)
                    await bridge_log(interaction, "msg", log_args, f"[REPLY] {content}")
                except:
                    await target_dest.send(content, allowed_mentions=mentions)
                    await interaction.followup.send("✅ **Sent** (as normal message)! 🌸", ephemeral=True)
                    await bridge_log(interaction, "msg", log_args, f"[SEND fallback] {content}")
            else:
                await target_dest.send(content, allowed_mentions=mentions)
                await interaction.followup.send("✅ **Sent** successfully! ✨", ephemeral=True)
                await bridge_log(interaction, "msg", log_args, f"[SEND] {content}")

        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            if is_owner:
                await interaction.followup.send(content, ephemeral=False, allowed_mentions=discord.AllowedMentions.all())
                await interaction.followup.send(
                    "⚠️ Remote send failed (Bot not in server), so I sent it here publicly via Webhook! ☁️",
                    ephemeral=True
                )
                await bridge_log(interaction, "msg", {"dest": "WEBHOOK FALLBACK", "ping": str(ping_reply)}, f"[WEBHOOK] {content}")
            else:
                await interaction.followup.send("❌ Error: I can't reach that destination! 🍓", ephemeral=True)
                await bridge_log(interaction, "msg", {"dest": str(target_dest), "ping": str(ping_reply)}, "❌ Failed: Forbidden/NotFound")

        except Exception as e:
            await interaction.followup.send(f"❌ **Error:** {str(e)[:50]}", ephemeral=True)
            await bridge_log(interaction, "msg", {"dest": str(target_dest)}, f"❌ Exception: {str(e)[:100]}")

    # =====================================================================
    # ✏️ COMMAND: /edit-msg
    # =====================================================================
    @bot.tree.command(name="edit-msg", description="Edit a message previously sent by the bot 📝")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(
        channel="The channel where the message is 👀",
        message_id="Select or paste the ID of the bot's message 😇",
        new_content="The updated text for the message 🙂"
    )
    @app_commands.autocomplete(message_id=reply_to_autocomplete)
    async def edit_msg(interaction: discord.Interaction, channel: discord.TextChannel, message_id: str, new_content: str):
        try:
            await interaction.response.defer(ephemeral=True)
        except:
            return
        try:
            if message_id == "ignore" or not message_id.isdigit():
                await interaction.followup.send("⚠️ Please provide a valid numeric Message ID.")
                return
            try:
                target_msg = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                await interaction.followup.send(f"❌ **Error:** Message ID `{message_id}` not found in {channel.mention}.")
                return
            if target_msg.author.id != interaction.client.user.id:
                await interaction.followup.send(
                    f"🚫 **Permission Denied:** You can only edit messages sent by **{interaction.client.user.display_name}**🥲\n"
                    f"Target message author: `{target_msg.author.display_name}`"
                )
                return
            old_preview = (target_msg.content[:96] + "...") if len(target_msg.content) > 96 else target_msg.content
            await target_msg.edit(content=new_content)
            await interaction.followup.send(f"✅ **Message Edited** in {channel.mention}!")
            await bridge_log(interaction, "edit-msg", f"Chan: #{channel.name} | Old: {old_preview}", new_content)
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ **Edit Error:** {str(e)[:100]}")
            except:
                pass

    # =====================================================================
    # 🗑️ COMMAND: /clear-msg
    # =====================================================================
    @bot.tree.command(name="clear-msg", description="Delete a specific amount of the bot's own messages 🧹")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=True)
    @app_commands.describe(amount="How many of my messages should I delete?")
    async def clear_msg(interaction: discord.Interaction, amount: int = 5):
        if amount < 1 or amount > 50:
            await interaction.response.send_message("⚠️ Please choose an amount between 1 and 50.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted_count = 0
        try:
            async for message in interaction.channel.history(limit=100):
                if message.author.id == interaction.client.user.id:
                    await message.delete()
                    deleted_count += 1
                if deleted_count >= amount:
                    break
            await bridge_log(interaction, "clear-msg", f"Amount requested: {amount}", f"Successfully purged {deleted_count} bot messages.")
            await interaction.followup.send(f"✅ Cleaned up `{deleted_count}` of my messages!", ephemeral=True)
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ **System Error:** `{str(e)[:100]}`", ephemeral=True)
            except:
                pass

    # =====================================================================
    # 🎭 COMMAND: /react
    # =====================================================================
    @bot.tree.command(name="react", description="Add a reaction to a message ⭐️")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=True)
    @app_commands.describe(channel="Target channel", message_id="Select a recent message to react to", emoji="Emoji (Standard or Custom)")
    @app_commands.autocomplete(message_id=reply_to_autocomplete)
    async def react(interaction: discord.Interaction, channel: discord.TextChannel, message_id: str, emoji: str):
        try:
            await interaction.response.defer(ephemeral=True)
        except:
            return
        try:
            if message_id == "ignore":
                await interaction.followup.send("⚠️ Please select a valid message from the list.")
                return
            clean_emoji = emoji.strip().replace('\ufe0f', '')
            target_msg  = await channel.fetch_message(int(message_id))
            await target_msg.add_reaction(clean_emoji)
            await interaction.followup.send(f"✅ Added {clean_emoji} to message by **{target_msg.author.display_name}**")
            await bridge_log(interaction, "react", f"Msg ID: {message_id}", f"Added {clean_emoji}")
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ **Error:** {str(e)[:50]}")
            except:
                pass

    # =====================================================================
    # 🗑️ COMMAND: /unreact
    # =====================================================================
    @bot.tree.command(name="unreact", description="Remove the bot's reaction from a message ❌️")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=True)
    @app_commands.describe(channel="Target channel", message_id="Select the message to remove reaction from", emoji="Emoji to remove")
    @app_commands.autocomplete(message_id=reply_to_autocomplete)
    async def unreact(interaction: discord.Interaction, channel: discord.TextChannel, message_id: str, emoji: str):
        try:
            await interaction.response.defer(ephemeral=True)
        except:
            return
        try:
            if message_id == "ignore":
                await interaction.followup.send("⚠️ Please select a valid message from the list.")
                return
            clean_emoji = emoji.strip().replace('\ufe0f', '')
            target_msg  = await channel.fetch_message(int(message_id))
            await target_msg.remove_reaction(clean_emoji, interaction.client.user)
            await interaction.followup.send(f"Removed {clean_emoji} from message by **{target_msg.author.display_name}** 🗑")
            await bridge_log(interaction, "unreact", f"Msg ID: {message_id}", f"Removed {clean_emoji}")
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ **Failed:** {str(e)[:50]}")
            except:
                pass

    # =====================================================================
    # 🛡️ COMMAND: /dm-user  [MOD ONLY]
    # =====================================================================
    @bot.tree.command(name="dm-user", description="[MOD ONLY] Send a DM via @Mention or User ID 😊")
    @app_commands.describe(target="The user to message", content="The message content")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def dm_user(interaction: discord.Interaction, target: discord.User, content: str):
        try:
            await interaction.response.defer(ephemeral=True)
        except:
            return
        try:
            await target.send(content)
            await interaction.followup.send(f"✅ **Mod DM Sent** to {target.mention}", ephemeral=True)
            await bridge_log(interaction, "dm-user", f"Target: {target.name}", content)
        except discord.Forbidden:
            await interaction.followup.send("❌ **Failed:** DMs closed.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ **Error:** {str(e)[:50]}", ephemeral=True)
