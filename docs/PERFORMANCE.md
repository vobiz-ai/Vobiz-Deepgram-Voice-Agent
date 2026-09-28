# Performance and tuning

Notes behind the defaults in `app.py`, and what to change when a call does not feel right.

All figures here were measured on live PSTN calls and live Deepgram connections from Bengaluru,
on mu-law 8 kHz. They are indicative, not a benchmark suite — re-measure from your own region
before treating any of them as a target.

## Where the time actually goes

Deepgram emits a `LatencyReport` per turn, broken down by stage. `app.py` logs it:

```
[latency] stt=180ms  llm_first_token=826ms  tts=44ms  total=1070ms
```

| Field | Meaning |
|---|---|
| `stt_latency` | audio received to transcript produced |
| `ttt_text_latency` | time to the first text token from the language model |
| `tts_latency` | first text token to first audio byte |
| `total_latency` | caller stopped speaking to first audio byte |

Deepgram also emits a report per *recognition segment*, tens of times per turn. Only the
turn-level report carries `total_latency`, and only that one is logged — filtering on anything
else floods the log.

Measured on this stack, the split is lopsided:

| Stage | Typical |
|---|---|
| Recognition | a few hundred ms |
| **Language model** | **the large majority of the total** |
| Synthesis | under 100 ms |

That ordering is the useful finding. Synthesis is effectively free, recognition is small and
mostly fixed, and the language model is where nearly all the latency lives — so tuning
end-of-turn thresholds cannot substitute for choosing the right `think` model.

## Choosing the language model

All of these are Deepgram-managed, so none needs a provider API key. Median time from a user
message to the first byte of audio, six turns each, against the India endpoint:

| Provider | Model | Median | Range |
|---|---|---|---|
| `anthropic` | **`claude-haiku-4-5`** (default) | **945 ms** | 822 – 962 |
| `open_ai` | `gpt-4.1-mini` | 1347 ms | 1202 – 1790 |
| `google` | `gemini-3.1-flash-lite` | 1387 ms | 1196 – 1465 |
| `open_ai` | `gpt-4o-mini` | 1444 ms | 1115 – 1755 |
| `google` | `gemini-2.5-flash` | 1807 ms | |
| `google` | `gemini-3.5-flash` | 2700 ms | |
| `open_ai` | `gpt-5-mini` | 4792 ms | |

Haiku was picked for the median *and* for the spread: its slowest turn was faster than any other
model's median. On a phone call consistency matters more than the average, because a caller
adapts to a steady rhythm and notices one that swings by a second.

Two caveats. The LLM step is the one part of the pipeline that does not run in India — it executes
wherever the provider runs — so this ranking is worth re-checking from your own region. And model
line-ups change; re-measure rather than trusting this table indefinitely.

Set `LLM_PROVIDER` and `LLM_MODEL` to switch. Nothing else in the pipeline changes.

## The system prompt is a latency cost

The prompt is re-sent as context on every turn, so each extra sentence adds to time-to-first-token
on every reply. The prompts in `app.py` are deliberately terse for that reason — trimming a long
one to roughly a quarter of its length was a visible improvement on its own.

Keep additions short, and prefer specific instructions over explanatory prose.

## Turn-taking

Three settings on the Flux listen provider, all ignored by the `indic` locale because Nova does
its own endpointing.

| Variable | Default | Effect |
|---|---|---|
| `EOT_THRESHOLD` | `0.7` | Confidence needed to declare the turn over, `0.5`–`1.0`. Lower ends turns sooner and risks cutting the caller off; higher waits longer and tolerates noise better |
| `EOT_TIMEOUT_MS` | `3000` | Hard ceiling — end the turn this long after speech regardless of confidence. Deepgram's default is 5000, which is a long silence on a phone call |
| `EAGER_EOT_THRESHOLD` | `0.4` | Start generating the reply on a medium-confidence turn end, before the turn is confirmed. If the caller was mid-sentence the speculative work is discarded |

Eager end-of-turn is the cheapest latency win available: generation overlaps with the tail of the
caller's speech instead of starting after it. The cost is extra language-model calls for the turns
that get retracted. `0` disables it.

On a noisy or low-bitrate line, raise `EOT_THRESHOLD` before reaching for anything else —
background speech being read as the caller taking a turn is the usual cause of an agent that
interrupts itself.

## Why every locale replies in English

Deepgram listens in nine Indian languages and speaks none of them. Flux Multilingual understands
Hindi; Nova-3 adds Tamil, Telugu, Marathi, Bengali, Gujarati, Punjabi, Kannada, Assamese and Urdu.
There is no Indic-language TTS voice — every Flux voice is an English model, and Aura's
code-switching covers English and Spanish only.

An earlier version of this agent replied in romanised Hindi ("aapko kya chahiye") rendered by
`flux-naveen-en`. That sounded wrong on a call, and the `LatencyReport` showed why: the voice was
being asked to guess grapheme-to-phoneme mappings for letter sequences English never produces.
Synthesis was slow and the prosody audibly broken. With the same agent replying in English,
`tts_latency` settled at 43–103 ms.

So the Indian character comes from the **voice**, not the words:

| Voice | |
|---|---|
| `flux-meena-en` | female — customer service, casual chat (default) |
| `flux-priya-en` | female — IVR, confident and reassuring |
| `flux-naveen-en` | male — IVR, support, informative |

If you genuinely need Hindi speech output, it has to come from a third-party TTS provider in the
`speak` block — Deepgram's own multilingual guidance points at ElevenLabs or Cartesia for
languages it cannot voice. That adds a provider key and takes the synthesis step outside
Deepgram's managed path.

## Recognition accuracy

`keyterms` on the listen provider biases recognition toward words the model would otherwise miss.
`INDIA_KEYTERMS` in `app.py` carries Indian financial and civic vocabulary — Aadhaar, UPI, PAN
card, GST, IFSC, RuPay, lakh, crore, KYC, OTP — plus major city names.

Brand names are the highest-value entries and the easiest to overlook. Before `Vobiz` and
`Deepgram` were added, a live call transcribed the product name as *"boobies"* and *"Vobel"* —
the two words the agent says most often were the two it understood worst. Add your own product,
company and domain terms first.

Locale choice also affects recognition. `en-in` uses the monolingual `flux-general-en`, which has
tighter end-of-turn behaviour than `flux-general-multi`; `hi-in` trades a little of that for
Hindi–English code-switching, which is what real Indian callers do. Pick `hi-in` only if your
callers actually mix languages.

## Audio framing

Deepgram delivers output audio in chunks of its own choosing — usually exact multiples of a frame,
but the first and last of a turn rarely are. `VobizStream.play()` carries the remainder across
chunks so every `playAudio` frame is exactly 20 ms, and flushes the tail on `AgentAudioDone`.

Vobiz recommends 20–60 ms chunks. Smaller chunks let `clearAudio` cancel more of the queued
speech on a barge-in, so 20 ms favours interruption responsiveness.

## Barge-in ordering

Vobiz voids any checkpoint whose audio was still queued when a flush happened, and sending
`playAudio` into an in-flight flush lets the new audio race it and be partially dropped — which
the caller hears as the next reply starting mid-word.

`app.py` therefore holds audio that arrives between `clearAudio` and `clearedAudio`, and releases
it on the acknowledgement. `CLEAR_ACK_TIMEOUT_S` (default 1 s) releases it anyway if the
acknowledgement never comes, because a clipped syllable is a far better outcome than an agent that
stays silent for the rest of the call.

Checkpoints are tracked rather than merely sent, so any turn the caller never confirmed hearing is
reported when the stream ends:

```
[call] turns never confirmed played: ['turn-1', 'turn-2']
```

On a call with barge-ins that output is expected — interrupted turns genuinely never reached the
caller.

## Region

See [India vs global](../README.md#india-vs-global) in the README. Short version: for calls
terminating in India, `DEEPGRAM_REGION=india` cut time-to-first-audio from roughly 1.5 s to
roughly 0.4 s in testing, with identical models and pricing.
