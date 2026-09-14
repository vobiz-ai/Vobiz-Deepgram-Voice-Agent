"""
Stand in for Vobiz so the bridge can be tested without placing a call.

Speaks the Vobiz side of the media-stream protocol against a running app.py: opens the WebSocket,
sends `start`, streams audio in as `media` frames, collects the `playAudio` frames that come back,
answers `checkpoint` with `playedStream`, notes `clearAudio`, then sends `stop`.

    python app.py                                  # terminal 1
    python mock_vobiz.py                           # terminal 2 — streams silence
    python mock_vobiz.py --wav question.wav        # stream a real question at the agent

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
import wave
from collections import Counter

import websockets
from dotenv import load_dotenv

load_dotenv()

PROFILES = {
    "mulaw": {"encoding": "audio/x-mulaw", "rate": 8000, "width": 1, "silence": b"\xff"},
    "l16": {"encoding": "audio/x-l16", "rate": 16000, "width": 2, "silence": b"\x00\x00"},
}
FRAME_MS = 20


def frames_from_wav(path: str, width: int) -> list[bytes]:
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != width or w.getnchannels() != 1:
            raise SystemExit(
                f"{path}: need mono {width * 8}-bit audio, got "
                f"{w.getnchannels()}ch/{w.getsampwidth() * 8}-bit"
            )
        raw = w.readframes(w.getnframes())
        rate = w.getframerate()
    size = rate * width * FRAME_MS // 1000
    print(f"[mock] {path}: {len(raw)} bytes at {rate} Hz")
    return [raw[i:i + size] for i in range(0, len(raw), size)]


async def run(url: str, mode: str, wav: str | None, seconds: float) -> int:
    profile = PROFILES[mode]
    stream_id = "mock-stream-0001"
    call_id = "mock-call-0001"
    frame_bytes = profile["rate"] * profile["width"] * FRAME_MS // 1000

    if wav:
        frames = frames_from_wav(wav, profile["width"])
    else:
        silence = profile["silence"] * (frame_bytes // profile["width"])
        frames = [silence] * int(seconds * 1000 / FRAME_MS)

    played = Counter()
    played_bytes = 0
    checkpoints: list[str] = []
    cleared = 0
    first_audio_at: float | None = None

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

        reader_task = asyncio.create_task(reader())

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
            await ws.send(json.dumps({
                "event": "media",
                "streamId": stream_id,
                "media": {
                    "payload": base64.b64encode(frame).decode(),
                    "contentType": profile["encoding"],
                    "sampleRate": profile["rate"],
                    "timestamp": int(time.time() * 1000),
                },
            }))
            await asyncio.sleep(max(0, (i + 1) * FRAME_MS / 1000 - (time.monotonic() - started)))

        # Give the agent a moment to finish its reply before tearing down.
        await asyncio.sleep(3)
        await ws.send(json.dumps(
            {"event": "stop", "streamId": stream_id, "reason": "call_ended"}
        ))
        await asyncio.sleep(0.3)
        reader_task.cancel()

    print("\n--- result ---")
    print(f"  audio sent             {len(frames)} frames")
    print(f"  playAudio received     {sum(played.values())} frames / {played_bytes} bytes")
    print(f"  formats                {dict(played) or '(none)'}")
    print(f"  checkpoints            {len(checkpoints)} {checkpoints}")
    print(f"  barge-ins (clearAudio) {cleared}")
    if first_audio_at:
        print(f"  time to first audio    {first_audio_at - started:.2f}s")

    if not played_bytes:
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
    parser.add_argument("--wav", help="mono WAV at the profile's sample rate, streamed as the caller")
    parser.add_argument("--seconds", type=float, default=4.0, help="silence to stream when no --wav")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.url, args.mode, args.wav, args.seconds)))
