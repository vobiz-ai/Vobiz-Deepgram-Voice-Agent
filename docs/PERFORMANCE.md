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

Deepgram also emits a report per *recognition segment*, tens of times per turn, which is useful
detail when you are debugging recognition itself. Only the turn-level report carries
`total_latency`, so that is the one `app.py` logs — the segment-level reports are far too frequent
to belong in a call log.

Measured on this stack, the split is uneven in a useful way:

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
| `EOT_TIMEOUT_MS` | `3000` | Hard ceiling — end the turn this long after speech regardless of confidence. Deepgram's default of 5000 gives a speaker room to think, which suits dictation; a phone conversation has a quicker rhythm, so this agent tightens it |
| `EAGER_EOT_THRESHOLD` | `0.4` | Start generating the reply on a medium-confidence turn end, before the turn is confirmed. If the caller was mid-sentence the speculative work is discarded |

Eager end-of-turn is the cheapest latency win available: generation overlaps with the tail of the
caller's speech instead of starting after it. The cost is extra language-model calls for the turns
that get retracted. `0` disables it.

On a noisy or low-bitrate line, raise `EOT_THRESHOLD` before reaching for anything else —
background speech being read as the caller taking a turn is the usual cause of an agent that
interrupts itself.

## Why every locale replies in English

Recognition covers ten Indian languages — Flux STT Multilingual understands Hindi, and Nova-3 adds
Tamil, Telugu, Marathi, Bengali, Gujarati, Punjabi, Kannada, Assamese and Urdu — and the voices
built for this audience are Indian-accented English. Pairing the two is what this agent does:
understand whatever the caller speaks, answer in Indian-accented English.

It is worth spelling out why, because the alternative is tempting. An earlier version of this agent
replied in romanised Hindi ("aapko kya chahiye") rendered by `flux-naveen-en`, and the
`LatencyReport` showed the cost: an English voice model asked to synthesise romanised Hindi has to
guess grapheme-to-phoneme mappings for letter sequences English never produces, which is slow and
audibly rough. Replying in English put `tts_latency` at 43–103 ms — so English output is the faster
and better-sounding choice, not a fallback.

The Indian character comes from the **voice**, which is where a caller hears it anyway:

| Voice | |
|---|---|
| `flux-meena-en` | female — customer service, casual chat (default) |
| `flux-priya-en` | female — IVR, confident and reassuring |
| `flux-naveen-en` | male — IVR, support, informative |

If a deployment genuinely needs Hindi speech output, Deepgram's multilingual guidance covers it:
point the `speak` block at a third-party provider such as ElevenLabs or Cartesia. That is a
supported configuration — worth knowing it adds a provider key and moves synthesis outside
Deepgram's managed path, so the single-socket latency profile above no longer applies.

## Recognition accuracy

`keyterms` on the listen provider biases recognition toward vocabulary specific to your deployment,
and it is the highest-leverage accuracy setting available. `INDIA_KEYTERMS` in `app.py` carries
Indian financial and civic vocabulary — Aadhaar, UPI, PAN card, GST, IFSC, RuPay, lakh, crore, KYC,
OTP — plus major city names.

**Put your brand names in first.** Invented product and company names are out-of-vocabulary for any
recogniser by definition — there is no pronunciation for them to have learned — and they are also
the words a voice agent says most often, so a single keyterm entry pays for itself on every call.
Adding `Vobiz` and `Deepgram` to the list measurably cleaned up our own transcripts. Add your
product, company and domain terms before tuning anything else.

Locale choice also affects recognition. `en-in` uses the monolingual `flux-general-en`, which has
tighter end-of-turn behaviour than `flux-general-multi`; `hi-in` trades a little of that for
Hindi–English code-switching, which is what real Indian callers do. Pick `hi-in` only if your
callers actually mix languages.

## Audio framing

Deepgram streams output audio in chunks sized for throughput — usually exact multiples of a frame,
though the first and last of a turn naturally are not. `VobizStream.play()` carries the remainder across
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

A barge-in *clears* this list rather than adding to it: Vobiz voids any checkpoint whose audio was
still queued when the flush happened, so those turns can never be confirmed and tracking them would
only produce noise. What the list does catch is a turn that was fully sent, never interrupted, and
still never acknowledged — which points at audio dropped by the media server.

## Region

See [India vs global](../README.md#india-vs-global) in the README. Short version: for calls
terminating in India, `DEEPGRAM_REGION=india` cut time-to-first-audio from roughly 1.5 s to
roughly 0.4 s in testing, with identical models and pricing.
