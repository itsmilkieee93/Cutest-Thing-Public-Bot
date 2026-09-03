# Terms of Service — Cutest Thing

**Last updated:** 4 September 2026  
**Source of truth:** this document is written from the public code in [itsmilkieee93/Cutest-Thing-Public-Bot](https://github.com/itsmilkieee93/Cutest-Thing-Public-Bot).

Operator: **its.milkieee** (“Operator,” “we,” “us”)  
Bot name: **Cutest Thing**

By inviting the Bot, using a slash command, mentioning or replying to it, sending it a DM, or running this code, you agree to these Terms. If you do not agree, remove the Bot and stop using it.

This is a project policy, **not legal advice**. It does not replace:

- [Discord Terms of Service](https://discord.com/terms)
- [Discord Community Guidelines](https://discord.com/guidelines)
- [Discord Developer Terms](https://support-dev.discord.com/hc/articles/8562894815383)
- [Discord Developer Policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy)
- provider rules such as [Groq Acceptable Use](https://console.groq.com/docs/legal/ai-policy)

Related document: [Privacy Policy](./PRIVACY.md)

---

## 1. Independent project

Cutest Thing is a self-hosted Python bot (`discord.py`). It is **not** made, endorsed, or operated by Discord Inc., Groq, Google, OpenAI, Meta, Cloudflare, OpenRouter, Exa, Pexels, Pixabay, Unsplash, YouTube, Wikipedia, JokeAPI, or any other API the code calls.

“Discord” and related marks belong to Discord Inc.

---

## 2. What the Bot does (from the code)

### Chat (no slash command required)

If you **mention** the Bot, **reply** to it, or **DM** it, `libraries/groq_service.py` sends that text (and sometimes an attached image) to **Groq**. Replies can:

- keep recent conversation memory
- look at images you attach
- look up public facts (Exa / news fallbacks)
- search stock photos or videos
- suggest music
- answer questions about the current server using a local cache
- split a long or private answer into DMs when the instructions say so
- apply safety filters before and after the model runs

Personality text comes from `libraries/personality.py` and uses the Bot’s **current nickname in that server**.

### Slash commands currently registered

**AI**

| Command | What the code does |
| --- | --- |
| `/gemini`, `/gemini-reply` | Google Gemini / Gemma, with JSON memory |
| `/openrouter`, `/openrouter-reply` | OpenRouter models |
| `/cloudflare-ai`, `/cloudflare-ai-reply` | Cloudflare Workers AI |

**Info & knowledge**

| Command | What the code does |
| --- | --- |
| `/info`, `/help` | Bot status and command list |
| `/wikipedia`, `/wiki-news`, `/news` | Wikipedia / news APIs |
| `/weather` | Weather for a city you type |
| `/fact`, `/advice`, `/dadjoke`, `/quote`, `/question`, `/praise` | Public joke / quote / trivia APIs |

**Media**

| Command | What the code does |
| --- | --- |
| `/music` | YouTube Music search |
| `/youtube` | Video / playlist metadata via yt-dlp |
| `/livestream-chat` | Recent YouTube live chat line |
| `/my-channel` | Operator’s YouTube channel stats |
| `/photo` | Unsplash |
| `/pexels` | Pexels |
| `/edit-image` | Local photo filters |

**Server & messages**

| Command | What the code does |
| --- | --- |
| `/server-info` | Server metadata |
| `/server-persona-profile`, `/server-persona-style` | Per-guild nick, bio, avatar, banner, name style |
| `/summarize` | Reads recent channel messages and summarizes them |
| `/random-msg` | Picks a random message from channel history |
| `/permission` | Lists the Bot’s Discord permissions |
| `/embed-msg` | Custom embed |
| `/forward-msg` | Forwards a message |
| `/msg`, `/edit-msg`, `/clear-msg` | Send / edit / delete the Bot’s own messages |
| `/register-server` | Mods with Moderate Members register a server ID into a local whitelist that extends `/msg` and `/forward-msg` access beyond the config file |
| `/react`, `/unreact` | Add or remove the Bot’s reaction |
| `/dm-user` | Mods with Manage Messages can ask the Bot to DM a user |

**Tools**

| Command | What the code does |
| --- | --- |
| `/math`, `/math-ref` | Math helper |
| `/encrypt`, `/decrypt` | Base64 / emoji cipher helpers (not military-grade encryption) |
| `/speed-test` | Speed test of the **host** internet, not yours |
| `/upload` | Re-send a file you attach |
| `/status-set`, `/status-remove` | Bot presence (owner-oriented) |
| `/reload` | Reload modules (owner only) |

Some commands are limited to servers on an allow-list, to the Bot owner, or to Discord age-restricted channels. The live list is whatever Discord shows after `/`.

---

## 3. Who may use it

- You must already be allowed to use Discord (Discord’s own minimum age, commonly 13+).
- The Operator may limit the Bot to specific guild IDs and may block user IDs.
- A server admin who invites the Bot is responsible for that community, including telling members that messages sent **to** the Bot are processed.
- User-installable commands (Discord “user apps”) still send your input to the same processors.

---

## 4. Acceptable use

You may not use the Bot to:

1. Break Discord rules, a provider AUP, or applicable law
2. Harass, threaten, or target people
3. Request or share sexual content involving minors, or any other illegal content
4. Try to extract tokens, passwords, private keys, or other people’s private data
5. Spam, raid, scrape, or overload Discord or the Bot
6. Bypass safety filters
7. Impersonate the Operator, Discord Staff, or another person in a misleading way
8. Treat Bot output as medical, legal, financial, or safety advice

The Operator may refuse a request, block a user ID, leave a server, or take the Bot offline.

Do not paste secrets into chat with the Bot. Logs and API calls can include what you typed.

---

## 5. AI output

Replies come from third-party models. They can be wrong, outdated, or blunt. Filters can block a prompt. A block is not a debate.

---

## 6. Data

See the [Privacy Policy](./PRIVACY.md). Short version: content you send **to** the Bot can be stored on the host and sent to AI / search APIs. Operational logs can be copied to a private log channel. Data is used to run and secure the Bot, not to sell ads.

---

## 7. Self-hosting

If you run your own copy:

- **you** are that instance’s operator
- these Terms do not automatically cover your users
- publish your own Terms and Privacy Policy
- do not commit `.env`, `auth/token.txt`, API keys, `memory.db`, webhook URLs, or `logs/`

---

## 8. Availability

No uptime promise. Models, commands, and memory can change. Discord or Groq rate limits can stop replies.

---

## 9. Intellectual property

You keep rights to content you submit. You grant the Operator a limited right to process it only to provide the Bot (including sending it to processors listed in the Privacy Policy).

Bot name, avatar, banner, and original assets belong to the Operator unless a license file says otherwise. This repository currently has **no license file** — ask before republishing it as your own public bot.

---

## 10. Disclaimer

THE BOT AND THIS SOURCE CODE ARE PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT.

TO THE MAXIMUM EXTENT PERMITTED BY LAW, THE OPERATOR IS NOT LIABLE FOR INDIRECT, INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES, LOST DATA, OR RELIANCE ON AI OUTPUT.

---

## 11. Changes

Updates will be committed to this file. The “Last updated” date will change. Using the Bot after that is acceptance of the new Terms.

---

## 12. Contact

- Issues: https://github.com/itsmilkieee93/Cutest-Thing-Public-Bot/issues  
- Discord: **its.milkieee**
