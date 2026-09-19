"""
Background ambience for the outbound leg.

Deepgram has no ambient-sound feature -- it returns clean synthesized speech and
nothing else -- so if the agent is to sound like it is sitting in a room rather than
a vacuum, the room has to be mixed in here, on the audio going back to Vobiz.

The ambience is generated rather than sampled. A recorded loop would sound better in
a studio, but the outbound leg is 8 kHz mu-law with a 300-3400 Hz passband, which
discards most of what makes a real recording good and none of what makes it a
licensing problem. Synthesis keeps the repo asset-free and the loop seamless.

Two pieces, both deliberately understated:

  room tone   low-passed noise, the hum of an occupied office
  keystrokes  short bursts at irregular intervals, the sound that actually reads as
              "a person at a desk" rather than "a hiss on the line"

Mixing happens in the linear domain. Mu-law is logarithmic, so samples cannot simply
be added -- they are decoded to 16-bit linear, summed with headroom, and re-encoded.
The codec below is table-driven and self-contained because `audioop`, the obvious
tool, is deprecated in 3.11 and removed in 3.13; depending on it would leave a fault
waiting for the next base-image bump.
"""

from __future__ import annotations

import bisect as _bisect
import os as _os
import wave as _wave
import math
import random
import struct

# --- mu-law codec (G.711), tables built once at import -------------------------
_BIAS = 0x84


def _ulaw_to_linear(byte: int) -> int:
    """G.711 mu-law expansion, the Sun reference formulation."""
    byte = ~byte & 0xFF
    sign, exponent, mantissa = byte & 0x80, (byte >> 4) & 0x07, byte & 0x0F
    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -magnitude if sign else magnitude


_ULAW_TO_LIN = [_ulaw_to_linear(b) for b in range(256)]

# The encoder is built by inverting the decoder rather than by re-deriving G.711's
# compression algorithm. There are two formulations of that algorithm in the wild --
# one operating on 16-bit samples, one on 14 -- and mixing them silently produces
# audio that decodes to garbage. Inverting the table cannot disagree with the
# decoder, because it is defined in terms of it.
_SORTED = sorted((v, b) for b, v in enumerate(_ULAW_TO_LIN))
_SORTED_VALUES = [v for v, _ in _SORTED]


def _nearest_ulaw(value: int) -> int:
    i = _bisect.bisect_left(_SORTED_VALUES, value)
    if i == 0:
        return _SORTED[0][1]
    if i >= len(_SORTED):
        return _SORTED[-1][1]
    lo_v, lo_b = _SORTED[i - 1]
    hi_v, hi_b = _SORTED[i]
    return lo_b if value - lo_v <= hi_v - value else hi_b


# Quantise to 14 bits before lookup: mu-law carries no more resolution than that,
# so the table stays at 16384 entries and nothing audible is lost.
_LIN_TO_ULAW = [_nearest_ulaw(v << 2) for v in range(-8192, 8192)]

# Mu-law encodes zero twice, as 0x7F and 0xFF, so one of the two cannot survive a
# byte round-trip whichever the encoder picks. Prefer 0xFF: it is the conventional
# silence byte on a telephony leg, and some equipment treats it as such.
_LIN_TO_ULAW[8192] = 0xFF


def ulaw_decode(data: bytes) -> list[int]:
    return [_ULAW_TO_LIN[b] for b in data]


def ulaw_encode(samples: list[int]) -> bytes:
    out = bytearray(len(samples))
    for i, s in enumerate(samples):
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        out[i] = _LIN_TO_ULAW[(s >> 2) + 8192]
    return bytes(out)


# --- the ambience itself -------------------------------------------------------
DEFAULT_ASSET = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "assets", "office-ambience-8k.wav")


def _load_wav(path: str) -> tuple[list[int], int]:
    """Read a mono 16-bit WAV into linear samples."""
    with _wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected mono 16-bit, got "
                             f"{w.getnchannels()}ch/{w.getsampwidth()*8}-bit")
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    return list(struct.unpack(f"<{len(raw)//2}h", raw)), rate


def _resample(samples: list[int], src: int, dst: int) -> list[int]:
    """Linear resample. Crude, but this is a background bed well below the voice."""
    if src == dst:
        return samples
    n = int(len(samples) * dst / src)
    out = [0] * n
    for i in range(n):
        pos = i * src / dst
        a = int(pos)
        b = min(a + 1, len(samples) - 1)
        frac = pos - a
        out[i] = int(samples[a] * (1 - frac) + samples[b] * frac)
    return out


def _seamless(buf: list[int], fade: int) -> list[int]:
    """Cross-fade the tail into the head so the loop point cannot be heard.

    Worth doing even on a recording sold as a loop: this one ends far quieter than
    it begins, so played end-to-start it clicks on every pass.
    """
    fade = min(fade, len(buf) // 4)
    if fade <= 0:
        return buf
    out = list(buf)
    for k in range(fade):
        w = k / fade
        out[k] = int(buf[k] * w + buf[len(buf) - fade + k] * (1.0 - w))
    return out[: len(buf) - fade]


class Ambience:
    """A seamless loop of office sound, served 20 ms at a time.

    Prefers the recording in assets/ -- a real room has an irregularity that is hard
    to fake -- and falls back to synthesis when the file is absent, so the module
    still works in a checkout without the asset.
    """

    def __init__(self, sample_rate: int, level: float = 0.08,
                 asset: str | None = DEFAULT_ASSET) -> None:
        self.sample_rate = sample_rate
        self.level = max(0.0, min(1.0, level))
        self._cursor = 0
        if asset and _os.path.exists(asset):
            samples, rate = _load_wav(asset)
            self.source = f"{_os.path.basename(asset)} @ {rate}Hz"
            self._loop = _seamless(_resample(samples, rate, sample_rate), sample_rate // 2)
        else:
            self.source = "synthesised"
            self._loop = self._render(30)

    def _render(self, seconds: int) -> list[int]:
        rng = random.Random(0xB0FF1CE)  # fixed seed: same room every call
        n = self.sample_rate * seconds
        buf = [0] * n

        # Room tone: white noise through a one-pole low-pass, which on a telephony
        # band reads as the dull presence of an occupied room rather than hiss.
        prev = 0.0
        alpha = 0.06
        for i in range(n):
            prev += alpha * (rng.uniform(-1.0, 1.0) - prev)
            buf[i] = int(prev * 2600)

        # Keystrokes: a short noise burst with a fast decay. Irregular spacing is what
        # sells it -- evenly spaced clicks read as a machine fault.
        i = 0
        while i < n:
            i += int(rng.uniform(0.18, 1.5) * self.sample_rate)
            if i >= n:
                break
            burst = int(0.012 * self.sample_rate)
            peak = rng.uniform(0.5, 1.0)
            for k in range(burst):
                if i + k >= n:
                    break
                decay = math.exp(-6.0 * k / burst)
                buf[i + k] += int(rng.uniform(-1.0, 1.0) * 5200 * peak * decay)

        # Cross-fade the tail into the head so the loop point is inaudible.
        fade = min(self.sample_rate // 2, n // 4)
        for k in range(fade):
            w = k / fade
            buf[k] = int(buf[k] * w + buf[n - fade + k] * (1.0 - w))
        return buf[: n - fade]

    def take(self, count: int) -> list[int]:
        """The next `count` samples, scaled to the configured level."""
        out = []
        loop = self._loop
        size = len(loop)
        pos = self._cursor
        for _ in range(count):
            out.append(int(loop[pos] * self.level))
            pos += 1
            if pos >= size:
                pos = 0
        self._cursor = pos
        return out

    # --- mixing -----------------------------------------------------------------
    def under_mulaw(self, frame: bytes) -> bytes:
        """Mix ambience under one mu-law frame."""
        speech = ulaw_decode(frame)
        bed = self.take(len(speech))
        # Duck the bed slightly under speech so the voice stays in front.
        return ulaw_encode([int(s * 0.92) + b for s, b in zip(speech, bed)])

    def under_linear16(self, frame: bytes) -> bytes:
        """Mix ambience under one little-endian 16-bit PCM frame."""
        count = len(frame) // 2
        speech = struct.unpack(f"<{count}h", frame[: count * 2])
        bed = self.take(count)
        mixed = []
        for s, b in zip(speech, bed):
            v = int(s * 0.92) + b
            mixed.append(32767 if v > 32767 else -32768 if v < -32768 else v)
        return struct.pack(f"<{count}h", *mixed)
