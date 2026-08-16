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
    "owner_id": 1165555555268567040,

    # Multiple owner/trusted-user IDs, all granted the same owner-only
    # permissions as owner_id above. Add as many snowflakes as you want —
    # e.g. co-developers, a trusted mod, an alt account for testing.
    "owner_ids": [1165555555268567040, 813617286127550485],

    # Server used for instant slash-command syncing during development.
    # Guild syncs are instant; global syncs can take up to 1 hour to propagate.
    "test_guild_id": 1385237763816816710,

    # Servers allowed to use restricted commands (e.g. /msg) (separated by commas)
    "allowed_server_ids": [1385237763816816710, 1239881782308900874],

    # User IDs banned from using specific commands — every command/interaction
    # should check this and silently ignore (or send a short refusal) before
    # doing anything else. Add as many snowflakes as needed.
    "blocked_user_ids": [9876543210987654321],

    # Unix timestamp for the bot's "creation date", shown in /about.
    "birthday_ts": 1776210930,

    # Channel ID(s) where bridge_log() posts the "Command Executed! 🌟" embed
    # for every slash command run. Add as many channels as you want — e.g.
    # a private dev log + a public activity feed.
    "log_channel_ids": [1439421632056655983, 1537360337269956700],
}

WEBHOOKS = {
    # Private channel webhook that bot.log (stdout/stderr) gets streamed to
    # by log_shipper.py. Server Settings → Integrations → Webhooks → New.
    "log_webhook_url": "https://discord.com/api/webhooks/1537361199854002206/wr2-WZBxPqHsPeXnsLF2Fn9AqGfkiV1HAk6ESjwe5rBOq1qZVKY_I9SwGeuaoalfX14d",
}

COMMANDS = {
    # Slash-command IDs, used to render clickable </command:id> links in
    # embeds (e.g. the /help menu). Discord assigns these when commands
    # are synced — check console output after tree.sync(), or in Discord,
    # right-click a command → Copy Command ID.

    "ai": {  # AI chat commands
        "gemini": 1499372361869299712,
        "gemini_reply": 1509113355128799312,
        "openrouter": 1509097665122795590,
        "openrouter_reply": 1509116087776706562,
        "cloudflare_ai": 1509160938438266882,
        "cloudflare_ai_reply": 1509160938438266883,
    },
    "messaging": {  # Send / edit / relay message commands
        "msg": 1499363515725512709,
        "edit_msg": 1499363515725512711,
        "dm_user": 1499378105595465841,
        "forward_msg": 1507682331853328443,
        "embed_msg": 1507682332071559174,
    },
    "interaction": {  # Reactions & moderation commands
        "react": 1498221002205827194,
        "unreact": 1498229098277896203,
        "clear_msg": 1499801891813462288,
        "safs": 1506177506724679760,
        "unf": 1506177506724679761,
    },
    "system": {  # Bot status & utility commands
        "info": 1507682331853328436,
        "about": 1507682331853328438,
        "speed_test": 1507682332071559170,
        "encrypt": 1507682332071559171,
        "decrypt": 1507682332071559172,
        "upload": 1507682331853328442,
    },
    "math": {  # Math solver commands
        "math": 1507682332071559175,
        "math_ref": 1507682332071559176,
    },
    "knowledge": {  # Fun facts, quotes & info-lookup commands
        "fact": 1498340229441126430,
        "quote": 1499378105595465842,
        "advice": 1498340229441126431,
        "dadjoke": 1499378105595465840,
        "question": 1498992621777981620,
        "wikipedia": 1507682331853328435,
        "wiki_news": 1507682331853328439,
        "news": 1507682331853328440,
        "weather": 1507682331853328444,
        "praise": 1503056320821985332,
        "joke_unf": 1499363515725512707,
    },
    "media": {  # Music & YouTube commands
        "music": 1507682331853328441,
        "download_music": 1507682332071559173,
        "youtube": 1507682332071559169,
        "livestream_chat": 1507682332071559177,
        "my_channel": 1507682332071559178,
    },
    "images": {  # Photo search commands
        "photo": 1507682332545650718,
        "pexels": 1507682332545650719,
    },
}
