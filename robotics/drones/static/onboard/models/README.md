# "ok nao" model (openWakeWord)

`WAKE_MODE=local` loads `models/ok_nao.onnx` (`WAKE_MODEL=ok_nao`). This file is not
in the repository yet: it has to be trained once with this folder, then copied here
(and committed, it weighs ~200 KB).

| File | Role |
|---|---|
| `train_ok_nao.ipynb` | Colab notebook: the whole training, from start to the `.onnx` |
| `generate_clips_fr.py` | French synthetic voices (edge-tts + Piper), positive and negative |
| `ok_nao.yml` | `openwakeword.train` configuration |

## In the meantime: try with an existing model

openWakeWord provides English models. To test the whole chain right away, in the
drone's `.env`:

```
WAKE_MODE=local
WAKE_MODEL=hey_jarvis
```

and say "hey Jarvis" (or `alexa`, `hey_mycroft`). The model is downloaded on the first
start. body-gateway does not look at the word: it receives the sentence that follows,
unfiltered.

## Training "ok nao"

The simplest: open `train_ok_nao.ipynb` in
[Google Colab](https://colab.research.google.com/) (*File > Upload notebook > GitHub*,
repository `Monolithoid/ia-agent`), choose a T4 GPU and run everything. Allow 1 to 2
hours. The last cell downloads `ok_nao.onnx`.

What the notebook does, to redo it elsewhere (Linux PC with a GPU):

1. **French voices**: openWakeWord's generator only speaks English,
   `generate_clips_fr.py` replaces it:

   ```bash
   pip install edge-tts piper-tts scipy numpy     # + ffmpeg (apt)
   python generate_clips_fr.py --out sortie/ok_nao --n-train 3000 --n-test 300
   ```

   - **positives**: "ok nao" written several ways (« OK Nao. », « Ok, Nao ! »,
     « okay nao »…);
   - **negatives**: close words (« ok », « nao », « le Nao », « ok non », « cacao »,
     « chaos », « ok Google »…) and common sentences: the model learns not to
     trigger on them;
   - **voices**: all edge-tts fr-* voices (France, Canada, Belgium, Switzerland) with
     random rate and pitch, and the French Piper voices (including
     `fr_FR-mls-medium` and its 125 speakers) with varied length and noise. The test
     clips come from voices absent from training. `--no-edge` to work offline (Piper
     only).

2. **Augmentation**: reverberation (MIT impulse responses), background noise
   (AudioSet, FMA music), then feature computation:
   `python -m openwakeword.train --training_config ok_nao.yml --augment_clips`.
   `train.py` always imports `generate_samples`: an empty module in `./stub` is
   enough (never pass `--generate_clips`).

3. **Training**: `--train_model`, with ~2000 h of precomputed negatives (ACAV100M) and
   ~11 h of false-trigger validation, provided by openWakeWord on Hugging Face.
   Output: `sortie/ok_nao.onnx`. The `.tflite` conversion that follows may fail
   without TensorFlow: the drone only uses the ONNX.

## Improving the model

- **Too many spurious wake-ups**: raise `WAKE_THRESHOLD` on the drone (0.6, 0.7…);
  for training, lower `target_false_positives_per_hour` or add the offending words to
  `NEGATIVE` in `generate_clips_fr.py`.
- **"ok nao" missed**: look at the score with `LOG_LEVEL=DEBUG` (line
  `Max score for "ok_nao" over 1 s`) and lower `WAKE_THRESHOLD`; for training, more
  clips (`--n-train 6000`) or `layer_size: 64`.
- **Real voices**: a few dozen recordings of "ok nao" by the people in the house, in
  the room, as 16 kHz mono WAV, added to `sortie/ok_nao/positive_train` before
  `--augment_clips`, help a lot.
