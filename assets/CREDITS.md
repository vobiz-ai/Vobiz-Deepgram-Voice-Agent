# Asset credits

## office-ambience-8k.wav

Office ambience bed mixed under the agent's audio when `AMBIENCE=office`.

| | |
|---|---|
| Source | ["Busy Office No People Loop" by Fupicat](https://freesound.org/people/Fupicat/sounds/534123/) |
| Licence | **CC0 1.0 Universal (public domain dedication)** |
| Original | 24.1 s, 44.1 kHz stereo WAV |

CC0 in the author's own words: *"You can copy, modify, distribute and perform the
sound, even for commercial purposes, all without the need of asking permission to the
author."* No attribution is required; this file records it anyway so the provenance of
a binary in the repo is never a question.

Chosen over richer-sounding alternatives for two reasons. It is CC0 rather than
CC BY-NC, so it can ship in a commercial product. And it contains **no voices** —
background chatter would be more realistic for a call centre, but a caller can mistake
it for someone speaking to them, and it would be captured into any call recording.

### Processing applied

```
ffmpeg -i original.mp3 -ac 1 -ar 8000 \
       -af "highpass=f=250,lowpass=f=3400,dynaudnorm=f=200:g=5" \
       -c:a pcm_s16le office-ambience-8k.wav
```

Downmixed to mono, resampled to 8 kHz and band-limited to roughly the telephony
passband, so no bits are spent on frequencies the call discards.

`ambience.py` additionally cross-fades the last half-second into the first at load
time. The recording is sold as a loop but ends far quieter than it starts, so played
end-to-start it clicks audibly on every pass.
