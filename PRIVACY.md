# Privacy Policy — Cutest Thing

**Last updated:** 4 September 2026  
**Applies to:** the Operator’s running instance of **Cutest Thing**, as implemented in [this repository](https://github.com/itsmilkieee93/Cutest-Thing-Public-Bot).

Operator: **its.milkieee**

If you fork or self-host the code, this policy does **not** cover your instance. Publish your own.

This is a project policy, **not legal advice**. It is written so Discord users can see what the code actually collects, why, where it goes, and how to ask for deletion.

Related document: [Terms of Service](./TERMS.md)

---

## 1. When collection happens

The Bot is **not** a silent logger of every message in a server.

Data is processed when you:

- mention the Bot, reply to it, or DM it (`libraries/groq_service.py`)
- run a slash command
- attach a file a command is asked to read (images for vision or `/edit-image`, files for `/upload`)
- use a command that **reads channel history** (`/summarize`, `/random-msg`, `/clear-msg` scanning the Bot’s own messages)
- use `/gemini-reply` / similar “reply to this message” commands (the target message content is included in the prompt)

When the Bot **joins** a guild, it can cache structure Discord already grants it (members, roles, channels) via `libraries/resources/shared.py`.

---

## 2. What is collected

### Identity and location

- Discord user ID, username, display name, avatar URL (for log embeds)
- Guild ID and name; channel ID and name
- Whether the chat is a server channel or a DM
- For DMs: the DM channel snowflake (folder name for memory)

### Content you send to the Bot

- Message text
- Reply, forward, and embed text used as context for a mention
- Image bytes / URLs for vision
- Slash-command arguments (prompts, city names, search queries, message IDs, etc.)
- Files you attach to commands that accept them

### Conversation memory (host disk)

| Store | Path in code | Shape |
| --- | --- | --- |
| Groq mention/reply/DM chat | `groq/memory/{guild_id}/memory.db` or `groq/memory/dm/{channel_id}/memory.db` | SQLite, one row per `user_id` in that bucket; recent turns (code cap on the order of **200** Groq turns) |
| Gemini slash chat | `gemini/memory/{model}/chat_{username}_{user_id}.json` | JSON history trimmed to about **90** turns |

Memory is **not** shared across servers. Each DM channel has its own file.

### Server cache (per invited guild)

`cache/guild_data/{guild_id}/`

| File | Typical fields |
| --- | --- |
| `metadata.db` | Name, owner ID, created date, member count, description, icon/banner URLs, locale, safety-level flags |
| `roles.db` | Roles, members the Bot can see, member–role links |
| `channels.db` | Channel names, types, topics, position |

Used so chat can answer “who owns this server?” / “what roles exist?” without guessing.

### Persona settings

`interactions/persona/{guild_id}/` — nick, bio, avatar, banner, name style if someone uses `/server-persona-*`.

### Message whitelist

`config/msg_whitelist.db` — guild IDs registered via `/register-server` (requires the Moderate Members permission). Extends the `/msg` and `/forward-msg` allow-list beyond the static config file. No user data, only the guild ID and who registered it (for the log embed).

### Channel history (only for specific commands)

- `/summarize` — recent messages in the channel, sent to an AI summarizer
- `/random-msg` — samples this channel’s history
- `/clear-msg` — walks recent history to delete the **Bot’s** own messages

### Operational logs (three streams)

1. **`logs/bot.log`** — process stdout/stderr from `bot.py`
2. **`bridge_log`** in `libraries/resources/shared.py` — slash traces to `logs/command_track.log` **and** Discord channels in `log_channel_ids` (user, server, channel, command, short result, user avatar)
3. **`log_webhook.py`** — every print / warning / traceback batched to a private Discord webhook
4. **Groq activity embeds** — model name, user, server or DM, channel ID, rate-limit headers, and (current build) prompt / response text

The Operator keeps the log channel private (`@everyone` without View Channel cannot see it). Anyone **with** access to that channel can read what was shipped there.

### Config lists on the host

Owner IDs, blocked user IDs, allowed server IDs, log channel IDs, webhook URL. These are operational controls.

---

## 3. What we do not try to collect

- Payment cards or government ID
- Voice or video call contents
- Every message in a channel where the Bot was not invoked (except the history commands above)
- Data from servers the Bot has not been invited to

`/speed-test` measures the **host machine’s** network, not the user’s home connection.

---

## 4. Why

| Purpose | Code behavior |
| --- | --- |
| Reply | Groq / Gemini / OpenRouter / Cloudflare |
| Keep context | SQLite / JSON memory |
| Server Q&A | Guild cache |
| Safety | Classifiers in `groq_instruct.py` / Groq pipeline; user block list |
| Keep the Bot running | Webhook logs, `bridge_log`, rate-limit headers |

We do **not** sell personal information.  
We do **not** use Discord message content to train **our own** models.  
Third-party inference providers process prompts so the Bot can answer. Groq’s public position is that API inference is not used for training by default: https://console.groq.com/docs/your-data

---

## 5. Who else receives data

Depends on which feature you used:

| Processor | Triggered by |
| --- | --- |
| **Groq** | Mentions, replies, DMs, vision, some fallbacks |
| **Google Gemini / Gemma** | `/gemini`, `/gemini-reply` |
| **OpenRouter** | `/openrouter` commands |
| **Cloudflare Workers AI** | `/cloudflare-ai` commands |
| **Exa** and news/page-fetch fallbacks | Grounded Groq answers |
| **Pexels / Pixabay / Unsplash** | Image / video search |
| **YouTube / yt-dlp / YouTube Music** | `/youtube`, `/music`, `/download-music`, `/livestream-chat`, `/my-channel` |
| **Wikipedia, weather, news APIs** | Matching slash commands |
| **Joke / quote / trivia / compliment APIs** | `/dadjoke`, `/quote`, `/fact`, `/advice`, `/question`, `/praise`, and related |
| **Discord** | Normal bot API traffic plus the Operator’s log embeds and webhook |

Each of those services has its own terms and privacy policy. The Operator does not control their retention clocks.

---

## 6. How long it is kept

- **Groq / Gemini memory:** until overwritten by newer turns, the host files are deleted, or a deletion request is completed
- **Guild cache:** until refreshed, the Bot leaves, or `cache/guild_data/{id}` is deleted
- **Persona files:** until reset in-bot or deleted on the host
- **Message whitelist (`msg_whitelist.db`):** until an Operator removes the row on the host — no in-bot removal command yet
- **`logs/bot.log`, `command_track.log`, Discord log channel, webhook history:** until the Operator clears them

Third-party APIs may keep a short copy for abuse or reliability. Host deletion does not unwind a prompt already sent to Groq or Gemini.

---

## 7. Your choices

- Stop using the Bot; block it; ask an admin to kick it
- Do not send anything you do not want stored or forwarded
- Ask the Operator to delete **your** Groq row and/or Gemini JSON for a user ID (Contact)
- Server admins can kick the Bot to stop new collection in that guild

There is **no** public `/forget` command in the current tree. Deletion of host files is manual.

---

## 8. Children

The Bot is only for people who already meet Discord’s age rules. We do not knowingly store data from children under 13. If that may have happened, contact us; we will delete host copies we control.

Some commands only run in Discord age-restricted servers or channels, as coded.

---

## 9. Security

- Log channel is intended to deny View Channel for `@everyone`
- Memory, cache, `auth/token.txt`, and keys belong on the host — **not** in git
- `.gitignore` currently lists `.env` only; self-hosters should also ignore `logs/`, `*.db`, `auth/token.txt`, and webhook URLs

No setup is perfect. A stolen owner account, bot token, or webhook can expose logs. Do not send secrets to the Bot.

---

## 10. International processing

The host (phone or VPS) and the APIs above may sit in other countries than you. If that is not acceptable, do not use the Bot.

---

## 11. Changes

Updates will be committed to this file. The “Last updated” date will change.

---

## 12. Contact

- Issues: https://github.com/itsmilkieee93/Cutest-Thing-Public-Bot/issues  
- Discord: **its.milkieee**

For deletion, send the Discord user ID and say whether you want **server memory**, **DM memory**, **Gemini JSON**, or all of them removed.
