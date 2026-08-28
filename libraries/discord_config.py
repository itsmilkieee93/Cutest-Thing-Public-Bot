"""
🌸 Cutest Thing — Discord Configuration
Central place for every Discord snowflake ID used across the bot.

⚠️ This file contains REAL IDs — keep it in .gitignore, never commit it.
   Commit discord_config.example.py instead (same structure, placeholder values).
"""

BOT = {
    # Your personal Discord user ID (Developer Mode on → right-click your
    # name → Copy User ID). Grants owner-only commands like !sync / !reload.
    # Kept for backward compatibility — prefer "owner_ids" below for new code.
    "owner_id": 000000000000000000,

    # Multiple owner/trusted-user IDs, all granted the same owner-only
    # permissions as owner_id above. Add as many snowflakes as you want —
    # e.g. co-developers, a trusted mod, an alt account for testing.
    "owner_ids": [000000000000000000, 000000000000000001],

    # Server used for instant slash-command syncing during development.
    # Guild syncs are instant; global syncs can take up to 1 hour to propagate.
    "test_guild_id": 000000000000000000,

    # Servers allowed to use restricted commands (e.g. /msg) (separated by commas)
    "allowed_server_ids": [000000000000000000, 000000000000000001],

    # User IDs banned from using specific commands — every command/interaction
    # should check this and silently ignore (or send a short refusal) before
    # doing anything else. Add as many snowflakes as needed.
    "blocked_user_ids": [000000000000000000],

    # Unix timestamp for the bot's "creation date", shown in /about.
    "birthday_ts": 0000000000,

    # Channel ID(s) where bridge_log() posts the "Command Executed! 🌟" embed
    # for every slash command run. Add as many channels as you want — e.g.
    # a private dev log + a public activity feed.
    "log_channel_ids": [000000000000000000],
}

WEBHOOKS = {
    # Private channel webhook that bot.log (stdout/stderr) gets streamed to
    # by log_shipper.py. Server Settings → Integrations → Webhooks → New.
    "log_webhook_url": "https://discord.com/api/webhooks/000000000000000000/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
}

COMMANDS = {
    # Slash-command IDs, used to render clickable </command:id> links in
    # embeds (e.g. the /help menu). Discord assigns these when commands
    # are synced — check console output after tree.sync(), or in Discord,
    # right-click a command → Copy Command ID.

    "ai": {  # AI chat commands
        "gemini": 000000000000000000,
        "gemini_reply": 000000000000000001,
        "openrouter": 000000000000000002,
        "openrouter_reply": 000000000000000003,
        "cloudflare_ai": 000000000000000004,
        "cloudflare_ai_reply": 000000000000000005,
    },
    "messaging": {  # Send / edit / relay message commands
        "msg": 000000000000000000,
        "edit_msg": 000000000000000001,
        "dm_user": 000000000000000002,
        "forward_msg": 000000000000000003,
        "embed_msg": 000000000000000004,
    },
    "interaction": {  # Reactions & moderation commands
        "react": 000000000000000000,
        "unreact": 000000000000000001,
        "clear_msg": 000000000000000002,
        "safs": 000000000000000003,
        "unf": 000000000000000004,
    },
    "system": {  # Bot status & utility commands
        "info": 000000000000000000,
        "about": 000000000000000001,
        "speed_test": 000000000000000002,
        "encrypt": 000000000000000003,
        "decrypt": 000000000000000004,
        "upload": 000000000000000005,
    },
    "math": {  # Math solver commands
        "math": 000000000000000000,
        "math_ref": 000000000000000001,
    },
    "knowledge": {  # Fun facts, quotes & info-lookup commands
        "fact": 000000000000000000,
        "quote": 000000000000000001,
        "advice": 000000000000000002,
        "dadjoke": 000000000000000003,
        "question": 000000000000000004,
        "wikipedia": 000000000000000005,
        "wiki_news": 000000000000000006,
        "news": 000000000000000007,
        "weather": 000000000000000008,
        "praise": 000000000000000009,
        "joke_unf": 000000000000000010,
    },
    "media": {  # Music & YouTube commands
        "music": 000000000000000000,
        "download_music": 000000000000000001,
        "youtube": 000000000000000002,
        "livestream_chat": 000000000000000003,
        "my_channel": 000000000000000004,
    },
    "images": {  # Photo search commands
        "photo": 000000000000000000,
        "pexels": 000000000000000001,
    },
}

