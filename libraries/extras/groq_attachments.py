"""🌸 extras/groq_attachments.py

ATTACHMENT / IMAGE VISION — lets the bot actually SEE image attachments
a user posts alongside their message, using Groq's multimodal Qwen
vision models, instead of only ever reading message.clean_content text.

Source: https://console.groq.com/docs/vision (fetched live, since this
is new Groq surface area not in training data). Key facts from there
that shape this module's limits below:
  - qwen/qwen3.6-27b: image+text input, max 5 images/request, 20MB
    request size cap, ~2048 tokens per image.
  - qwen/qwen3.8-27b: image+text input, max 3 images/request, same
    20MB cap, same ~2048 tokens/image. Frontier-level reasoning/coding,
    but the STRICTER image count makes it the worse default when we
    don't know in advance how many images a message will have.
  - Images are passed as {"type": "image_url", "image_url": {"url":
    ...}} content blocks — either a real URL or a base64 data URI
    (`data:image/<ext>;base64,<...>`). Discord attachment URLs expire /
    aren't guaranteed publicly fetchable by Groq's servers depending on
    channel permissions, so this module downloads bytes itself via
    discord.Attachment.read() and sends base64 data URIs — the "locally
    saved image" pattern from Groq's docs — rather than passing
    attachment.url straight through.

This is a SEPARATE, dedicated vision call — not woven into
groq_ai.get_ai_response's main personality-driven conversation/history
pipeline. Keeps this additive: zero risk to MODEL_POOL rotation,
history, or personality plumbing. The caller (groq_service.py) gets
back a plain text description string and folds it into the normal text
`prompt` as its own labeled block — the same pattern already used for
forwarded-message text there — so the rest of the pipeline (personality,
[REACT:...], [DM_START]...) needs no awareness that vision happened at
all.

🌸 TEXT ATTACHMENTS (.txt / any UTF-8 file) — a SEPARATE, much cheaper
path from the image one above. No extra Groq call at all: a UTF-8
decode costs nothing, so attachment.read() bytes get decoded and the
FULL file content is folded straight into the prompt as its own
labeled block, the exact same way image_description/forwarded_text
already are in groq_service.py. "Bloating the token input" here would
only mean spending a whole extra completion call turning the file into
a paraphrased description first (like vision does) — skipping that
step entirely is what keeps this cheap, not truncating the file. The
only guard is MAX_TEXT_ATTACHMENT_CHARS, a generous per-file ceiling
so one freak huge log dump can't single-handedly blow the whole
prompt's context budget — real-world .txt/.py/.md/.json/etc. files
people actually share in Discord pass through completely untouched.
Non-text (binary) attachments are detected the honest way: the UTF-8
decode itself raises UnicodeDecodeError and that attachment is
silently skipped, no content_type/extension guessing needed.
"""

import asyncio
import base64
import discord

# 🌸 Model choice — see module docstring for the per-model image-count
# caps this is built around. qwen3.6-27b is the DEFAULT (more generous
# 5-image cap, and it's already in groq_instruct.MODEL_POOL for the main
# text pipeline, so reusing it here doesn't introduce a brand new
# unfamiliar model into the system). qwen3.8-27b is offered as the
# explicit "stronger reasoning, fewer images" alternative — see
# choose_vision_model below — for callers that want frontier-level
# analysis of 1-3 images specifically (e.g. detailed OCR/comparison)
# rather than just "what's in this picture".
VISION_MODEL_DEFAULT = "qwen/qwen3.6-27b"
VISION_MODEL_STRONGER = "qwen/qwen3.8-27b"

VISION_MODEL_MAX_IMAGES = {
    VISION_MODEL_DEFAULT: 5,
    VISION_MODEL_STRONGER: 3,
}

# 🌸 Groq's documented hard limit: requests containing an image URL
# input are capped at 20MB total. Guarding per-attachment against a
# fraction of that (rather than the full 20MB) leaves headroom for
# multiple images in the same request to combine safely without the
# WHOLE request getting rejected by Groq after we've already spent the
# time downloading+encoding everything.
MAX_SINGLE_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB per image

# 🌸 Absolute ceiling regardless of which model ends up chosen — even
# the more generous qwen3.6-27b caps at 5. Enforced again defensively
# inside build_image_content_blocks even if a caller passes more.
ABSOLUTE_MAX_IMAGES = 5

# 🌸 TEXT ATTACHMENTS — see module docstring. This is a pre-read size
# guard (checked against attachment.size, before ever downloading
# anything) so we don't waste a Discord CDN round-trip on something
# that's obviously not a small text file to begin with — e.g. a 40MB
# video someone attached alongside their message. Deliberately much
# more generous than MAX_SINGLE_IMAGE_BYTES since there's no Groq
# request-size cap in play here at all (no vision call happens for
# text), this is purely "don't bother downloading+decoding something
# this large".
MAX_TEXT_ATTACHMENT_DOWNLOAD_BYTES = 1 * 1024 * 1024  # 1MB

# 🌸 Post-decode ceiling on what actually gets folded into the prompt,
# per file. ~40k characters is roughly 10-12k tokens (rough 3-4
# chars/token ballpark for English/code) — generous enough that real
# .txt/.py/.md/.json/.log files people paste into Discord pass through
# completely whole, while still keeping one outlier file from eating
# the entire prompt budget by itself. Only the excess past this point
# gets truncated (with a note) — never a summary, always the file's
# own real text.
MAX_TEXT_ATTACHMENT_CHARS = 40_000

# 🌸 How many separate text-file attachments we'll bother reading per
# message — mirrors the spirit of ABSOLUTE_MAX_IMAGES. Most messages
# have at most one attached file; this just stops one message with
# five random file attachments from ballooning the prompt with five
# separate full-file blocks.
ABSOLUTE_MAX_TEXT_ATTACHMENTS = 3


def get_image_attachments(message: discord.Message) -> list[discord.Attachment]:
    """🌸 Filters a message's attachments down to just images Groq's
    vision models can actually take as input — anything without an
    `image/...` content_type (files, videos, audio, generic uploads) is
    silently excluded here rather than erroring, since a message can
    freely mix an image with an unrelated file attachment and only the
    image half is this module's concern.

    discord.py populates `content_type` from Discord's own attachment
    metadata (e.g. "image/png", "image/jpeg", "image/webp", "image/gif")
    — trusting it here rather than sniffing bytes ourselves, since
    that's already what Discord itself determined about the upload.
    """
    return [
        a for a in message.attachments
        if a.content_type and a.content_type.startswith("image/")
    ]


def choose_vision_model(image_count: int, prefer_stronger: bool = False) -> str:
    """🌸 Picks whichever vision model actually fits `image_count` under
    its documented per-request image cap (see VISION_MODEL_MAX_IMAGES).

    prefer_stronger=True tries qwen3.8-27b FIRST (its frontier-level
    reasoning per Groq's model card) but only when image_count still
    fits under ITS stricter 3-image cap — otherwise silently falls back
    to qwen3.6-27b's more generous 5-image cap so a caller asking for
    "the stronger model" on a 4-5 image message doesn't just get
    rejected by Groq, it gets the model that can actually serve the
    request.
    """
    if prefer_stronger and image_count <= VISION_MODEL_MAX_IMAGES[VISION_MODEL_STRONGER]:
        return VISION_MODEL_STRONGER
    if image_count <= VISION_MODEL_MAX_IMAGES[VISION_MODEL_DEFAULT]:
        return VISION_MODEL_DEFAULT
    # 🌸 More images than even the generous model allows — caller is
    # expected to have already truncated to ABSOLUTE_MAX_IMAGES before
    # calling this, but default to the more-permissive model as the
    # least-bad choice if not.
    return VISION_MODEL_DEFAULT


async def build_image_content_blocks(attachments: list[discord.Attachment]) -> list[dict]:
    """🌸 Downloads each image attachment via discord.Attachment.read()
    (no separate HTTP client needed — this is a method Discord.py
    already gives the Attachment object itself) and returns a list of
    Groq-vision-ready {"type": "image_url", ...} content blocks using
    base64 data URIs — the "locally saved image" pattern from Groq's
    vision docs — rather than passing attachment.url straight through,
    since Discord CDN URLs can be permission-gated or expire in ways
    that make them unreliable for Groq's servers to fetch independently.

    Enforces, per attachment, BOTH:
      - MAX_SINGLE_IMAGE_BYTES (this module's own conservative guard)
      - ABSOLUTE_MAX_IMAGES total (Groq's stricter model's hard cap)
    Oversized or excess attachments are just skipped (not raised as
    errors) — the caller gets back whatever fit, which is always
    better than the whole vision call failing over one big/extra image
    when a message has several attached.

    Returns an empty list if nothing usable was found — callers should
    treat that the same as "no images" and skip the vision call
    entirely rather than making a text-only call to a vision model.
    """
    blocks = []

    for attachment in attachments[:ABSOLUTE_MAX_IMAGES]:
        if attachment.size and attachment.size > MAX_SINGLE_IMAGE_BYTES:
            print(f"⚠️ Skipping image attachment {attachment.filename} — {attachment.size} bytes exceeds {MAX_SINGLE_IMAGE_BYTES} cap")
            continue

        try:
            image_bytes = await attachment.read()
        except discord.HTTPException as e:
            print(f"⚠️ Failed to read attachment {attachment.filename}: {e}")
            continue
        except discord.Forbidden as e:
            print(f"⚠️ Forbidden reading attachment {attachment.filename}: {e}")
            continue

        # 🌸 content_type from Discord is already validated by
        # get_image_attachments as "image/..." — reuse it directly as
        # the data URI's mime type rather than re-deriving from the
        # filename extension, since it's the more authoritative source
        # discord.py already gives us.
        mime_type = attachment.content_type or "image/png"
        b64_data = base64.b64encode(image_bytes).decode("utf-8")

        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
        })

    return blocks


async def describe_attachments(
    client,
    message: discord.Message,
    prefer_stronger: bool = False,
    user_text: str | None = None,
) -> str | None:
    """🌸 Main entry point — groq_service.py calls this with the live
    Groq client (self.bot.groq.client) and the incoming discord.Message.
    Returns a plain-text description of the message's image
    attachment(s), or None if there was nothing to describe / the call
    failed, so the caller can cleanly skip folding anything into the
    prompt.

    `user_text` is the user's own message content, if any — passed
    through as extra context alongside the image(s) (e.g. "what's
    wrong with this code screenshot?") rather than the vision model
    only ever getting a generic "describe this image" instruction.
    This is a SEPARATE call from the main get_ai_response conversation
    — see module docstring — so the personality/history/[REACT:...]/
    [DM_START] machinery there is entirely unaffected by this running
    or not.

    Fails soft (returns None, logs why) on: no client, no image
    attachments, zero usable blocks after size/type filtering, or any
    Groq API error — a missed image description is far less harmful
    than crashing the whole message-handling pipeline over a vision
    call that didn't need to be load-bearing.
    """
    if not client:
        return None

    image_attachments = get_image_attachments(message)
    if not image_attachments:
        return None

    blocks = await build_image_content_blocks(image_attachments)
    if not blocks:
        print(f"⚠️ describe_attachments: no usable image blocks after filtering for message {message.id}")
        return None

    model = choose_vision_model(len(blocks), prefer_stronger=prefer_stronger)

    prompt_text = (
        user_text.strip() if user_text and user_text.strip()
        else "Describe what's in this image in a couple of sentences."
    )

    content = [{"type": "text", "text": prompt_text}] + blocks

    def _call():
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            temperature=1,
            max_completion_tokens=768,
            top_p=1,
            stream=False,
        )

    try:
        # 🌸 asyncio.to_thread — same pattern as every other Groq call in
        # groq_ai.py. The groq SDK's client.chat.completions.create is a
        # BLOCKING/synchronous call; without offloading it to a thread it
        # would stall the entire bot's event loop for the duration of the
        # vision request (image calls are naturally slower than text-only
        # ones), freezing every other guild's message handling meanwhile.
        completion = await asyncio.to_thread(_call)
        description = (completion.choices[0].message.content or "").strip()
        return description or None
    except Exception as e:
        print(f"⚠️ Vision call failed (model={model}, images={len(blocks)}): {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
# 🌸 TEXT ATTACHMENTS (.txt / any UTF-8 file) — see module docstring
# for why this is a cheaper, separate path from the image vision code
# above: no Groq call at all, just a direct UTF-8 decode of whatever
# attachment.read() returns.
# ─────────────────────────────────────────────────────────────────────

def get_text_attachments(message: discord.Message) -> list[discord.Attachment]:
    """🌸 Filters a message's attachments down to ones worth attempting a
    UTF-8 text read on. Deliberately does NOT gate on content_type or
    file extension — Discord's content_type for code/config files
    (.py, .json, .yml, .env, extensionless files, etc.) is frequently
    missing or generic ("application/octet-stream"), so trusting it
    the way get_image_attachments trusts "image/..." would silently
    drop plenty of legitimately-readable text files. Instead this just
    excludes images (already handled by the vision path above, and
    reusing that same result here would be pointless) and anything
    over MAX_TEXT_ATTACHMENT_DOWNLOAD_BYTES — actual "is this really
    text" filtering happens honestly in read_text_attachments, via the
    UTF-8 decode itself succeeding or failing.
    """
    out = [
        a for a in message.attachments
        if not (a.content_type and a.content_type.startswith("image/"))
        and not (a.size and a.size > MAX_TEXT_ATTACHMENT_DOWNLOAD_BYTES)
    ]
    return out[:ABSOLUTE_MAX_TEXT_ATTACHMENTS]


async def read_text_attachments(attachments: list[discord.Attachment]) -> list[tuple[str, str]]:
    """🌸 Downloads each attachment via attachment.read() and attempts a
    strict UTF-8 decode. A binary file (image already filtered out
    upstream, but also zips, executables, audio, etc.) simply fails
    that decode with UnicodeDecodeError and gets silently skipped —
    same fail-soft philosophy as the vision path: a file the bot can't
    read as text is not a reason to error out the whole message.

    Per-file content is capped at MAX_TEXT_ATTACHMENT_CHARS (truncated
    with a trailing note, not summarized) so the caller always gets
    back the file's OWN real text, in full, up to that ceiling — never
    an AI paraphrase of it.

    Returns a list of (filename, text) pairs for whatever successfully
    decoded, in the same order as `attachments`. Empty list if nothing
    was readable as UTF-8 text.
    """
    results = []

    for attachment in attachments:
        try:
            raw = await attachment.read()
        except discord.HTTPException as e:
            print(f"⚠️ Failed to read attachment {attachment.filename}: {e}")
            continue
        except discord.Forbidden as e:
            print(f"⚠️ Forbidden reading attachment {attachment.filename}: {e}")
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # 🌸 Not UTF-8 text — a binary file discord.py's content_type
            # didn't flag as such (or didn't set at all). Silently skip,
            # same as an oversized/unreadable image attachment.
            continue

        if not text.strip():
            continue

        if len(text) > MAX_TEXT_ATTACHMENT_CHARS:
            remaining = len(text) - MAX_TEXT_ATTACHMENT_CHARS
            text = (
                text[:MAX_TEXT_ATTACHMENT_CHARS]
                + f"\n...[truncated — {remaining:,} more characters in the original file]"
            )

        results.append((attachment.filename, text))

    return results


def format_text_attachment_blocks(text_attachments: list[tuple[str, str]]) -> str:
    """🌸 Formats (filename, text) pairs into labeled prompt blocks — one
    per file, e.g.:

        [Attached file: notes.txt]
        <full file content>

    Same "own labeled block" shape as [Forwarded message]/[Attached
    image] already use in groq_service.py's prompt assembly, so Groq
    sees a consistent structure regardless of which kind of extra
    context got folded in. Multiple files are joined with a blank line
    between them. Returns "" if given an empty list.
    """
    return "\n\n".join(
        f"[Attached file: {filename}]\n{text}"
        for filename, text in text_attachments
    )


async def read_and_format_text_attachments(message: discord.Message) -> str | None:
    """🌸 Main entry point for text attachments — the read_text_attachments
    equivalent of describe_attachments above, minus the Groq call.
    groq_service.py calls this with the incoming discord.Message and
    gets back a single ready-to-append prompt block (or None if there
    was nothing readable), the same shape as image_description already
    has, so it slots into the existing prompt-assembly `if` chain with
    zero special-casing.
    """
    attachments = get_text_attachments(message)
    if not attachments:
        return None

    text_attachments = await read_text_attachments(attachments)
    if not text_attachments:
        return None

    return format_text_attachment_blocks(text_attachments)
