"""
Stand in for Vobiz so the bridge can be tested without placing a call.

Speaks the Vobiz side of the media-stream protocol against a running app.py: opens the WebSocket,
sends `start`, streams audio in as `media` frames, collects the `playAudio` frames that come back,
answers `checkpoint` with `playedStream`, notes `clearAudio`, then sends `stop`.

    python app.py                                  # terminal 1
    python mock_vobiz.py                           # terminal 2 — streams silence
    python mock_vobiz.py --wav question.wav        # stream a real question at the agent
    python mock_vobiz.py --wav question.ulaw       # headerless, for the mulaw profile

A pass means playAudio frames came back in the format the XML asked for. With silence in you are
only testing the transport and the greeting; use --wav to exercise a full turn.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
from collections import Counter
from pathlib import Path

import websockets
from dotenv import load_dotenv

load_dotenv()

PROFILES = {
    "mulaw": {"encoding": "audio/x-mulaw", "rate": 8000, "width": 1, "silence": b"\xff"},
    "l16": {"encoding": "audio/x-l16", "rate": 16000, "width": 2, "silence": b"\x00\x00"},
}
FRAME_MS = 20


# RIFF format tags. The stdlib `wave` module only parses PCM: handed a mu-law file it
# raises "unknown format: 7", and -- the worse failure -- an 8-bit PCM file loads
# happily and then goes down the wire as though its bytes were mu-law, which the agent
# hears as noise. Telling those apart means reading the header ourselves.
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_MULAW = 7
FORMAT_NAMES = {WAVE_FORMAT_PCM: "PCM", WAVE_FORMAT_MULAW: "mu-law", 6: "A-law", 3: "float"}
HEADERLESS_SUFFIXES = (".ulaw", ".raw", ".pcm", ".l16")


def read_riff(path: str) -> tuple[bytes, int, int, int, int]:
    """Parse a WAV by hand: returns (samples, format_tag, channels, rate, bits)."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise SystemExit(f"{path}: not a RIFF/WAVE file (use a .ulaw/.raw file for headerless audio)")

    fmt_tag = channels = rate = bits = 0
    data = b""
    offset = 12
    while offset + 8 <= len(raw):
        chunk_id = raw[offset:offset + 4]
        size = int.from_bytes(raw[offset + 4:offset + 8], "little")
        body = raw[offset + 8:offset + 8 + size]
        if chunk_id == b"fmt " and len(body) >= 16:
            fmt_tag = int.from_bytes(body[0:2], "little")
            channels = int.from_bytes(body[2:4], "little")
            rate = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little")
        elif chunk_id == b"data":
            data = body
        offset += 8 + size + (size % 2)  # chunks are word-aligned

    if not fmt_tag:
        raise SystemExit(f"{path}: no fmt chunk")
    if not data:
        raise SystemExit(f"{path}: no data chunk")
    return data, fmt_tag, channels, rate, bits


def frames_from_file(path: str, mode: str) -> list[bytes]:
    """Load caller audio and slice it into 20 ms frames for the given profile.

    Nothing here resamples or transcodes, so anything that does not already match the
    profile exactly is refused rather than sent as garbage.
    """
    profile = PROFILES[mode]
    width, want_rate = profile["width"], profile["rate"]
    want_tag = WAVE_FORMAT_MULAW if mode == "mulaw" else WAVE_FORMAT_PCM

    if path.lower().endswith(HEADERLESS_SUFFIXES):
        # No header to check against, so the profile is taken on trust.
        raw, rate = Path(path).read_bytes(), want_rate
        print(f"[mock] {path}: {len(raw)} bytes, headerless -- assuming {mode} at {rate} Hz")
    else:
        raw, fmt_tag, channels, rate, bits = read_riff(path)
        if fmt_tag != want_tag:
            raise SystemExit(
                f"{path}: AUDIO_MODE={mode} needs {FORMAT_NAMES[want_tag]} audio, but this file is "
                f"{FORMAT_NAMES.get(fmt_tag, f'format {fmt_tag}')}. Convert it:\n"
                f"  ffmpeg -i {path} -ar {want_rate} -ac 1 "
                + ("-c:a pcm_mulaw out.wav" if mode == "mulaw" else "-c:a pcm_s16le out.wav")
            )
        if channels != 1:
            raise SystemExit(f"{path}: need mono, got {channels} channels")
        if bits != width * 8:
            raise SystemExit(f"{path}: need {width * 8}-bit samples, got {bits}-bit")
        if rate != want_rate:
            raise SystemExit(
                f"{path}: {rate} Hz, but AUDIO_MODE={mode} streams at {want_rate} Hz "
                f"and nothing here resamples"
            )
        print(f"[mock] {path}: {len(raw)} bytes, {FORMAT_NAMES[fmt_tag]} mono at {rate} Hz")

    size = want_rate * width * FRAME_MS // 1000
    return [raw[i:i + size] for i in range(0, len(raw), size)]


async def run(url: str, mode: str, wav: str | None, seconds: float) -> int:
    profile = PROFILES[mode]
    stream_id = "mock-stream-0001"
    call_id = "mock-call-0001"
    frame_bytes = profile["rate"] * profile["width"] * FRAME_MS // 1000

    if wav:
        frames = frames_from_file(wav, mode)
    else:
        silence = profile["silence"] * (frame_bytes // profile["width"])
        frames = [silence] * int(seconds * 1000 / FRAME_MS)

    played = Counter()
    played_bytes = 0
    checkpoints: list[str] = []
    cleared = 0
    first_audio_at: float | None = None

    closed_by_server: str | None = None

    async with websockets.connect(url) as ws:
        print(f"[mock] connected to {url}")

        async def reader():
            nonlocal played_bytes, cleared, first_audio_at
            async for raw in ws:
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "playAudio":
                    media = msg.get("media") or {}
                    played[(media.get("contentType"), media.get("sampleRate"))] += 1
                    played_bytes += len(base64.b64decode(media.get("payload", "")))
                    if first_audio_at is None:
                        first_audio_at = time.monotonic()
                        print(f"[mock] <- first playAudio {media.get('contentType')} "
                              f"@{media.get('sampleRate')}")
                elif event == "checkpoint":
                    name = msg.get("name", "")
                    checkpoints.append(name)
                    print(f"[mock] <- checkpoint {name} ({played_bytes} bytes queued)")
                    # Vobiz confirms a checkpoint once the queued audio has played out.
                    await ws.send(json.dumps(
                        {"event": "playedStream", "streamId": stream_id, "name": name}
                    ))
                elif event == "clearAudio":
                    cleared += 1
                    print("[mock] <- clearAudio (barge-in)")
                    await ws.send(json.dumps(
                        {"sequenceNumber": 0, "event": "clearedAudio", "streamId": stream_id}
                    ))

        async def read_until_closed():
            """Run the reader, but report a server-side close as a result, not a crash.

            The bridge closes this socket deliberately when it cannot reach Deepgram --
            a rejected API key, for instance. That is a legitimate outcome to test for,
            so it should read as a one-line diagnosis rather than a websockets traceback.
            """
            nonlocal closed_by_server
            try:
                await reader()
            except websockets.exceptions.ConnectionClosed as exc:
                # A 1000 here is just the server acknowledging our own `stop`, which is
                # how every healthy run ends. Only an abnormal code is worth reporting.
                code = exc.rcvd.code if exc.rcvd else None
                if code is not None and code != 1000:
                    closed_by_server = f"{code} {exc.rcvd.reason or ''}".strip()

        reader_task = asyncio.create_task(read_until_closed())

        async def send(payload: dict) -> bool:
            """Send, treating a socket the server has closed as a stop condition."""
            try:
                await ws.send(json.dumps(payload))
                return True
            except websockets.exceptions.ConnectionClosed:
                return False

        await ws.send(json.dumps({
            "sequenceNumber": 0,
            "event": "start",
            "start": {
                "callId": call_id,
                "streamId": stream_id,
                "auth_id": "mock",
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": profile["encoding"], "sampleRate": profile["rate"]},
            },
            # Real Vobiz always sends "{}" here — extraHeaders values never reach the socket.
            "extra_headers": "{}",
        }))
        started = time.monotonic()

        # Stream in real time — the agent's turn detection depends on the audio clock.
        for i, frame in enumerate(frames):
            sent = await send({
                "event": "media",
                "streamId": stream_id,
                "media": {
                    "payload": base64.b64encode(frame).decode(),
                    "contentType": profile["encoding"],
                    "sampleRate": profile["rate"],
                    "timestamp": int(time.time() * 1000),
                },
            })
            if not sent:
                break
            await asyncio.sleep(max(0, (i + 1) * FRAME_MS / 1000 - (time.monotonic() - started)))

        # Give the agent a moment to finish its reply before tearing down.
        await asyncio.sleep(3)
        await send({"event": "stop", "streamId": stream_id, "reason": "call_ended"})
        await asyncio.sleep(0.3)
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)

    print("\n--- result ---")
    print(f"  audio sent             {len(frames)} frames")
    print(f"  playAudio received     {sum(played.values())} frames / {played_bytes} bytes")
    print(f"  formats                {dict(played) or '(none)'}")
    print(f"  checkpoints            {len(checkpoints)} {checkpoints}")
    print(f"  barge-ins (clearAudio) {cleared}")
    if first_audio_at:
        print(f"  time to first audio    {first_audio_at - started:.2f}s")

    if closed_by_server:
        print(f"  closed by server       {closed_by_server}")

    if not played_bytes:
        if closed_by_server:
            print(f"\nFAIL — the bridge closed the socket ({closed_by_server}) instead of streaming "
                  f"audio.\nThat is what a rejected Deepgram key looks like; the app.py log has the "
                  f"reason.")
        else:
            print("\nFAIL — no playAudio came back. Check the app.py log.")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # STREAM_SECRET is a path segment on the media stream URL, so mirror what the XML builds.
    _secret = os.getenv("STREAM_SECRET", "")
    _default_url = (
        f"ws://127.0.0.1:{os.getenv('HTTP_PORT', '5050')}/media"
        + (f"/{_secret}" if _secret else "")
    )
    parser.add_argument("--url", default=_default_url)
    parser.add_argument("--mode", default=os.getenv("AUDIO_MODE", "mulaw"), choices=list(PROFILES))
    parser.add_argument("--wav", metavar="FILE",
                        help="mono audio at the profile's rate and encoding, streamed as the caller: "
                             "a WAV (mu-law for AUDIO_MODE=mulaw, 16-bit PCM for l16) "
                             "or a headerless .ulaw/.raw file")
    parser.add_argument("--seconds", type=float, default=4.0, help="silence to stream when no --wav")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.url, args.mode, args.wav, args.seconds)))
