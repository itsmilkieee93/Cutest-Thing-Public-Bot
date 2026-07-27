import discord
import time
import aiohttp
import os

def format_size(size_bytes):
    """Converts bytes to a cute, readable format 🎀"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"

async def save_to_downloads(interaction: discord.Interaction, attachment: discord.Attachment):
    """Privately tracks the background archiving process ✨"""
    start_time = time.time()
    downloaded = 0
    last_update_time = 0
    
    save_dir = "./downloads"
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    
    full_path = os.path.join(save_dir, attachment.filename)
    
    # 🎨 The Private Progress Embed
    embed = discord.Embed(
        title="☁️ Upload Processing...",
        description=f"Archiving **{attachment.filename}** in the background... ✨",
        color=0xFFB6C1
    )
    progress_msg = await interaction.followup.send(embed=embed, ephemeral=True)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as response:
                with open(full_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(512 * 1024): 
                        downloaded += len(chunk)
                        f.write(chunk)
                        
                        current_time = time.time()
                        if current_time - last_update_time > 1.5 or downloaded == attachment.size:
                            elapsed = current_time - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            percent = (downloaded / attachment.size) * 100
                            
                            bar = "■" * int(10 * downloaded // attachment.size) + "□" * (10 - int(10 * downloaded // attachment.size))
                            
                            new_embed = discord.Embed(
                                title="☁️ Upload Processing...",
                                description=(
                                    f"**Progress:** `{bar}` {percent:.1f}%\n"
                                    f"**Speed:** `{format_size(speed)}/s` ⚡"
                                ),
                                color=0xFFB6C1
                            )
                            await progress_msg.edit(embed=new_embed)
                            last_update_time = current_time

        # Final Success State (Only you see this!)
        await progress_msg.edit(content=f"✅ **Upload Complete!** Safely stored in the background. 🎀", embed=None)
        return full_path
        
    except Exception as e:
        await interaction.followup.send(f"❌ Processing error: `{e}`", ephemeral=True)
        return None
