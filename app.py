"""
Vobiz voice agent on the Deepgram Voice Agent API.

A caller dials a Vobiz number. Vobiz fetches the answer URL, gets back a <Stream> element, and
opens a bidirectional WebSocket to /media. This server is a thin audio bridge: it relays the
caller's audio into a single Deepgram Voice Agent WebSocket and relays the agent's audio back to
Vobiz as playAudio frames. Deepgram runs the whole conversation loop — speech-to-text, the LLM,
text-to-speech, and turn-taking — and tells us when the caller interrupts so we can flush Vobiz's
audio buffer (barge-in).

    Routes
      GET/POST /answer         the number's answer URL — returns the <Stream> XML
      WS       /media/<secret> the bidirectional media stream Vobiz connects to
      POST     /stream-status  <Stream statusCallbackUrl> — StartStream / StopStream
      GET      /health         resolved configuration, for a quick sanity check

Single file on purpose, so the whole flow is readable top to bottom.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import os
from html import escape
from urllib.parse import parse_qsl

from deepgram import AsyncDeepgramClient
from deepgram.agent.v1 import (
    AgentV1AgentAudioDone,
    AgentV1ConversationText,
    AgentV1Error,
    AgentV1Settings,
    AgentV1SettingsApplied,
    AgentV1UserStartedSpeaking,
    AgentV1Warning,
    AgentV1Welcome,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

load_dotenv()

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
PUBLIC_HOSTNAME = os.environ["PUBLIC_HOSTNAME"]  # host only, no scheme
HTTP_PORT = int(os.getenv("HTTP_PORT", "5050"))

# --- Security (optional but strongly recommended for anything reachable) -------
# Both endpoints below are exposed to the public internet through your tunnel.
# Without these, anyone who learns your URL can POST fake webhooks or open a media
# socket and run up your Deepgram bill.
#
#   VERIFY_SIGNATURE -> checks the X-Vobiz-Signature-V3/V2 HMAC on /answer, keyed by
#                       VOBIZ_AUTH_TOKEN. Opt-in, because Vobiz only emits signature
#                       headers when the callback URL has auth credentials configured
#                       on it — turn this on once you have set those, not before.
#   STREAM_SECRET    -> a random string embedded in the media stream's URL path, checked
#                       on connect, so only sockets opened by our own XML are served.
#                       (extraHeaders cannot do this — see the /media handler.)
#
# If unset, validation is skipped and a warning is printed — handy for a first local
# run, but do not leave it off on a shared or long-lived tunnel.
VOBIZ_AUTH_TOKEN = os.environ.get("VOBIZ_AUTH_TOKEN", "")
VERIFY_SIGNATURE = os.getenv("VERIFY_SIGNATURE", "").strip().lower() in ("1", "true", "yes")
STREAM_SECRET = os.environ.get("STREAM_SECRET")

if VERIFY_SIGNATURE and not VOBIZ_AUTH_TOKEN:
    raise SystemExit("VERIFY_SIGNATURE is on but VOBIZ_AUTH_TOKEN is empty — it is the signing key.")

if STREAM_SECRET and not STREAM_SECRET.isalnum():
    raise SystemExit(
        "STREAM_SECRET must be alphanumeric — it becomes a URL path segment.\n"
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(16))\""
    )
if not VERIFY_SIGNATURE:
    print("[security] VERIFY_SIGNATURE not set — /answer requests are NOT verified.")
if not STREAM_SECRET:
    print("[security] STREAM_SECRET not set — /media connections are NOT authenticated.")

# --- Models / prompt ----------------------------------------------------------
DG_STT_MODEL = "flux-general-en"      # Deepgram Flux (conversational STT, /v2/listen)
DG_TTS_MODEL = "flux-alexis-en"       # Deepgram Flux (streaming TTS voice, /v2/speak)
LLM_MODEL = "gpt-4o-mini"             # OpenAI, run as Deepgram's "think" provider

GREETING = "Hi! Thanks for calling. How can I help you today?"
PROMPT = (
    "You are a friendly voice assistant on a phone call. "
    "Reply in one or two short, spoken-sounding sentences. "
    "No lists, no markdown, no preamble — just answer."
)

# --- Audio ---------------------------------------------------------------------
# the two directions are configured independently and neither needs resampling here.
#
#   Vobiz -> app   the <Stream contentType> attribute. Allowed: audio/x-mulaw;rate=8000,
#                  audio/x-l16;rate=8000, audio/x-l16;rate=16000.
#   app -> Vobiz   the contentType/sampleRate inside each playAudio frame.
#
# `mulaw` is the default: 8 kHz mu-law both ways, matching the PSTN leg exactly, and the
# Deepgram agent resamples internally for Flux. `l16` trades a little bandwidth for a
# wider band — 16 kHz in, 24 kHz out. playAudio accepts L16 at 8/16/24 kHz and mu-law at 8 kHz.
# Never ask for 24 kHz via <Stream contentType> — that attribute configures the INBOUND
# direction, which tops out at 16 kHz. 24 kHz is an outbound-only option.
AUDIO_PROFILES = {
    "mulaw": {
        "stream_content_type": "audio/x-mulaw;rate=8000",
        "agent_input": {"encoding": "mulaw", "sample_rate": 8000},
        "agent_output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"},
        "play_content_type": "audio/x-mulaw",
        "play_sample_rate": 8000,
        "bytes_per_sample": 1,
    },
    "l16": {
        "stream_content_type": "audio/x-l16;rate=16000",
        "agent_input": {"encoding": "linear16", "sample_rate": 16000},
        "agent_output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
        "play_content_type": "audio/x-l16",
        "play_sample_rate": 24000,
        "bytes_per_sample": 2,
    },
}
AUDIO_MODE = os.getenv("AUDIO_MODE", "mulaw").lower()
if AUDIO_MODE not in AUDIO_PROFILES:
    raise SystemExit(f"AUDIO_MODE must be one of {sorted(AUDIO_PROFILES)}, got {AUDIO_MODE!r}")
PROFILE = AUDIO_PROFILES[AUDIO_MODE]

# One playAudio frame per 20 ms of audio. Deepgram hands us arbitrarily sized chunks; Vobiz is
# happiest with steady telephony-sized frames, so we re-slice rather than forward blindly.
PLAY_FRAME_MS = 20
PLAY_FRAME_BYTES = (
    PROFILE["play_sample_rate"] * PROFILE["bytes_per_sample"] * PLAY_FRAME_MS // 1000
)

# Voice Agent configuration. Deepgram runs STT (listen), the LLM (think), and TTS (speak) behind
# this one connection.
#
# Both ends of the pipeline are Flux, Deepgram's conversational speech models, and both live
# on v2 endpoints — so each provider must pin `version: "v2"`. Omit it and the provider falls
# back to v1, where the Flux model names are not valid:
#
#   listen -> /v2/listen, "flux-general-en" (v1 is Nova). Flux brings model-integrated
#             end-of-turn detection tuned for voice agents.
#   speak  -> /v2/speak, "flux-{voice}-en" (v1 is Aura). Flux TTS is the streaming voice model.
AGENT_SETTINGS = AgentV1Settings.model_validate(
    {
        "type": "Settings",
        "audio": {"input": PROFILE["agent_input"], "output": PROFILE["agent_output"]},
        "agent": {
            "language": "en",
            "listen": {"provider": {"type": "deepgram", "version": "v2", "model": DG_STT_MODEL}},
            "think": {"provider": {"type": "open_ai", "model": LLM_MODEL}, "prompt": PROMPT},
            "speak": {"provider": {"type": "deepgram", "version": "v2", "model": DG_TTS_MODEL}},
            "greeting": GREETING,
        },
    }
)

dg_client = AsyncDeepgramClient(api_key=DEEPGRAM_API_KEY)

app = FastAPI(title="Vobiz × Deepgram Voice Agent")


# --- Helpers -------------------------------------------------------------------
def xe(value: str) -> str:
    """Escape a value for use inside an XML attribute."""
    return escape(str(value), quote=True)


async def call_params(request: Request) -> dict:
    """call parameters arrive form-encoded on POST, or in the query string on GET."""
    params = dict(request.query_params)
    if request.method == "POST":
        text = (await request.body()).decode(errors="replace")
        if "json" in request.headers.get("content-type", ""):
            try:
                params.update(json.loads(text or "{}"))
            except json.JSONDecodeError:
                pass
        else:
            params.update(dict(parse_qsl(text)))
    return params


def verify_vobiz_signature(request: Request) -> bool:
    """Signature is base64 HMAC-SHA256 keyed by the account **auth token**, over the base
    callback URL plus a nonce:

        V3   base64(HMAC-SHA256(authToken, baseURL + "." + nonce))
        V2   base64(HMAC-SHA256(authToken, baseURL + nonce))

    It covers the URL and nonce, never the body — voice webhooks are form-encoded, so any
    scheme that hashes a JSON body will never verify. Query parameters are stripped from the
    URL. Behind a tunnel `request.url` is the internal address, so the public URL is rebuilt
    from PUBLIC_HOSTNAME to match what Vobiz signed. V3 is preferred; V2 is accepted because
    coverage varies by callback type. Fails closed when neither header is present.
    """
    base_url = f"https://{PUBLIC_HOSTNAME}{request.url.path}"
    key = VOBIZ_AUTH_TOKEN.encode()
    for version, separator in (("V3", "."), ("V2", "")):
        signature = request.headers.get(f"x-vobiz-signature-{version.lower()}", "")
        nonce = request.headers.get(f"x-vobiz-signature-{version.lower()}-nonce", "")
        if not signature or not nonce:
            continue
        message = (base_url + separator + nonce).encode()
        expected = base64.b64encode(hmac.new(key, message, "sha256").digest()).decode()
        if hmac.compare_digest(signature, expected):
            return True
        print(f"[security] {version} signature mismatch for {base_url}")
    return False


def parse_extra_headers(raw) -> dict:
    """`extra_headers` arrives on the start event, as JSON or as `k=v,k2=v2`."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    out = {}
    for pair in raw.split(","):
        key, _, value = pair.partition("=")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


class VobizStream:
    """the four events we send back — playAudio, clearAudio, checkpoint — and the ids
    every one of them has to carry."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.stream_id: str | None = None
        self.call_id: str | None = None
        self.turns = 0

    def read_start(self, msg: dict) -> None:
        start = msg.get("start") or {}
        # Vobiz puts streamId at the top level on some events and inside `start` on others.
        self.stream_id = msg.get("streamId") or start.get("streamId")
        self.call_id = start.get("callId") or msg.get("callId")

        # We do no resampling, so a contentType mismatch between the XML and this profile is
        # silent garbage audio into the agent. Say so loudly instead.
        fmt = start.get("mediaFormat") or {}
        encoding = str(fmt.get("encoding") or "")
        rate = fmt.get("sampleRate")
        want = PROFILE["agent_input"]
        want_encoding = "mulaw" if want["encoding"] == "mulaw" else "l16"
        if encoding and want_encoding not in encoding.lower():
            print(f"[audio] WARNING Vobiz is sending {encoding} but AUDIO_MODE={AUDIO_MODE}")
        if rate and int(rate) != want["sample_rate"]:
            print(f"[audio] WARNING Vobiz is sending {rate} Hz but the agent expects "
                  f"{want['sample_rate']} Hz")

    async def send(self, payload: dict) -> None:
        await self.ws.send_text(json.dumps(payload))

    async def play(self, audio: bytes) -> None:
        """Agent output audio -> playAudio frames on the call leg."""
        for i in range(0, len(audio), PLAY_FRAME_BYTES):
            frame = {
                "event": "playAudio",
                "media": {
                    "contentType": PROFILE["play_content_type"],
                    "sampleRate": PROFILE["play_sample_rate"],
                    "payload": base64.b64encode(audio[i:i + PLAY_FRAME_BYTES]).decode(),
                },
            }
            if self.stream_id:
                frame["streamId"] = self.stream_id
            await self.send(frame)

    async def clear(self) -> None:
        """Barge-in — drop whatever Vobiz still has queued for playback."""
        if self.stream_id:
            await self.send({"event": "clearAudio", "streamId": self.stream_id})

    async def checkpoint(self) -> str:
        """Ask Vobiz to confirm when everything queued so far has actually reached the caller.

        Vobiz answers with playedStream once the buffered audio has played out, which is the
        only signal that the caller heard the turn rather than merely that we sent it.
        """
        self.turns += 1
        name = f"turn-{self.turns}"
        if self.stream_id:
            await self.send(
                {"event": "checkpoint", "streamId": self.stream_id, "name": name}
            )
        return name


# --- XML: tell Vobiz to open a bidirectional media stream to /media -----------
@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request) -> Response:
    params = await call_params(request)

    # Verify the request actually came from Vobiz before responding.
    if VERIFY_SIGNATURE and not verify_vobiz_signature(request):
        print("[security] rejected /answer: invalid Vobiz signature")
        return Response(status_code=403, content="Invalid Vobiz signature")

    print(f"[call] answer — call {params.get('CallUUID', '(none)')} from {params.get('From')}")

    # the WebSocket URL is the element's text content, not a url="" attribute, and three
    # attributes carry weight:
    #   bidirectional="true"  we send agent audio back over the same socket — this is what makes
    #                         barge-in possible, and what unlocks playAudio/clearAudio/checkpoint.
    #   keepCallAlive="true"  without it the <Stream> element returns immediately, the document
    #                         ends, and Vobiz hangs up on "End Of XML Instructions".
    #   audioTrack="inbound"  the media server rejects "both" when bidirectional is true.
    # If a STREAM_SECRET is set it becomes a path segment on the wss:// URL, so only sockets
    # opened by our own XML are served.
    media_path = f"/media/{STREAM_SECRET}" if STREAM_SECRET else "/media"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Stream bidirectional="true"
          audioTrack="inbound"
          keepCallAlive="true"
          contentType="{xe(PROFILE['stream_content_type'])}"
          statusCallbackUrl="{xe(f'https://{PUBLIC_HOSTNAME}/stream-status')}"
          statusCallbackMethod="POST">wss://{xe(PUBLIC_HOSTNAME)}{xe(media_path)}</Stream>
  <Hangup/>
</Response>"""
    return Response(content=xml, media_type="text/xml")


# --- Relay: Deepgram agent -> Vobiz ------------------------------------------
async def agent_to_vobiz(stream: VobizStream, agent) -> None:
    """Pump messages from the Voice Agent. Audio is forwarded to Vobiz; control events
    drive logging, barge-in and checkpoints. The agent sends output audio as raw `bytes`
    messages and everything else as typed event objects."""
    while True:
        try:
            message = await agent.recv()
        except Exception:
            break  # connection closed

        # Agent output audio -> Vobiz playAudio frames.
        if isinstance(message, bytes):
            if stream.stream_id:
                await stream.play(message)
            continue

        # Barge-in: Deepgram detected the caller talking and has stopped the agent. We flush
        # the audio Vobiz still has buffered so the agent goes quiet immediately.
        if isinstance(message, AgentV1UserStartedSpeaking):
            await stream.clear()
            continue

        # The agent finished a turn. Checkpoint it so Vobiz tells us when the caller heard it.
        if isinstance(message, AgentV1AgentAudioDone):
            await stream.checkpoint()
            continue

        if isinstance(message, AgentV1ConversationText):
            print(f"[{message.role}] {message.content}")
        elif isinstance(message, (AgentV1Welcome, AgentV1SettingsApplied)):
            print(f"[deepgram] {type(message).__name__}")
        elif isinstance(message, (AgentV1Error, AgentV1Warning)):
            print(f"[deepgram] {type(message).__name__}: {message}")


# --- Vobiz media WebSocket ---------------------------------------------------
@app.websocket("/media")
@app.websocket("/media/{secret}")
async def media(vobiz_ws: WebSocket, secret: str = "") -> None:
    # The secret rides in the URL path, not in extraHeaders.
    #
    # extraHeaders looks like the natural place for it, but it does not reach the socket at
    # all. Confirmed on a live call and in the media
    # server's own logs: `set_streaming_callback_params` builds the stream's parameter set from
    # {Event, Error, CallUUID, From, To, ServiceURL, status_callback_url, status_callback_method,
    # StreamID, Timestamp} — no extraHeaders. The values are echoed into the statusCallbackUrl
    # payload as `X-VH-<key>`, and nowhere else. The start frame's `extra_headers` stays "{}".
    # So extraHeaders is status-callback metadata, not stream credentials.
    if STREAM_SECRET and not hmac.compare_digest(secret, STREAM_SECRET):
        print("[security] rejected /media: bad or missing path secret")
        await vobiz_ws.close(code=1008)  # policy violation
        return
    await vobiz_ws.accept()
    stream = VobizStream(vobiz_ws)

    # One Deepgram Voice Agent connection for the life of the call.
    async with dg_client.agent.v1.connect() as agent:
        relay_task = asyncio.create_task(agent_to_vobiz(stream, agent))

        try:
            async for raw in vobiz_ws.iter_text():
                # Never trust a frame's shape — a malformed message must not kill the call.
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                event = msg.get("event")

                if event == "start":
                    stream.read_start(msg)
                    print(f"[call] started stream {stream.stream_id} (call {stream.call_id})")
                    # Sending settings starts the conversation (and the greeting).
                    await agent.send_settings(AGENT_SETTINGS)

                elif event == "media":
                    # Caller audio (inbound track) -> Deepgram agent.
                    payload = (msg.get("media") or {}).get("payload")
                    if not payload:
                        continue
                    try:
                        audio = base64.b64decode(payload)
                    except (ValueError, binascii.Error):
                        continue
                    await agent.send_media(audio)

                # Acknowledgements from Vobiz.
                elif event == "playedStream":
                    print(f"[call] caller heard {msg.get('name')}")
                elif event == "clearedAudio":
                    print("[call] barge-in flushed")

                elif event == "stop":
                    print(f"[call] stopped ({msg.get('reason', 'unknown')})")
                    break
        except WebSocketDisconnect:
            print("[call] websocket disconnected")
        finally:
            relay_task.cancel()


# --- Stream status callback ---------------------------------------------------
@app.post("/stream-status")
async def stream_status(request: Request) -> Response:
    """<Stream statusCallbackUrl> — StartStream, StopStream, and failures."""
    params = await call_params(request)
    print(f"[stream-status] {params.get('Event', params.get('event', '?'))} {params}")
    return Response(status_code=204)


@app.post("/hangup")
async def hangup(request: Request) -> Response:
    """The call's hangup_url — set by call.py on outbound calls. The hangup webhook always
    reports zero cost; the real number lands in the CDR a moment later."""
    params = await call_params(request)
    print(f"[hangup] {params.get('CallUUID')} {params.get('HangupCause')} "
          f"duration={params.get('Duration')}s")
    return Response(status_code=204)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "answer_url": f"https://{PUBLIC_HOSTNAME}/answer",
                "stream_url": f"wss://{PUBLIC_HOSTNAME}/media"
                          + (f"/{STREAM_SECRET}" if STREAM_SECRET else ""),
            "audio_mode": AUDIO_MODE,
            "vobiz_to_app": PROFILE["stream_content_type"],
            "app_to_vobiz": f"{PROFILE['play_content_type']};rate={PROFILE['play_sample_rate']}",
            "play_frame_bytes": PLAY_FRAME_BYTES,
            "agent": {"listen": DG_STT_MODEL, "think": LLM_MODEL, "speak": DG_TTS_MODEL},
                "answer_signature_checked": VERIFY_SIGNATURE,
            "stream_secret_checked": bool(STREAM_SECRET),
        }
    )


if __name__ == "__main__":
    # Convenience launcher so `python app.py` works. Equivalent to:
    #   uvicorn app:app --port 5050 --reload
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=HTTP_PORT, reload=True)
