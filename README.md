<p align="center">
  <img src="./images/CutestThing_banner.png" alt="Cutest Thing banner" width="100%" />
</p>

<h1 align="center">Cutest Thing 🌸✨</h1>

<p align="center"><strong>Version 9.6.3</strong> — a self-hosted Discord bot written in Python (<code>discord.py</code>)</p>

<p align="center"><em>Floating on a strawberry cloud</em></p>

Mention it, reply to it, or DM it and it talks back using **Groq**. Slash commands cover extra AI providers, media, server tools, and utilities.

This repository is the public source for the Operator’s instance. It is **not** affiliated with Discord, Groq, Google, or any other API listed below.

- [Terms of Service](./TERMS.md)
- [Privacy Policy](./PRIVACY.md)

---

## Features

**Chat (no slash command needed)**

- Replies on **mention**, **reply to the bot**, and **DMs**
- Personality follows the bot’s **per-server nickname** (`libraries/personality.py`)
- Conversation memory split **per guild** and **per DM** (SQLite)
- Image attachments can be sent to a vision model
- Web / news lookup when a question needs current facts
- Stock photo & video search, music suggestions
- Server-aware answers from a local guild cache (owner, roles, channels)
- Safety checks on the Groq path
- Per-server look: nick, bio, avatar, banner, name style

**Also included**

Gemini / OpenRouter / Cloudflare AI slash chat, Wikipedia, news, weather, YouTube / yt-dlp, music download, photo editor, math, ciphers, embeds, forward, summarize, permissions, speed test of the **host**, and owner reload/sync.

---

## Requirements

- Python 3 (the public tree includes an Android/Termux native photo-editor `.so`; desktop Linux/Windows may need that cog to fail-soft or be rebuilt)
- A Discord application with these **Privileged Gateway Intents** enabled:
  - Message Content
  - Server Members
  - Presence (the process uses `discord.Intents.all()`)
- API keys for the features you actually use (Groq is required for mention chat)

Python packages: see [`requirements.txt`](./requirements.txt) (`discord.py`, `groq`, `google-genai`, `aiosqlite`, `yt-dlp`, `ytmusicapi`, `pytchat`, `Pillow`, `exa_py`, `speedtest-cli`, …).

```bash
python -m pip install -r requirements.txt
```

---

## Setup

### 1. Token and API keys

The live bot reads **`auth/key_config.py`**. Set `DISCORD_TOKEN` there — that is what `libraries/bot_service.py` uses on startup (`tkn = key_config.DISCORD_TOKEN`). The same file holds every other key.

`bot.py` still mentions `auth/token.txt`, but it does **not** pass that file unless you run `bot_service.py --token`. Treat `token.txt` as optional leftover.

Create the app at [Discord Developer Portal](https://discord.com/developers/applications) → Bot → Reset Token. Invite with scopes `bot` and `applications.commands`.

Placeholders in `key_config.py`:

| Variable | Used for |
| --- | --- |
| `DISCORD_TOKEN` | **Bot login** (required) |
| `GROQ_API_KEYS` | Mention / reply / DM chat (list; rotated on limits) |
| `GEMINI_API_KEYS` | `/gemini` |
| `OPENROUTER_API_KEYS` | `/openrouter` |
| `CLOUDFLARE_AI` (+ `_2`, `_3`) | `/cloudflare-ai` |
| `PEXELS_API_KEY` | `/pexels` |
| `UNSPLASH_CLIENT_ID` / `UNSPLASH_TOKEN` | `/photo` |
| `NEWS_API_KEY` | `/news` (Currents) |
| `YOUTUBE_API_KEY` | Channel / YouTube helpers |
| `WEATHER_API_KEY` | `/weather` |
| `HUGGING_FACE_API_KEY` | Optional HF calls |

You can also load secrets from a **`.env`** via `libraries/resources/env_loader.py` (`GROQ_API_KEYS=key1,key2`, Cloudflare `CLOUDFLARE_ACCOUNT_ID`, etc.).

**Never commit a filled `key_config.py`.** `.gitignore` currently only lists `.env` — also ignore `auth/key_config.py` (or keep a placeholder-only copy), `auth/token.txt`, `logs/`, `*.db`, `downloads/`, and webhook URLs.

### 2. Discord IDs

Copy the structure in [`libraries/discord_config.py`](./libraries/discord_config.py) and fill:

- `owner_id` / `owner_ids` — owner-only commands
- `test_guild_id` — instant slash sync while developing
- `allowed_server_ids` — servers allowed to use restricted commands such as `/msg`
- `blocked_user_ids`
- `log_channel_ids` — slash + Groq activity embeds
- `WEBHOOKS["log_webhook_url"]` — stdout/stderr streamed as embeds (`libraries/log_webhook.py`)
- `COMMANDS` — slash command snowflakes for clickable `/help` links (copy after first sync)

The comments in that file say to keep real IDs out of git and commit an example instead.

---

## Run

From the repo root:

```bash
python bot.py --on       # start (logs to logs/bot.log)
python bot.py --status
python bot.py --restart
python bot.py --off
```

`bot.py` is a small process manager. The real client is `libraries/bot_service.py` (`EnchantedBot`, prefix `Ct~!`).

There is a **network watchdog** (TCP probes) so a wifi/data handoff on Termux or a sleeping PC recycles the HTTP session instead of hanging forever.

After the first boot, the **owner** can run:

- `!sync` — `tree.sync()` (global slash commands can take up to an hour; use `test_guild_id` for instant guild sync)
- `!reload` or **`/reload`** — reload modules and re-register cogs

---

## How mention chat works

```
user mentions / replies / DMs
        ↓
libraries/groq_service.py   (interceptors: server Qs, media, music, attachments)
        ↓
libraries/groq_ai.py        (models, memory, safety)
        ↓
personality.py + shared.py  (name, history, guild summary)
        ↓
Groq API  →  Discord reply  →  save memory
```

Logs can go to:

1. `logs/bot.log`
2. `logs/command_track.log` + `log_channel_ids` (`bridge_log`)
3. The log webhook (all prints)
4. Groq “reply sent” embeds (model, user, rate limits, prompt/response in the current build)

Keep that log channel **private** (`@everyone` denied View Channel).

---

## Slash commands

Live global tree (54 commands). IDs are the current snowflakes — paste them into `libraries/discord_config.py` → `COMMANDS` so `/help` can render clickable mentions. IDs change if you delete and re-sync the command.

Chat **without** a slash command: mention the bot, reply to it, or DM it (Groq). Owner prefix: `!sync`, `!reload` (prefer `/reload`).

### AI

| # | Command | ID | What it does |
| ---: | --- | --- | --- |
| 1 | `/gemini` | `1538184079164575814` | Gemini / Gemma chat with JSON memory |
| 2 | `/gemini-reply` | `1538184079164575815` | Gemini replies to a chosen message |
| 39 | `/openrouter` | `1538184079533670408` | OpenRouter models |
| 40 | `/openrouter-reply` | `1538184079533670409` | OpenRouter reply to a message |
| 41 | `/cloudflare-ai` | `1538184079663697920` | Cloudflare Workers AI |
| 42 | `/cloudflare-ai-reply` | `1538184079663697921` | Cloudflare AI reply to a message |

### Fun & knowledge

| # | Command | ID | What it does |
| ---: | --- | --- | --- |
| 3 | `/joke-unf` | `1538184079164575816` | Joke command; code-gated to Discord age-restricted servers/channels |
| 4 | `/praise` | `1538184079164575817` | Send a compliment to a user or message |
| 5 | `/fact` | `1538184079164575818` | Random fact |
| 6 | `/advice` | `1538184079164575819` | Random advice |
| 7 | `/dadjoke` | `1538184079164575820` | Dad joke |
| 8 | `/quote` | `1538184079164575821` | Quote (ZenQuotes) |
| 9 | `/question` | `1538184079164575822` | Trivia question |
| 18 | `/wikipedia` | `1538184079298797655` | Wikipedia page |
| 22 | `/wiki-news` | `1538184079415967817` | News via Wikipedia |
| 23 | `/news` | `1538184079415967818` | News by category |
| 26 | `/weather` | `1538184079415967821` | Weather for a city you type |

### Messages

| # | Command | ID | What it does |
| ---: | --- | --- | --- |
| 10 | `/msg` | `1538184079164575823` | Send a message (allow-listed guilds / owner) |
| 11 | `/edit-msg` | `1538184079298797648` | Edit a message the bot sent |
| 12 | `/clear-msg` | `1538184079298797649` | Delete the bot’s own recent messages |
| 13 | `/react` | `1538184079298797650` | Add the bot’s reaction |
| 14 | `/unreact` | `1538184079298797651` | Remove the bot’s reaction |
| 15 | `/dm-user` | `1538184079298797652` | Mod-only: DM a user (Manage Messages) |
| 25 | `/forward-msg` | `1538184079415967820` | Forward a message |
| 32 | `/embed-msg` | `1538184079533670401` | Send a custom embed |

### Bot & safety stubs

| # | Command | ID | What it does |
| ---: | --- | --- | --- |
| 16 | `/safs` | `1538184079298797653` | Safety ping (“nominal”) |
| 17 | `/unf` | `1538184079298797654` | Stub unfold ping |
| 19 | `/info` | `1538184079298797656` | Heartbeat / connection |
| 20 | `/help` | `1538184079298797657` | Command manual |
| 21 | `/about` | `1538184079415967816` | About / birthday |
| 52 | `/reload` | `1538184079663697929` | Reload modules (owner) |
| 53 | `/status-set` | `1538184079793725460` | Set custom status |
| 54 | `/status-remove` | `1538184079793725461` | Clear custom status |

### Media

| # | Command | ID | What it does |
| ---: | --- | --- | --- |
| 24 | `/music` | `1538184079415967819` | YouTube Music search |
| 27 | `/youtube` | `1538184079415967822` | Video / playlist metadata (yt-dlp) |
| 31 | `/download-music` | `1538184079533670400` | Download audio (guilds only) |
| 35 | `/livestream-chat` | `1538184079533670404` | Recent YouTube live chat line |
| 36 | `/my-channel` | `1538184079533670405` | Operator’s YouTube stats |
| 37 | `/photo` | `1538184079533670406` | Unsplash photo |
| 38 | `/pexels` | `1538184079533670407` | Pexels search |
| 51 | `/edit-image` | `1538184079663697928` | Local photo filters |

### Server

| # | Command | ID | What it does |
| ---: | --- | --- | --- |
| 43 | `/summarize` | `1538184079663697922` | Summarize recent channel messages |
| 44 | `/permission` | `1538184079663697923` | List the bot’s Discord permissions |
| 45 | `/random-msg` | `1538184079663697924` | Random message from this channel’s history |
| 46 | `/server-info` | `1538184079663697925` | Server metadata |
| 47 | `/server-persona-profile` | `1544394262559330415` | Per-guild nick, bio, avatar, banner |
| 48 | `/server-persona-style` | `1544394262559330416` | Per-guild name font / effects / colors |
| 49 | `/server-persona-reset-profile` | `1544583853908164648` | Reset persona profile |
| 50 | `/server-persona-reset-style` | `1544583853908164649` | Reset persona style |

### Tools

| # | Command | ID | What it does |
| ---: | --- | --- | --- |
| 28 | `/speed-test` | `1538184079415967823` | Speed test of the **host** internet |
| 29 | `/encrypt` | `1538184079415967824` | Base64 / emoji cipher |
| 30 | `/decrypt` | `1538184079415967825` | Reverse the cipher |
| 33 | `/math` | `1538184079533670402` | Math solver |
| 34 | `/math-ref` | `1538184079533670403` | Math with a referenced message |

Some commands are owner-only, allow-listed to certain guilds, or limited to Discord age-restricted channels. `/download-music` is guild-only. `/speed-test` measures the host, not the user’s phone.

---

## Layout

```text
bot.py                      start/stop wrapper
auth/key_config.py          DISCORD_TOKEN + every API key (keep private)
images/                     default avatar / banner
libraries/
  bot_service.py            client, on_message, watchdog
  discord_commands.py       cog registration
  discord_config.py         snowflake IDs
  groq_service.py           mention pipeline
  groq_ai.py / groq_instruct.py / groq_exa_search.py
  groq_pexels.py / groq_music_suggestion.py
  extras/groq_attachments.py, groq_dm.py
  personality.py            system prompt from nickname
  resources/shared.py       Groq memory + guild cache + bridge_log
  log_webhook.py            stdout → Discord
  server_persona.py         per-guild profile/style
  gemini_service.py, openrouter.py, cloudflare_ai.py
  + one module per slash feature
TERMS.md  PRIVACY.md
requirements.txt
```

Runtime folders the process creates:

| Path | Purpose |
| --- | --- |
| `groq/memory/{guild_id}/memory.db` | Groq chat history (≈200 turns / user) |
| `groq/memory/dm/{channel_id}/memory.db` | DM history |
| `gemini/memory/.../*.json` | Gemini history (≈90 turns) |
| `cache/guild_data/{guild_id}/` | `metadata.db` `roles.db` `channels.db` |
| `interactions/persona/{guild_id}/` | persona SQLite |
| `logs/` | `bot.log`, `command_track.log` |
| `downloads/` | saved attachments |
| `libraries/status/status.json` | custom status text |

---

## Self-hosting

If you run your own copy, **you** are the operator of that instance. Publish your own Terms and Privacy Policy. Do not reuse this repo’s documents as if they applied to your keys and log channel.

Suggested `.gitignore` extras:

```gitignore
.env
auth/key_config.py
auth/token.txt
logs/
downloads/
*.db
groq/memory/
gemini/memory/
cache/guild_data/
```

---

## License

There is **no license file** in this repository. Ask the Operator before publishing a public fork as your own bot.

---

## Contact

- Issues: https://github.com/itsmilkieee93/Cutest-Thing-Public-Bot/issues
- Discord: **its.milkieee**
