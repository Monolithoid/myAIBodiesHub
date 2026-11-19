"""
wakeword.py — Local wake word ("ok nao") detection with openWakeWord.

Used by static_drone.py when WAKE_MODE=local: the drone listens continuously,
gives each audio block to the detector and only sends the VPS the sentence that
follows the wake word (see README, "Ok nao": on the drone or on the VPS).

openWakeWord works on 80 ms blocks (1280 samples) at 16 kHz: the detector
resamples and re-chunks the microphone blocks. openwakeword is not imported when
the module loads: the tests run without it.
"""

import logging
import os
import time
from math import gcd

import numpy as np

log = logging.getLogger("static_drone")

MODEL_RATE = 16000
CHUNK = 1280            # 80 ms at 16 kHz


def resolve_model(wanted, models_dir):
    """Path of the model to load, or name of a model provided by openWakeWord.

    - an existing path (.onnx) → as is;
    - a name (e.g. "ok_nao") for which models_dir/<name>.onnx exists → that file;
    - otherwise the name as is: openWakeWord looks for it among its pre-trained
      models (alexa, hey_jarvis, hey_mycroft…).
    """
    wanted = (wanted or "").strip()
    if os.path.isfile(wanted):
        return wanted
    path = os.path.join(models_dir, wanted + ".onnx")
    if os.path.isfile(path):
        return path
    return wanted


class Resampler:
    """int16 mono at `rate` Hz → int16 mono at `target` Hz, block by block.

    Resampling each 30 ms block separately would leave a glitch at every
    joint: a little audio is kept on each side (resample_poly
    filter), at the cost of a few milliseconds of delay.
    """

    def __init__(self, rate, target=MODEL_RATE):
        g = gcd(int(rate), int(target))
        self.up, self.down = int(target) // g, int(rate) // g
        # Half-length of the resample_poly filter, in input samples,
        # rounded to a multiple of `down` so that the outputs line up.
        half = -(-10 * max(self.up, self.down) // self.up) + 1
        self.margin = -(-half // self.down) * self.down
        self._buf = np.zeros(self.margin, dtype=np.float32)    # contexte gauche : silence

    def __call__(self, pcm):
        pcm = np.asarray(pcm, dtype=np.int16)
        if self.up == self.down:
            return pcm
        from scipy.signal import resample_poly     # openWakeWord dependency
        self._buf = np.concatenate([self._buf, pcm.astype(np.float32)])
        n = (len(self._buf) - 2 * self.margin) // self.down * self.down
        if n <= 0:
            return np.zeros(0, dtype=np.int16)
        out = resample_poly(self._buf, self.up, self.down)
        start = self.margin * self.up // self.down
        out = out[start:start + n * self.up // self.down]
        self._buf = self._buf[n:]
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def load_model(model, models_dir):
    """Loads an openWakeWord model (ONNX, onnxruntime); returns (Model, name)."""
    import openwakeword.utils
    from openwakeword.model import Model

    path = resolve_model(model, models_dir)
    name = os.path.splitext(os.path.basename(path))[0]
    if not os.path.isfile(path) and name not in openwakeword.MODELS:
        raise RuntimeError(
            "Modèle de réveil {!r} introuvable : ni fichier, ni {}/{}.onnx, ni modèle "
            "openWakeWord ({}). Voir models/README.md pour entraîner « ok nao ».".format(
                model, models_dir, model, ", ".join(openwakeword.MODELS)))
    # Shared models (mel + embedding) and the pre-trained model if any:
    # downloaded once into the openwakeword package, nothing if already present.
    # (An empty list would download all the pre-trained models.)
    try:
        openwakeword.utils.download_models([name])
    except Exception as exc:
        log.warning("Unable to download the openWakeWord models: %s", exc)
    return Model(wakeword_models=[path], inference_framework="onnx"), name


class WakeWordDetector:
    """Scores each audio block; reports the wake word above the threshold.

    `model` follows the openwakeword.model.Model interface: predict(int16[1280])
    → {name: score}, reset().
    """

    def __init__(self, model, name, threshold, rate, debug_every_s=1.0):
        self.model = model
        self.name = name
        self.threshold = threshold
        self.rate = rate
        self.debug_every_s = debug_every_s
        self._resample = Resampler(rate)
        self._buf = np.zeros(0, dtype=np.int16)
        self._peak = 0.0
        self._logged = time.monotonic()

    def process(self, mono):
        """int16 mono block at `rate` Hz → score if the wake word is heard, otherwise None."""
        self._buf = np.concatenate([self._buf, self._resample(mono)])
        best = 0.0
        while len(self._buf) >= CHUNK:
            chunk, self._buf = self._buf[:CHUNK], self._buf[CHUNK:]
            scores = self.model.predict(chunk)
            best = max(best, max((float(s) for s in scores.values()), default=0.0))
        self._debug(best)
        return best if best >= self.threshold else None

    def reset(self):
        """Empties the buffers: avoids a second trigger on the same "ok nao"."""
        self._buf = np.zeros(0, dtype=np.int16)
        self.model.reset()

    def _debug(self, score):
        # In DEBUG, the max score of each second helps tune WAKE_THRESHOLD.
        self._peak = max(self._peak, score)
        if time.monotonic() - self._logged >= self.debug_every_s:
            log.debug("Max score for \"%s\" over 1 s: %.2f (threshold %.2f)",
                      self.name, self._peak, self.threshold)
            self._peak, self._logged = 0.0, time.monotonic()
