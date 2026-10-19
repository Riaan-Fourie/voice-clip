"""Room check for the Auto mic choice (Jarvis #767).

After each dictation, score how far the voice rose above the room noise on the
MacBook mic, and pick the mic for the NEXT dictation:

  * quiet room  -> MacBook mic (full-band audio, transcribes best)
  * noisy room  -> AirPods (mic at the mouth, noise suppressed on the bud;
                   narrowband HFP, but far better than a drowned-out Mac mic)

The MacBook mic is always the probe. Opening it has no Bluetooth side effect,
whereas opening the AirPods mic forces the A2DP -> HFP flip (#269), so the
AirPods are never opened just to measure.

Measured on 2026-10-19 (noisy room, same sentence on both mics at once): the
Mac mic scored 4.1 dB and whisper-small.en got 100% of the words wrong; the
AirPods got 28% wrong. A known sentence mixed into that real room noise was
transcribed cleanly at 13 dB and started to fail around 9 dB. The two
thresholds below sit either side of that gap, so the choice does not flap
between dictations in a borderline room.
"""

import numpy as np

from recorder import MIC_AIRPODS, MIC_MACBOOK, SAMPLE_RATE

FRAME = 800  # 50 ms at 16 kHz
NOISE_PERCENTILE = 20  # quietest frames: the gaps between words
VOICE_PERCENTILE = 95  # loudest frames: the voice
MIN_SECONDS = 1.0  # shorter clips have too few gaps to measure the noise

NOISY_BELOW_DB = 10.0  # Mac mic below this -> AirPods for the next dictation
QUIET_ABOVE_DB = 15.0  # Mac mic above this -> MacBook mic again


def voice_to_noise_db(audio):
    """Score a mono int16 clip: loud frames over quiet frames, in dB.

    Returns None when the clip is too short to tell voice from noise."""
    n = len(audio) // FRAME
    if n * FRAME < MIN_SECONDS * SAMPLE_RATE:
        return None
    frames = audio[: n * FRAME].astype(np.float32).reshape(n, FRAME)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    noise = float(np.percentile(rms, NOISE_PERCENTILE))
    voice = float(np.percentile(rms, VOICE_PERCENTILE))
    return 20.0 * np.log10((voice + 1.0) / (noise + 1.0))


def next_choice(current, score_db):
    """The mic for the next dictation, given the current one and the score.

    Between the two thresholds the current choice stays."""
    if score_db is None:
        return current
    if score_db < NOISY_BELOW_DB:
        return MIC_AIRPODS
    if score_db > QUIET_ABOVE_DB:
        return MIC_MACBOOK
    return current
