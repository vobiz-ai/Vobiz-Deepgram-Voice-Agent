"""
Vobiz to Deepgram Voice Agent bridge.

Vobiz owns the phone call. Deepgram owns the conversation. This service is the seam
between the two and deliberately keeps no conversational state of its own:

    PSTN -- Vobiz --+-- media frames ---> this bridge ---> Deepgram Voice Agent
                    +<-- playAudio ------             <--- (Flux STT | LLM | Flux TTS)

Transcription, the language model, speech synthesis and turn-taking all happen inside
one Deepgram WebSocket. What is left for us is protocol translation plus four facts
about Vobiz's <Stream> element that are easy to get wrong and expensive to discover
on a live call:

  1. keepCallAlive="true" is mandatory. Without it the document ends the instant the
     element is parsed and the call drops with "End Of XML Instructions".
  2. audioTrack must be "inbound". The media server rejects "both" outright whenever
     bidirectional="true".
  3. extraHeaders cannot authenticate the media socket. Those values never reach the
     WebSocket -- not as upgrade headers, and not in the start frame, whose
     extra_headers field stays the literal "{}". They surface only in the
     statusCallbackUrl payload, prefixed X-VH-. Stream credentials therefore have to
     ride somewhere the socket can actually see them, which leaves the URL path.
  4. The callback signature is computed over the URL and a nonce, never over the
     request body. Voice webhooks are form-encoded, so any scheme that hashes a
     JSON body will never match.

Routes

    GET|POST  /answer            the number's answer URL -- returns the <Stream> document
    WS        /media/{secret}    bidirectional audio, one socket for the life of the call
    POST      /stream-status     <Stream statusCallbackUrl> lifecycle events
    POST      /hangup            call-ended webhook
    GET       /health            resolved configuration, for a quick sanity check
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qsl

from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from deepgram.environment import DeepgramClientEnvironment
from deepgram.agent.v1 import (
    AgentV1AgentAudioDone,
    AgentV1ConversationText,
    AgentV1Error,
    AgentV1LatencyReport,
    AgentV1Settings,
    AgentV1SettingsApplied,
    AgentV1UserStartedSpeaking,
    AgentV1Warning,
    AgentV1Welcome,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

load_dotenv()


def _required(name: str) -> str:
    """Read a mandatory setting, failing with a sentence instead of a KeyError."""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"{name} is required -- set it in .env (see .env.example).")
    return value


def _optional(name: str, default: str) -> str:
    """Read an override, treating a blank value as absent.

    os.getenv returns "" rather than None for a variable that is present but empty,
    which is exactly how an override appears in a .env copied from the template
    (`DG_TTS_MODEL=`). Falling through to the default is what the reader intends.
    """
    return (os.getenv(name) or "").strip() or default


# --- Where Vobiz can reach us -------------------------------------------------
# Vobiz dials out to this host, so it has to be the public name, not the port we
# bind. We accept a pasted "https://host/" and normalise it: the scheme and any
# trailing slash would otherwise end up spliced into a wss:// URL, and the failure
# surfaces as a stream that never opens rather than as anything resembling a
# configuration error.
PUBLIC_HOSTNAME = (
    _required("PUBLIC_HOSTNAME")
    .removeprefix("https://")
    .removeprefix("http://")
    .rstrip("/")
)
if "/" in PUBLIC_HOSTNAME or PUBLIC_HOSTNAME.startswith("your-host"):
    raise SystemExit(
        f"PUBLIC_HOSTNAME must be a bare hostname, got {PUBLIC_HOSTNAME!r}.\n"
        "Example: PUBLIC_HOSTNAME=abc123.ngrok-free.app"
    )

DEEPGRAM_API_KEY = _required("DEEPGRAM_API_KEY")
HTTP_PORT = int(_optional("HTTP_PORT", "5050"))

# --- Guarding the two public endpoints ----------------------------------------
# Both the answer URL and the media socket are reachable by anyone who learns the
# hostname, and an accepted media socket opens a billed Deepgram session. Two
# independent controls, each off until configured:
#
#   VERIFY_SIGNATURE   checks the X-Vobiz-Signature-V3/V2 HMAC on every webhook,
#                      keyed by VOBIZ_AUTH_TOKEN. Opt-in rather than automatic
#                      because Vobiz only emits those headers once the callback URL
#                      has auth credentials set on it in the console -- enabling
#                      this first would reject every genuine request.
#   STREAM_SECRET      a random path segment on the wss:// URL, compared at the
#                      handshake. Rejecting before accept() matters: it means an
#                      unauthorised socket never reaches the Deepgram connect below.
VOBIZ_AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN", "")
VERIFY_SIGNATURE = os.getenv("VERIFY_SIGNATURE", "").strip().lower() in ("1", "true", "yes")
STREAM_SECRET = os.getenv("STREAM_SECRET", "").strip()

if VERIFY_SIGNATURE and not VOBIZ_AUTH_TOKEN:
    raise SystemExit("VERIFY_SIGNATURE is on but VOBIZ_AUTH_TOKEN is empty -- it is the signing key.")

# The secret becomes a URL path segment and is compared with hmac.compare_digest,
# which refuses non-ASCII input. str.isalnum() alone would wave through "cafe1"
# with an accent and turn every subsequent call into a 500, so test both.
if STREAM_SECRET and not (STREAM_SECRET.isascii() and STREAM_SECRET.isalnum()):
    raise SystemExit(
        "STREAM_SECRET must be ASCII alphanumeric -- it becomes a URL path segment.\n"
        'Generate one with: python -c "import secrets; print(secrets.token_hex(16))"'
    )
if not VERIFY_SIGNATURE:
    print("[security] VERIFY_SIGNATURE is off -- webhooks are NOT verified.")
if not STREAM_SECRET:
    print("[security] STREAM_SECRET is unset -- /media connections are NOT authenticated.")


# --- Audio ---------------------------------------------------------------------
# The two directions are negotiated separately and neither is resampled here, so a
# profile has to be internally consistent or the caller hears nonsense.
#
#   inbound   <Stream contentType>, one of audio/x-mulaw;rate=8000,
#             audio/x-l16;rate=8000, audio/x-l16;rate=16000. That attribute
#             configures the inbound leg only and tops out at 16 kHz -- asking it
#             for 24 kHz is the most common way to break this.
#   outbound  the contentType and sampleRate carried by each playAudio frame.
#             playAudio accepts L16 at 8/16/24 kHz and mu-law at 8 kHz, so 24 kHz
#             is available outbound even though it is not available inbound.
@dataclass(frozen=True)
class AudioProfile:
    """One self-consistent pairing of the Vobiz wire format and the agent's audio."""

    stream_content_type: str
    agent_input: dict
    agent_output: dict
    play_content_type: str
    play_sample_rate: int
    bytes_per_sample: int

    @property
    def frame_bytes(self) -> int:
        """Bytes in one 20 ms outbound frame -- the cadence a PSTN leg expects."""
        return self.play_sample_rate * self.bytes_per_sample * FRAME_MS // 1000


FRAME_MS = 20

# How long to wait for clearedAudio before giving up and playing anyway. Vobiz
# acknowledges a flush in milliseconds on a healthy stream, so this only ever fires
# when something has gone wrong -- and when it does, a clipped word beats an agent
# that has gone silent for the rest of the call.
CLEAR_ACK_TIMEOUT_S = float(os.getenv("CLEAR_ACK_TIMEOUT_S", "1.0"))

AUDIO_PROFILES = {
    # Matches the PSTN leg exactly, both directions. Deepgram resamples internally
    # for Flux, so 8 kHz in costs nothing at the agent.
    "mulaw": AudioProfile(
        stream_content_type="audio/x-mulaw;rate=8000",
        agent_input={"encoding": "mulaw", "sample_rate": 8000},
        agent_output={"encoding": "mulaw", "sample_rate": 8000, "container": "none"},
        play_content_type="audio/x-mulaw",
        play_sample_rate=8000,
        bytes_per_sample=1,
    ),
    # Wider band for the leg we control: 16 kHz from Vobiz (its inbound ceiling),
    # 24 kHz back, which is Flux TTS's native rate and avoids a downsample.
    "l16": AudioProfile(
        stream_content_type="audio/x-l16;rate=16000",
        agent_input={"encoding": "linear16", "sample_rate": 16000},
        agent_output={"encoding": "linear16", "sample_rate": 24000, "container": "none"},
        play_content_type="audio/x-l16",
        play_sample_rate=24000,
        bytes_per_sample=2,
    ),
}

AUDIO_MODE = _optional("AUDIO_MODE", "mulaw").lower()
if AUDIO_MODE not in AUDIO_PROFILES:
    raise SystemExit(f"AUDIO_MODE must be one of {sorted(AUDIO_PROFILES)}, got {AUDIO_MODE!r}")
PROFILE = AUDIO_PROFILES[AUDIO_MODE]


# --- Which Deepgram region --------------------------------------------------
# Deepgram serves India from api.in.deepgram.com (AWS ap-south-2, Hyderabad), with
# the same API surface as the global endpoint. For calls that terminate in India
# this is not a preference, it is the difference between a natural conversation and
# a noticeable pause: measured from Bengaluru, time-to-first-audio on a greeting was
# ~0.4s against India versus ~1.5s against the global endpoint. On a phone call that
# gap is the whole impression.
#
# Audio, transcripts and synthesis stay in India by default. Two caveats worth
# knowing before promising anyone full data residency: the LLM step runs wherever
# that provider runs, and operational metadata and billing are processed in the US.
#
# The SDK picks the Voice Agent host from environment.agent -- which by default is
# agent.deepgram.com, a different host from the REST base -- so pointing at India
# means overriding the environment rather than just a base URL.
DEEPGRAM_REGION = _optional("DEEPGRAM_REGION", "india").lower()
DEEPGRAM_REGIONS = {
    "india": "api.in.deepgram.com",
    "global": None,  # SDK default: api.deepgram.com REST, agent.deepgram.com for the agent
}
if DEEPGRAM_REGION not in DEEPGRAM_REGIONS:
    raise SystemExit(f"DEEPGRAM_REGION must be one of {sorted(DEEPGRAM_REGIONS)}, got {DEEPGRAM_REGION!r}")

_region_host = DEEPGRAM_REGIONS[DEEPGRAM_REGION]
DG_ENVIRONMENT = (
    DeepgramClientEnvironment(
        base=f"https://{_region_host}",
        production=f"wss://{_region_host}",
        agent=f"wss://{_region_host}",
        agent_rest=f"https://{_region_host}",
    )
    if _region_host
    else DeepgramClientEnvironment.PRODUCTION
)


# --- Voice and language --------------------------------------------------------
# Deepgram's Indian coverage pairs broad recognition with Indian-accented English
# speech, and matching those two is what these profiles do:
#
#   listening  Flux multilingual covers Hindi, and Nova-3 adds Tamil, Telugu,
#              Marathi, Bengali, Gujarati and Punjabi. Good coverage.
#   speaking   Indian-accented ENGLISH in Flux TTS -- meena, naveen and priya --
#              and for an Indian caller that accent matters more than most people
#              expect.
#
# So the agent understands the caller in their language and answers in English. That
# is how Indian support desks already work, and the hi-IN profile below leans into
# it: understand whatever the caller speaks, answer in Hinglish rendered by an
# Indian-accented voice.
@dataclass(frozen=True)
class LocaleProfile:
    """STT model, language hinting and a matching voice for one target audience."""

    stt_model: str
    stt_version: str
    stt_language: dict     # merged into the listen provider as-is
    tts_model: str
    greeting: str
    prompt: str


# Every prompt below is deliberately short. The system prompt is re-sent as context
# on every turn, so each extra sentence costs time-to-first-token on every reply --
# and on a phone call that is the latency the caller actually feels.
#
# The hard rule in all of them: ALWAYS ANSWER IN ENGLISH.
#
# These are English voice models, so romanised Hindi ("aapko kya chahiye") pushes
# them out of distribution: the voice has to guess grapheme-to-phoneme on letter
# sequences English never produces, which is slower to synthesise and rougher to
# listen to. English text is both the faster and the better-sounding choice. The
# Indian feel comes from the VOICE (naveen, meena and priya are Indian-accented)
# and from English vocabulary a caller here expects -- not from Hindi text.
_STYLE = (
    "Answer in English, always. One or two short spoken sentences. No lists or "
    "markdown."
)

# Callers on a demo number always ask what they are talking to.
_IDENTITY = (
    "You are the demo voice agent for the Vobiz and Deepgram integration, on a call "
    "over Vobiz in India with Deepgram's India region doing speech and language. "
    "Say so briefly if asked."
)

# Understands Hindi, replies in English. This is the honest description of what the
# stack can do, and it is also how plenty of Indian support desks already run.
_HEARS_HINDI = (
    "The caller may speak Hindi, English or a mix. Understand any of it, but reply "
    "in English."
)

# Indian conversational vocabulary that a general English model mishears. Flux v2
# accepts keyterms to raise recall on exactly this kind of domain noun, and on a
# support line these are the words that actually carry the caller's intent.
INDIA_KEYTERMS = [
    # The two names the agent says most often, and the two Flux mangles worst
    # without help -- on a live call "Vobiz" came back as "boobies" and "Vobel".
    "Vobiz", "Deepgram",
    "Aadhaar", "UPI", "PAN card", "GST", "IFSC", "RuPay", "Paytm", "PhonePe",
    "lakh", "crore", "rupees", "PIN code", "KYC", "OTP", "SIM", "recharge",
    "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Chennai", "Pune", "Kolkata",
]

LOCALES = {
    # The India default, and the fastest configuration available. The monolingual
    # Flux model has tighter end-of-turn behaviour than the multilingual one, the
    # reply is English so the voice stays inside its own language, and the voice is
    # Indian-accented so the call still sounds local.
    "en-in": LocaleProfile(
        stt_model="flux-general-en",
        stt_version="v2",
        stt_language={"keyterms": INDIA_KEYTERMS},
        tts_model="flux-meena-en",
        greeting="Hi! This is the Vobiz and Deepgram voice agent. How can I help you today?",
        prompt=" ".join(
            [
                _IDENTITY,
                _STYLE,
                "Use Indian English: lakh, crore, rupees.",
            ]
        ),
    ),
    # Understands Hindi and English including mid-sentence code-switching, which is
    # how callers here actually talk -- and still answers in English, because that is
    # the only language the voice can speak well. Costs a little latency against
    # en-in for the multilingual model.
    "hi-in": LocaleProfile(
        stt_model="flux-general-multi",
        stt_version="v2",
        stt_language={"language_hints": ["hi", "en"], "keyterms": INDIA_KEYTERMS},
        tts_model="flux-meena-en",
        greeting="Hi! This is the Vobiz and Deepgram voice agent. How can I help you today?",
        prompt=" ".join(
            [
                _IDENTITY,
                _HEARS_HINDI,
                _STYLE,
                "Use Indian English: lakh, crore, rupees.",
            ]
        ),
    ),
    # Indic languages beyond Hindi. Nova-3 is the only model that covers them and it
    # is a v1 provider, so this trades Flux's end-of-turn detection -- and the eager
    # end-of-turn latency trick below -- for breadth. Noticeably slower; use it only
    # when the caller genuinely needs Tamil, Telugu, Marathi, Bengali, Gujarati,
    # Punjabi, Kannada, Assamese or Urdu. Set INDIC_LANGUAGE to its code.
    "indic": LocaleProfile(
        stt_model="nova-3",
        stt_version="v1",
        stt_language={
            "language": _optional("INDIC_LANGUAGE", "ta"),
            "keyterms": INDIA_KEYTERMS,
            "smart_format": True,
        },
        tts_model="flux-meena-en",
        greeting="Hi! This is the Vobiz and Deepgram voice agent. How can I help you today?",
        prompt=" ".join(
            [
                _IDENTITY,
                "Understand the caller's language, but reply in simple English.",
                _STYLE,
            ]
        ),
    ),
    # Non-India deployments, or an A/B against the default American voice.
    "en-us": LocaleProfile(
        stt_model="flux-general-en",
        stt_version="v2",
        stt_language={},
        tts_model="flux-alexis-en",
        greeting="Hi! Thanks for calling. How can I help you today?",
        prompt=" ".join([_IDENTITY, _STYLE]),
    ),
}

AGENT_LOCALE = _optional("AGENT_LOCALE", "en-in").lower()
if AGENT_LOCALE not in LOCALES:
    raise SystemExit(f"AGENT_LOCALE must be one of {sorted(LOCALES)}, got {AGENT_LOCALE!r}")
LOCALE = LOCALES[AGENT_LOCALE]

# Three Indian-accented Flux voices exist, and DG_TTS_MODEL overrides the locale's
# choice with any of them:
#   flux-meena-en   female -- customer service, casual chat   (the default here)
#   flux-priya-en   female -- IVR, confident and reassuring
#   flux-naveen-en  male   -- IVR, support, informative
DG_STT_MODEL = LOCALE.stt_model
DG_TTS_MODEL = _optional("DG_TTS_MODEL", LOCALE.tts_model)

# --- The language model, which is where the latency actually lives -------------
# Deepgram's own LatencyReport settles this. Measured on a live call, the stages
# came out: recognition a few hundred ms, synthesis 43-103 ms, and time-to-first
# LLM token 900-1900 ms. The language model is not one factor among several, it is
# effectively the whole delay -- so this is the only choice on this page worth
# benchmarking, and no amount of end-of-turn tuning substitutes for getting it right.
#
# Benchmarked against the India endpoint, six turns each, median time from a user
# message to the first byte of audio:
#
#   anthropic  claude-haiku-4-5        945 ms   (822-962)    <- chosen
#   open_ai    gpt-4.1-mini           1347 ms  (1202-1790)
#   google     gemini-3.1-flash-lite  1387 ms  (1196-1465)
#   open_ai    gpt-4o-mini            1444 ms  (1115-1755)
#   google     gemini-2.5-flash       1807 ms
#   google     gemini-3.5-flash       2700 ms
#   open_ai    gpt-5-mini             4792 ms
#
# Haiku wins on the median and, more importantly for a phone call, on spread: its
# slowest turn was faster than any other model's median. A caller forgives a
# consistent beat far more readily than one that varies by a second.
#
# All four providers below are Deepgram-managed, so none needs an API key of its
# own. Note the LLM step is the one part of the pipeline that does not run in
# India -- it executes wherever the model provider runs -- which is why these
# numbers are what they are and why the ranking is worth re-checking from your own
# region rather than taken on faith.
LLM_PROVIDER = _optional("LLM_PROVIDER", "anthropic")
LLM_MODEL = _optional("LLM_MODEL", "claude-haiku-4-5")

GREETING = _optional("GREETING", LOCALE.greeting)
PROMPT = LOCALE.prompt

# Turn-taking, tunable because the right answer depends on the line.
#
# Flux decides when the caller has finished speaking, and that judgement is the
# single biggest lever on how the agent feels. Both knobs live on the listen
# provider, not on the agent:
#
#   EOT_THRESHOLD    confidence required to end a turn (0.5-0.9, Deepgram default
#                    0.7). Raise it on a noisy or low-bitrate leg so background
#                    speech and crosstalk are less likely to be read as the caller
#                    taking a turn; lower it for a snappier agent on a clean line.
#   EOT_TIMEOUT_MS   hard ceiling -- end the turn this long after speech regardless
#                    of confidence (Deepgram default 5000). Five seconds of dead air
#                    is a long time on a phone call, so we shorten it.
#
# Mu-law at 8 kHz gives the model less to work with than the 16 kHz leg, so it gets
# the more conservative threshold by default.
EOT_THRESHOLD = float(_optional("EOT_THRESHOLD", "0.7"))
EOT_TIMEOUT_MS = int(_optional("EOT_TIMEOUT_MS", "3000"))

# The biggest latency win available here, and it is one line of configuration.
#
# With an eager threshold set, Flux emits a medium-confidence end-of-turn as soon as
# the caller *probably* stopped, and the agent starts generating the reply against
# that guess while still listening. If the caller turns out to be mid-sentence Flux
# retracts it and the speculative work is thrown away. Deepgram documents this as
# cutting hundreds of milliseconds off the response, at the cost of some extra LLM
# calls -- an easy trade on a phone call, where the pause is what makes an agent
# feel robotic.
#
# 0.4 is Deepgram's own low-latency profile. Raise it toward eot_threshold to
# speculate less often, or set it to 0 to switch speculation off entirely.
# Flux (v2) only -- Nova does its own endpointing and rejects the field.
EAGER_EOT_THRESHOLD = float(_optional("EAGER_EOT_THRESHOLD", "0.4"))
if EAGER_EOT_THRESHOLD and not 0.3 <= EAGER_EOT_THRESHOLD <= 0.9:
    raise SystemExit(
        f"EAGER_EOT_THRESHOLD must be 0 (off) or between 0.3 and 0.9, got {EAGER_EOT_THRESHOLD}"
    )
# The API reference gives the range as 0.5-1.0; the SDK docstring says 0.5-0.9.
# Trust the API reference, and stay well inside both by default.
if not 0.5 <= EOT_THRESHOLD <= 1.0:
    raise SystemExit(f"EOT_THRESHOLD must be between 0.5 and 1.0, got {EOT_THRESHOLD}")

# Both Flux models are served from v2 endpoints, so each provider pins version="v2".
# Drop it and the provider silently falls back to v1 -- Nova for listen, Aura for
# speak -- where neither Flux model name is valid.
#
# agent.language is deliberately absent: Deepgram deprecated it in favour of
# per-provider language settings, and the Flux v2 listen provider does not take a
# language field at all (only language_hints, and only for flux-general-multi).
_listen_provider = {
    "type": "deepgram",
    "version": LOCALE.stt_version,
    "model": DG_STT_MODEL,
    **LOCALE.stt_language,
}
# The end-of-turn knobs belong to the Flux v2 listen provider. Nova (v1) does its
# own endpointing and rejects them, so only send them where they mean something.
if LOCALE.stt_version == "v2":
    _listen_provider["eot_threshold"] = EOT_THRESHOLD
    _listen_provider["eot_timeout_ms"] = EOT_TIMEOUT_MS
    if EAGER_EOT_THRESHOLD:
        _listen_provider["eager_eot_threshold"] = EAGER_EOT_THRESHOLD

AGENT_SETTINGS = AgentV1Settings.model_validate(
    {
        "type": "Settings",
        "audio": {"input": PROFILE.agent_input, "output": PROFILE.agent_output},
        "agent": {
            "listen": {"provider": _listen_provider},
            "think": {
                "provider": {"type": LLM_PROVIDER, "model": LLM_MODEL},
                "prompt": PROMPT,
            },
            "speak": {"provider": {"type": "deepgram", "version": "v2", "model": DG_TTS_MODEL}},
            "greeting": GREETING,
        },
    }
)

dg_client = AsyncDeepgramClient(api_key=DEEPGRAM_API_KEY, environment=DG_ENVIRONMENT)
app = FastAPI(title="Vobiz x Deepgram Voice Agent")


# --- Request plumbing ----------------------------------------------------------
def xml_attr(value: object) -> str:
    """Escape a value for use inside a double-quoted XML attribute."""
    return escape(str(value), quote=True)


async def webhook_params(request: Request) -> dict:
    """Collect webhook parameters from wherever this particular callback puts them.

    Voice callbacks are form-encoded on POST and query-string on GET. A few
    account-level callbacks send JSON instead, so we accept that too -- but note
    that json.loads happily returns a list, a string or None for well-formed input,
    and feeding any of those to dict.update raises. Ignore anything that is not an
    object rather than turning a malformed body into a 500 and a dropped call.
    """
    params = dict(request.query_params)
    if request.method != "POST":
        return params

    body = (await request.body()).decode(errors="replace")
    if "json" in request.headers.get("content-type", "").lower():
        try:
            parsed = json.loads(body or "{}")
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            params.update(parsed)
    else:
        params.update(dict(parse_qsl(body)))
    return params


def signature_ok(request: Request) -> bool:
    """Validate the Vobiz callback signature.

        V3   base64(HMAC-SHA256(authToken, baseURL + "." + nonce))
        V2   base64(HMAC-SHA256(authToken, baseURL + nonce))

    The signed message is the callback URL with its query string stripped, plus a
    nonce. The body is not covered. We rebuild the URL from PUBLIC_HOSTNAME because
    behind a tunnel request.url is the internal address, which is not what Vobiz
    signed. Both header pairs are accepted since coverage varies by callback type,
    and the whole thing fails closed when neither is present.
    """
    base_url = f"https://{PUBLIC_HOSTNAME}{request.url.path}"
    key = VOBIZ_AUTH_TOKEN.encode()
    seen_header = False

    for version, joiner in (("v3", "."), ("v2", "")):
        signature = request.headers.get(f"x-vobiz-signature-{version}", "")
        nonce = request.headers.get(f"x-vobiz-signature-{version}-nonce", "")
        if not signature or not nonce:
            continue
        seen_header = True
        message = (base_url + joiner + nonce).encode()
        expected = base64.b64encode(hmac.new(key, message, "sha256").digest())
        # Compare bytes, not str: compare_digest raises TypeError on non-ASCII text,
        # and this value arrives straight from a header.
        if hmac.compare_digest(signature.encode("utf-8", "replace"), expected):
            return True
        print(f"[security] {version.upper()} signature mismatch for {base_url}")

    if not seen_header:
        # Worth distinguishing, because the fix is completely different: this means
        # the callback URL has no auth credentials configured in the Vobiz console,
        # not that someone forged a request.
        print(f"[security] no signature headers on {base_url} -- configure callback auth credentials")
    return False


def reject_unsigned(request: Request) -> Response | None:
    """Shared guard for every webhook. Returns a response to send, or None to continue."""
    if VERIFY_SIGNATURE and not signature_ok(request):
        return Response(status_code=403, content="Invalid Vobiz signature")
    return None


# --- The Vobiz side of the media socket ----------------------------------------
class VobizStream:
    """Outbound half of the media protocol: playAudio, clearAudio and checkpoint.

    Every frame we send has to carry the streamId from the start event, so this
    object exists mainly to hold that id and refuse to send without it.
    """

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.stream_id: str | None = None
        self.call_id: str | None = None
        self.started = False
        self.turns = 0
        self.unheard: set[str] = set()
        self._pending = b""          # partial frame carried between chunks
        self._awaiting_clear = False  # a clearAudio flush is in flight
        self._held = b""             # audio that arrived during that flush
        self._clear_sent_at = 0.0    # when, so the wait cannot last forever
        # Two coroutines reach this object: pump_agent, for audio arriving from
        # Deepgram, and the media() loop, which releases held audio on clearedAudio.
        # Serialise them so frames cannot interleave or overtake each other.
        self._lock = asyncio.Lock()

    def read_start(self, message: dict) -> None:
        """Absorb the start event. Raises if it cannot be used to send audio."""
        start = message.get("start") or {}
        # Vobiz reports streamId at the top level on some events and nested on
        # others, so check both before giving up.
        self.stream_id = message.get("streamId") or start.get("streamId")
        self.call_id = start.get("callId") or message.get("callId")
        if not self.stream_id:
            raise ValueError(f"start event carries no streamId: {message!r}")

        # Nothing here resamples, so a format disagreement between the XML we served
        # and this profile would be inaudible garbage rather than an error. Compare
        # defensively: sampleRate is whatever the media server chose to send, and a
        # diagnostic has no business dropping the call it is diagnosing.
        fmt = start.get("mediaFormat") or {}
        encoding = str(fmt.get("encoding") or "")
        want = PROFILE.agent_input
        want_encoding = "mulaw" if want["encoding"] == "mulaw" else "l16"
        if encoding and want_encoding not in encoding.lower():
            print(f"[audio] WARNING Vobiz is sending {encoding} but AUDIO_MODE={AUDIO_MODE}")
        rate = fmt.get("sampleRate")
        if isinstance(rate, (int, float)) or (isinstance(rate, str) and rate.strip().isdigit()):
            if int(rate) != want["sample_rate"]:
                print(
                    f"[audio] WARNING Vobiz is sending {rate} Hz but the agent "
                    f"expects {want['sample_rate']} Hz"
                )
        elif rate is not None:
            print(f"[audio] start event reported an unreadable sampleRate: {rate!r}")
        self.started = True

    async def _send(self, payload: dict) -> None:
        await self.ws.send_text(json.dumps(payload))

    async def play(self, audio: bytes) -> None:
        """Queue agent audio for the caller, holding the socket for the whole batch."""
        async with self._lock:
            await self._play(audio)

    async def _play(self, audio: bytes) -> None:
        """Emit audio as 20 ms playAudio frames. Caller must hold the lock.

        Deepgram hands over chunks on its own schedule -- mostly exact multiples of
        a frame, but the first and last of a turn rarely are. Carrying the remainder
        across calls keeps every frame we emit exactly one frame long, instead of
        emitting a short one at each chunk boundary.
        """
        if not self.stream_id:
            return
        if self._awaiting_clear:
            # Wait for the flush to be acknowledged -- but never indefinitely. If the
            # ack is lost, or Vobiz declines to send one, holding audio forever would
            # mute the agent for the remainder of the call, which is a far worse
            # outcome than the clipped syllable the gating exists to prevent.
            if (asyncio.get_running_loop().time() - self._clear_sent_at) > CLEAR_ACK_TIMEOUT_S:
                print("[call] clearedAudio never arrived -- resuming playback anyway")
                self._awaiting_clear = False
                audio = self._held + audio
                self._held = b""
            else:
                self._held += audio
                return
        buffer = self._pending + audio
        size = PROFILE.frame_bytes
        whole = len(buffer) - (len(buffer) % size)
        for offset in range(0, whole, size):
            await self._send(
                {
                    "event": "playAudio",
                    "streamId": self.stream_id,
                    "media": {
                        "contentType": PROFILE.play_content_type,
                        "sampleRate": PROFILE.play_sample_rate,
                        "payload": base64.b64encode(buffer[offset:offset + size]).decode(),
                    },
                }
            )
        self._pending = buffer[whole:]

    async def flush(self) -> None:
        """Emit any partial frame held back by play(), at the end of a turn."""
        async with self._lock:
            await self._flush()

    async def _flush(self) -> None:
        if self.stream_id and self._pending:
            tail, self._pending = self._pending, b""
            await self._send(
                {
                    "event": "playAudio",
                    "streamId": self.stream_id,
                    "media": {
                        "contentType": PROFILE.play_content_type,
                        "sampleRate": PROFILE.play_sample_rate,
                        "payload": base64.b64encode(tail).decode(),
                    },
                }
            )

    async def clear(self) -> None:
        """Barge-in: drop whatever Vobiz still holds buffered for playback.

        Vobiz's protocol requires waiting for the clearedAudio acknowledgement before
        sending more playAudio. Queue anything that arrives in between instead: audio
        sent into an in-flight flush races it and is partially dropped, which the
        caller hears as the next reply starting mid-word or breaking up. The frames
        already queued for the interrupted turn are genuinely stale, so they go, and
        with them the checkpoints that will now never be acknowledged -- Vobiz voids
        any checkpoint whose audio was still queued when the flush happened.
        """
        async with self._lock:
            await self._clear()

    async def _clear(self) -> None:
        if not self.stream_id:
            return
        self._pending = b""
        self.unheard.clear()
        self._awaiting_clear = True
        self._clear_sent_at = asyncio.get_running_loop().time()
        await self._send({"event": "clearAudio", "streamId": self.stream_id})

    async def cleared(self) -> None:
        """clearedAudio arrived: the flush is done, so release anything held back."""
        async with self._lock:
            self._awaiting_clear = False
            held, self._held = self._held, b""
            if held:
                await self._play(held)

    async def checkpoint(self) -> None:
        """Mark the end of a turn and remember it until Vobiz says it was played.

        Deepgram's AgentAudioDone means it has finished *sending* audio, not that the
        caller heard it -- the media server may still have seconds of it buffered.
        The playedStream reply to this checkpoint is the only signal that the turn
        actually reached the caller, so track what is outstanding and report anything
        still unplayed when the stream ends.
        """
        async with self._lock:
            await self._checkpoint()

    async def _checkpoint(self) -> None:
        if not self.stream_id:
            return
        self.turns += 1
        name = f"turn-{self.turns}"
        self.unheard.add(name)
        await self._send({"event": "checkpoint", "streamId": self.stream_id, "name": name})

    def mark_played(self, name: str | None) -> None:
        self.unheard.discard(name or "")


# --- Deepgram -> Vobiz ---------------------------------------------------------
async def pump_agent(stream: VobizStream, agent) -> None:
    """Relay one Deepgram agent connection onto the Vobiz socket.

    Output audio arrives as raw bytes; everything else arrives as a typed event.
    This is the main writer to the Vobiz socket, but not the only one: the media()
    loop also sends, via VobizStream.cleared(), when it releases audio held during a
    barge-in flush. VobizStream serialises the two behind its own lock, so ordering
    is the stream's guarantee rather than a property of this task being alone.
    """
    while True:
        try:
            message = await agent.recv()
        except Exception as exc:
            # Usually a normal close at end of call, but an expired key or an
            # exhausted quota lands here too, and that is worth a line -- otherwise
            # the only visible symptom is a call that goes quiet.
            print(f"[deepgram] receive loop ended: {type(exc).__name__}: {exc}")
            return

        if isinstance(message, bytes):
            await stream.play(message)
        elif isinstance(message, AgentV1UserStartedSpeaking):
            # Deepgram has already stopped generating; our job is to drop what Vobiz
            # still has queued so the agent goes quiet immediately.
            await stream.clear()
        elif isinstance(message, AgentV1AgentAudioDone):
            await stream.flush()
            await stream.checkpoint()
        elif isinstance(message, AgentV1LatencyReport):
            # Deepgram's own per-turn breakdown, and the only way to tell whether a
            # slow reply is recognition, the language model or synthesis. Every
            # field is optional, so read defensively.
            parts = [
                f"{label}={value * 1000:.0f}ms"
                for label, value in (
                    ("stt", message.stt_latency),
                    ("llm_first_token", message.ttt_text_latency),
                    ("tts", message.tts_latency),
                    ("total", message.total_latency),
                )
                if value is not None
            ]
            # Deepgram emits a report per recognition segment as well as per turn.
            # Only the turn-level one carries total_latency, and only that one is
            # worth a line -- the rest arrive tens of times per turn.
            if parts and message.total_latency is not None:
                print(f"[latency] {'  '.join(parts)}")
        elif isinstance(message, AgentV1ConversationText):
            print(f"[{message.role}] {message.content}")
        elif isinstance(message, (AgentV1Welcome, AgentV1SettingsApplied)):
            print(f"[deepgram] {type(message).__name__}")
        elif isinstance(message, (AgentV1Error, AgentV1Warning)):
            print(f"[deepgram] {type(message).__name__}: {message}")


# --- The media socket ----------------------------------------------------------
@app.websocket("/media")
@app.websocket("/media/{secret}")
async def media(vobiz_ws: WebSocket, secret: str = "") -> None:
    """Bridge one call. Vobiz opens this socket after fetching the answer URL.

    The shared secret is checked here, at the handshake, before accept() and before
    any Deepgram connection exists -- so a rejected socket costs nothing.
    """
    if STREAM_SECRET and not hmac.compare_digest(
        secret.encode("utf-8", "replace"), STREAM_SECRET.encode()
    ):
        print("[security] rejected /media: bad or missing path secret")
        await vobiz_ws.close(code=1008)  # policy violation
        return

    await vobiz_ws.accept()
    stream = VobizStream(vobiz_ws)
    relay: asyncio.Task | None = None

    # Entered by hand rather than with `async with`, because a rejected key raises
    # right here -- and the Vobiz socket is already accepted by this point. Letting
    # that exception escape leaves the caller listening to silence until they give up
    # and hang up, so close the socket deliberately instead.
    connection = dg_client.agent.v1.connect()
    try:
        agent = await connection.__aenter__()
    except ApiError as exc:
        # The SDK redacts the key in its own error text, so this is safe to print.
        print(f"[deepgram] connect rejected: HTTP {exc.status_code} -- check DEEPGRAM_API_KEY")
        await vobiz_ws.close(code=1011)  # internal error
        return
    except Exception as exc:
        print(f"[deepgram] connect failed: {type(exc).__name__}: {exc}")
        await vobiz_ws.close(code=1011)
        return

    try:
        relay = asyncio.create_task(pump_agent(stream, agent))
        try:
            async for raw in vobiz_ws.iter_text():
                # A malformed frame must never take the call down with it.
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue

                event = message.get("event")

                if event == "start":
                    if stream.started:
                        continue  # duplicate start; the first one already configured us
                    try:
                        stream.read_start(message)
                    except ValueError as exc:
                        print(f"[call] unusable start event, closing: {exc}")
                        break
                    print(f"[call] started stream {stream.stream_id} (call {stream.call_id})")
                    # Sending settings is what starts the conversation, greeting included.
                    await agent.send_settings(AGENT_SETTINGS)

                elif event == "media":
                    if not stream.started:
                        continue  # audio before settings would be rejected by the agent
                    payload = (message.get("media") or {}).get("payload")
                    if not isinstance(payload, str) or not payload:
                        continue
                    try:
                        audio = base64.b64decode(payload, validate=True)
                    except Exception:
                        continue
                    await agent.send_media(audio)

                elif event == "playedStream":
                    name = message.get("name")
                    stream.mark_played(name)
                    print(f"[call] caller heard {name}")

                elif event == "clearedAudio":
                    # The flush is complete; release any audio held during it.
                    await stream.cleared()
                    print("[call] barge-in flushed")

                elif event == "stop":
                    print(f"[call] stopped ({message.get('reason', 'unknown')})")
                    break
        except WebSocketDisconnect:
            print("[call] caller hung up")
        finally:
            if stream.unheard:
                # The caller never confirmed hearing these turns, which is the
                # signature of audio dropped at the media server.
                print(f"[call] turns never confirmed played: {sorted(stream.unheard)}")
            if relay is not None:
                relay.cancel()
                # Collect it, so a write that lost a race with the closing socket is
                # reported here instead of surfacing later as an orphaned task.
                await asyncio.gather(relay, return_exceptions=True)
    finally:
        await connection.__aexit__(None, None, None)


# --- HTTP surface --------------------------------------------------------------
@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request) -> Response:
    """Answer URL. Returns the document that opens the media stream.

    bidirectional="true" is what makes the socket two-way, and therefore what makes
    barge-in and playAudio/clearAudio/checkpoint possible at all. The WebSocket URL
    is the element's text content, not a url="" attribute.
    """
    rejected = reject_unsigned(request)
    if rejected is not None:
        print("[security] rejected /answer")
        return rejected

    params = await webhook_params(request)
    print(f"[call] answer -- call {params.get('CallUUID', '(none)')!r} from {params.get('From')!r}")

    media_path = f"/media/{STREAM_SECRET}" if STREAM_SECRET else "/media"
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Stream bidirectional="true"
          audioTrack="inbound"
          keepCallAlive="true"
          contentType="{xml_attr(PROFILE.stream_content_type)}"
          statusCallbackUrl="{xml_attr(f'https://{PUBLIC_HOSTNAME}/stream-status')}"
          statusCallbackMethod="POST">wss://{xml_attr(PUBLIC_HOSTNAME)}{xml_attr(media_path)}</Stream>
  <Hangup/>
</Response>"""
    return Response(content=document, media_type="text/xml")


@app.post("/stream-status")
async def stream_status(request: Request) -> Response:
    """<Stream statusCallbackUrl> -- StartStream, PlayedStream, ClearedAudio, StopStream."""
    rejected = reject_unsigned(request)
    if rejected is not None:
        return rejected
    params = await webhook_params(request)
    print(f"[stream-status] {params.get('Event', params.get('event', '?'))} {params}")
    return Response(status_code=204)


@app.post("/hangup")
async def hangup(request: Request) -> Response:
    """Call-ended webhook. Cost is always zero here -- the real figure is in the CDR."""
    rejected = reject_unsigned(request)
    if rejected is not None:
        return rejected
    params = await webhook_params(request)
    print(
        f"[hangup] {params.get('CallUUID')} {params.get('HangupCause')} "
        f"duration={params.get('Duration')}s"
    )
    return Response(status_code=204)


@app.get("/health")
async def health() -> JSONResponse:
    """Resolved configuration. Deliberately reports whether the media socket is
    guarded without echoing the secret that guards it."""
    return JSONResponse(
        {
            "answer_url": f"https://{PUBLIC_HOSTNAME}/answer",
            "stream_url": f"wss://{PUBLIC_HOSTNAME}/media",
            "audio_mode": AUDIO_MODE,
            "vobiz_to_app": PROFILE.stream_content_type,
            "app_to_vobiz": f"{PROFILE.play_content_type};rate={PROFILE.play_sample_rate}",
            "play_frame_bytes": PROFILE.frame_bytes,
            "region": DEEPGRAM_REGION,
            "agent_host": DG_ENVIRONMENT.agent,
            "locale": AGENT_LOCALE,
            "agent": {
                "listen": DG_STT_MODEL,
                "listen_language": LOCALE.stt_language or "en",
                "think": f"{LLM_PROVIDER}/{LLM_MODEL}",
                "speak": DG_TTS_MODEL,
                "eot_threshold": EOT_THRESHOLD if LOCALE.stt_version == "v2" else None,
                "eot_timeout_ms": EOT_TIMEOUT_MS if LOCALE.stt_version == "v2" else None,
            },
            "webhook_signature_checked": VERIFY_SIGNATURE,
            "media_socket_authenticated": bool(STREAM_SECRET),
        }
    )


if __name__ == "__main__":
    # Bind loopback: a tunnel fronts this in every documented setup, and the media
    # socket is only as guarded as STREAM_SECRET makes it.
    import uvicorn

    uvicorn.run(
        "app:app",
        host=_optional("BIND_HOST", "127.0.0.1"),
        port=HTTP_PORT,
        reload=bool(os.getenv("DEV_RELOAD")),
    )
