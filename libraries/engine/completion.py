"""🌸 Completion module — extracted from groq_ai.py's monolithic
get_ai_response. Structure-only split: same per-model-family kwargs,
same 413 three-stage fallback chain (slim retry → gpt-oss-120b +
browser_search → plain non-compound/non-120b model with the "live web
search wasn't available" note), same max_attempts formula, same
RateLimitError-only key rotation (the old `"rate" in str(e)` heuristic
is NOT revived), same success/transcript/memory-save side effects.

run_completion_loop(...) is the full loop moved verbatim out of
get_ai_response — it owns building messages/kwargs per attempt,
calling the SDK, handling GroqRateLimitError / GroqAPIStatusError /
generic Exception, running check_output_safety on the reply, logging
the transcript, saving memory, and firing the success embed. It
returns the final reply string, or None on total failure — exactly
the same contract get_ai_response had.
"""
import random
import asyncio

from groq import RateLimitError as GroqRateLimitError
from groq import APIStatusError as GroqAPIStatusError

from groq_instruct import MODEL_POOL, _strip_reasoning, _search_domains_for_prompt
from resources import shared


async def run_completion_loop(
    service,
    *,
    prompt: str,
    personality: str,
    recent_history: list,
    model_to_use: str,
    is_compound_call: bool,
    username: str,
    user_id: int,
    guild,
    channel,
    display_name,
    history: list,
    message_id,
    guild_id,
    dm_channel_id,
):
    """
    Runs the (blocking) Groq SDK call in a worker thread so it never
    stalls the bot's event loop — see the async def _call closure
    below, which awaits self.async_client directly (AsyncGroq, not a
    to_thread-wrapped sync client).

    Returns the final reply string on success, or None if every
    attempt in max_attempts is exhausted / a non-retryable error
    occurs.
    """
    async def _call(slim: bool = False):
        # 1. Buat parameter dasar yang selalu digunakan semua model
        # 🌸 slim=True drops recent_history entirely — used for the
        # 413 ("Request Entity Too Large") retry below. Compound
        # calls can tip over a thin TPM budget even with a small
        # history window (search tool round-trip adds its own
        # tokens on top), so on a genuine 413 the safest recovery is
        # just the system prompt + the user's actual message, no
        # conversational context at all.
        messages = [{"role": "system", "content": personality}]
        if not slim:
            messages.extend(recent_history)
        messages.append({"role": "user", "content": prompt})

        # 🌸 Needed one line earlier than before (used for max_tokens
        # below too now) — same value, just computed before kwargs
        # instead of after.
        model_lower = model_to_use.lower()

        # 🌸 COMPOUND TOKEN BUDGET (part 3): even a FULLY slim request
        # (no history, no server context — see used_slim_retry above)
        # can still 413 on compound/compound-mini, because the
        # search-tool round-trip's cost is on TOP of whatever we ask
        # it to output, and Groq's free/on-demand TPM ceiling counts
        # both. This is a known issue on Groq's side — their own
        # community forum has reports of compound 413ing on bare
        # one-line prompts with no system prompt at all — so trimming
        # OUR side further has diminishing returns. What we CAN do is
        # cap the output budget we ask for, since every token we
        # request is TPM we're not leaving for the search round-trip.
        max_tokens_ceiling = 300 if "compound" in model_lower else 1000

        kwargs = {
            "model": model_to_use,
            "messages": messages,
            # 🌸 Randomized per-call (0.10-0.99, 2 decimal places) so
            # replies aren't stuck at one fixed creativity level every
            # turn — same spirit as max_tokens below already varying
            # per call. round() to 2dp keeps the value clean in logs.
            "temperature": round(random.uniform(0.10, 0.99), 2),
            "max_tokens": random.randint(100, max_tokens_ceiling),
        }

        # 2. Atur parameter reasoning secara dinamis sesuai tipe model

        if "compound" in model_lower:
            # 🌸 groq/compound & groq/compound-mini are agentic SYSTEMS,
            # not plain chat models — they reject reasoning_format AND
            # reasoning_effort with a 400. Web search fires automatically
            # server-side (Tavily) whenever the system decides the query
            # needs current info; nothing extra to pass for that. Same
            # temperature/max_tokens kwargs above still apply fine.
            #
            # 🌸 search_settings.include_domains — restricts Tavily to a
            # small trusted allowlist picked from the PROMPT's topic
            # (sports/weather/news/reference — see
            # SEARCH_TOPIC_DOMAINS in groq_instruct.py). Smaller,
            # more relevant search results cost fewer tokens, which
            # is what was tipping compound-mini into 413 ("Request
            # Entity Too Large") on questions like the World Cup one
            # that prompted this. Falls back to None (Groq's normal
            # unrestricted search) when the prompt doesn't match any
            # known topic — an open-ended search question still gets
            # full search rather than a wrong/empty allowlist.
            domains = _search_domains_for_prompt(prompt)
            if domains:
                kwargs["search_settings"] = {"include_domains": domains}
        elif browser_search_active and "gpt-oss-120b" in model_lower:
            # 🌸 STAGE 2 SEARCH FALLBACK — Groq's browser_search tool is
            # a SEPARATE search mechanism from compound's Tavily
            # integration (different infra, different token-budget
            # profile), so when compound keeps 413ing this gives live
            # search a genuinely different path to succeed on instead
            # of just giving up on real-time info. tool_choice="auto"
            # lets the model decide whether the prompt actually needs
            # a search instead of forcing one on every call to this
            # fallback — "required" was firing browser_search even on
            # prompts that didn't need it, burning tokens/latency for
            # nothing. gpt-oss-120b with tool use wants
            # max_completion_tokens instead of max_tokens, and top_p=1
            # — matches Groq's documented working example for this tool.
            kwargs.pop("max_tokens", None)
            kwargs["max_completion_tokens"] = random.randint(200, 500)
            kwargs["top_p"] = 1
            # 🌸 Lower/narrower temperature than the usual randomized
            # 0.10-0.99 range — this call is meant to ground a factual
            # summary in real search results, not go for creative
            # variety, so keep it closer to deterministic.
            kwargs["temperature"] = round(random.uniform(0.10, 0.40), 2)
            kwargs["tools"] = [{"type": "browser_search"}]
            kwargs["tool_choice"] = "auto"
            kwargs["reasoning_format"] = "hidden"
            kwargs["reasoning_effort"] = "low"
        elif "qwen" in model_lower:
            # Qwen menolak reasoning_format, gunakan effort: none untuk mematikan thinking
            kwargs["reasoning_effort"] = "none"
        elif "llama" in model_lower:
            # Llama standar tidak mendukung parameter reasoning sama sekali.
            # Kita biarkan tanpa reasoning_format atau reasoning_effort agar tidak error 400.
            pass
        else:
            # GPT-OSS / DeepSeek menerima reasoning_format
            kwargs["reasoning_format"] = "hidden"
            # Paksa gpt-oss berpikir seminimal mungkin agar cepat
            if "gpt-oss" in model_lower:
                kwargs["reasoning_effort"] = "low"

        # 3. Jalankan request dengan mendekompresi (unpack) kwargs
        # 🌸 self.client is AsyncGroq — awaited directly here (no
        # asyncio.to_thread needed, that was only correct for a sync
        # Groq client; awaiting a coroutine from inside a worker
        # thread doesn't work, there's no event loop running there).
        return await service.async_client.chat.completions.with_raw_response.create(**kwargs)

    used_slim_retry = False
    # 🌸 STAGE 2 flags — a 413 that survives the slim retry falls
    # through to gpt-oss-120b + Groq's browser_search tool (a
    # DIFFERENT search mechanism from compound's Tavily integration —
    # see _call above). used_browser_search_fallback is the monotonic
    # "already tried stage 2" marker (prevents retrying it twice);
    # browser_search_active is only True WHILE that attempt is live,
    # so _call knows to attach the tool — it gets flipped back off if
    # we fall through to stage 3.
    used_browser_search_fallback = False
    browser_search_active = False
    # 🌸 STAGE 3 — browser_search fallback 413'd too, so live search
    # genuinely isn't happening this turn on either mechanism. Give up
    # and answer from the model's own knowledge instead of returning
    # None. See the GroqAPIStatusError handler below.
    used_final_fallback = False
    # 🌸 +3 (not +1) so the 413 slim-retry, the browser_search
    # fallback, AND the final no-search fallback ALL get their own
    # iteration even with a single API key — without the extra slots,
    # a lone key runs the loop out of iterations partway through a
    # `continue` chain and a later-stage retry never actually happens.
    max_attempts = max(len(service.api_keys), 1) + 3
    for attempt in range(max_attempts):
        try:
            raw_response = await _call(used_slim_retry)
            service._log_rate_limits(raw_response.headers, username, model_to_use)

            completion = await raw_response.parse()
            reply = _strip_reasoning(completion.choices[0].message.content)

            # 🌸 OUTPUT-SIDE safety gate — catches cases where an
            # innocent-looking prompt (cipher/decode tricks, etc.)
            # slipped past check_safety's prompt-only screening and got
            # the model to actually generate disallowed content. If
            # flagged, swap in a refusal BEFORE it's saved to memory,
            # written to the transcript log, or sent to Discord.
            if not await service.check_output_safety(reply, username):
                reply = await service._generate_safeguard_decline(username, display_name, guild, flagged_text=prompt)
                service._log_ai_transcript(
                    username, user_id, model_to_use, guild, channel,
                    prompt, "[BLOCKED BY OUTPUT SAFEGUARD — reply withheld]",
                )
            else:
                # 🌸 Full prompt+response transcript → log/groq_ai.log
                service._log_ai_transcript(username, user_id, model_to_use, guild, channel, prompt, reply)

            # 🌸 message_id on the user turn = this incoming message's
            # own snowflake. The assistant turn doesn't have its sent
            # message's snowflake yet at this point (message.reply()
            # hasn't happened — that's back in bot_service.py), so it's
            # tagged with the SAME message_id as the user turn it's
            # replying to. That's enough for the anchor search above:
            # replying to a bot message later just needs to match
            # message_id somewhere on an "assistant" row, and since the
            # user+assistant pair share one id, a reply targeting
            # either the user's message or the bot's reply resolves to
            # the same anchor point.
            history.append({"role": "user", "content": prompt, "message_id": message_id})
            history.append({"role": "assistant", "content": reply, "message_id": message_id})
            await shared.save_groq_memory(model_to_use, username, user_id, history[-200:], guild_id, dm_channel_id)

            # 🌸 Success log — fired for EVERY completed Groq reply, not
            # just errors. Best-effort: never let a logging hiccup take
            # down the actual reply.
            if service.bot and channel is not None:
                try:
                    success_embed = service._build_success_embed(
                        username, user_id, guild, channel, model_to_use,
                        headers=raw_response.headers,
                    )
                    asyncio.create_task(service.bot._broadcast_log_embed(success_embed))
                except Exception as log_err:
                    print(f"⚠️ Success log embed error: {log_err}")

            return reply
        except GroqRateLimitError as e:
            # 🌸 The SDK's own dedicated 429 exception — this is the
            # real, unambiguous signal that a key is actually rate
            # limited (see groq-python docs: groq.RateLimitError is
            # raised specifically for 429s, distinct from any other
            # APIStatusError). No guessing from error text needed.
            err_headers = getattr(getattr(e, "response", None), "headers", None)
            if err_headers:
                service._log_rate_limits(err_headers, username, model_to_use)

            print(f"⚠️ Groq RateLimitError (status_code={getattr(e, 'status_code', '?')}): {e}")
            print(f"   Rotating from Key #{service.current_key_index + 1}...")

            # 🌸 Rich 429 embed → every configured log channel. Fired
            # BEFORE rotate_key() so "Key #" in the embed reflects the
            # key that actually got limited, not the new one.
            if service.bot:
                try:
                    rl_embed = service._build_rate_limit_embed(e, username, user_id, model_to_use)
                    asyncio.create_task(service.bot._broadcast_log_embed(rl_embed))
                except Exception as log_err:
                    print(f"⚠️ Rate-limit embed error: {log_err}")

            service.rotate_key()
            continue
        except GroqAPIStatusError as e:
            # 🌸 413 "Request Entity Too Large" — most common on
            # compound/compound-mini, whose search-tool round-trip
            # (query + Tavily snippets fed back in) adds tokens on
            # top of whatever history we sent, tipping a thin TPM
            # budget over the edge. ONE retry with recent_history
            # dropped entirely (slim=True — just system prompt + the
            # user's actual message) before falling back further. Any
            # other non-429 status code (400, 500, etc.) falls
            # through to the generic handler below unchanged — this
            # branch only ever retries on a genuine 413.
            if getattr(e, "status_code", None) == 413 and not used_slim_retry:
                err_headers = getattr(getattr(e, "response", None), "headers", None)
                if err_headers:
                    service._log_rate_limits(err_headers, username, model_to_use)
                print(f"⚠️ Groq 413 on {model_to_use} — retrying once with history stripped...")
                used_slim_retry = True
                continue

            # 🌸 STAGE 2 — a 413 that survives the slim retry means
            # trimming OUR side of the request didn't help, which
            # tracks: Groq's own community forum has reports of
            # compound/compound-mini 413ing on bare one-line prompts
            # with NO system prompt at all, because the search-tool
            # round-trip's token cost is added server-side and isn't
            # something we control from the request we send. Instead
            # of giving up on live search immediately, try Groq's
            # OTHER built-in search tool — browser_search on
            # gpt-oss-120b — since it's separate infra from compound's
            # Tavily integration and may not be hitting the same
            # budget wall.
            if (
                getattr(e, "status_code", None) == 413
                and used_slim_retry
                and is_compound_call
                and not used_browser_search_fallback
            ):
                err_headers = getattr(getattr(e, "response", None), "headers", None)
                if err_headers:
                    service._log_rate_limits(err_headers, username, model_to_use)
                print(f"⚠️ Groq 413 persisted on {model_to_use} — trying gpt-oss-120b + browser_search instead...")
                used_browser_search_fallback = True
                browser_search_active = True
                model_to_use = "openai/gpt-oss-120b"
                is_compound_call = False
                continue

            # 🌸 STAGE 3 — the browser_search fallback ALSO 413'd, so
            # live search genuinely isn't available on either
            # mechanism this turn. Drop search entirely and retry ONCE
            # more on a plain model from the pool — the user still
            # gets an actual reply, just without real-time info, and
            # we say so up front so the model doesn't confidently make
            # something up in its place instead of just returning
            # None (dead silence + a scary error dump to the log
            # channel, which is what was happening before).
            if (
                getattr(e, "status_code", None) == 413
                and used_browser_search_fallback
                and not used_final_fallback
            ):
                err_headers = getattr(getattr(e, "response", None), "headers", None)
                if err_headers:
                    service._log_rate_limits(err_headers, username, model_to_use)
                print(f"⚠️ Groq 413 persisted on browser_search too — dropping search entirely, retrying plain...")
                used_final_fallback = True
                browser_search_active = False
                fallback_pool = [
                    m for m in MODEL_POOL
                    if "compound" not in m.lower() and "gpt-oss-120b" not in m.lower()
                ]
                model_to_use = random.choice(fallback_pool) if fallback_pool else service.default_model
                prompt = (
                    f"{prompt}\n\n(Note: live web search wasn't available just now — "
                    "answer from what you already know, and briefly mention you couldn't "
                    "pull live results instead of guessing at current facts.)"
                )
                continue

            err_headers = getattr(getattr(e, "response", None), "headers", None)
            if err_headers:
                service._log_rate_limits(err_headers, username, model_to_use)
            print(f"⚠️ Groq APIStatusError (status_code={getattr(e, 'status_code', '?')}): {e}")
            return None
        except Exception as e:
            # 🌸 BUGFIX: this used to check `"rate" in str(e).lower()`,
            # which false-positives on all kinds of unrelated errors —
            # "generate", "moderate", "separate", "duplicate",
            # "collaborate", etc. all contain "rate" as a substring, so
            # ANY unrelated exception got misclassified as a 429 and
            # burned through every key rotating for no reason (with the
            # logged x-ratelimit-remaining-tokens often still showing
            # the FULL limit, proving it wasn't a real rate limit at
            # all). Only groq.RateLimitError above should ever trigger
            # a rotation now — everything else (parse errors, network
            # issues, etc.) is logged and returns None so the caller
            # falls back to the random/context roulette instead.
            err_headers = getattr(getattr(e, "response", None), "headers", None)
            if err_headers:
                service._log_rate_limits(err_headers, username, model_to_use)

            print(f"⚠️ Groq error ({type(e).__name__}): {e}")
            return None

    return None
