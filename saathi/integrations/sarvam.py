"""sarvam.py - Sarvam Saaras v3 speech-to-text (live) + mock.

Live: two calls to POST https://api.sarvam.ai/speech-to-text with model=saaras:v3:
  mode=codemix   -> original-language transcript for display
  mode=translate -> English copy for the system
Mock (no SARVAM_API_KEY): maps the scenario's clip to its seeded transcript, logged "(mock)".
TTS: Saathi's replies are spoken with Sarvam Bulbul v3 (live) or the browser's speech engine (mock).

Production: Sarvam Saaras (streaming) or a self-hosted Indic ASR model.
"""
import os
import time

from saathi.core import logger
from saathi.sim.seed import LANGUAGES, SCENARIOS

URL = "https://api.sarvam.ai/speech-to-text"
MODEL = "saaras:v3"


def is_live():
    return bool(os.environ.get("SARVAM_API_KEY"))


def _call(audio, filename, mime, mode):
    import requests
    t0 = time.perf_counter()
    r = requests.post(URL, headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                      files={"file": (filename or "clip.webm", audio, mime or "audio/webm")},
                      data={"model": MODEL, "mode": mode}, timeout=60)
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    j = r.json()
    return j.get("transcript", ""), j.get("language_code"), ms


def transcribe(audio, filename=None, mime=None, scenario_id=None, merchant_id=None, lang=None):
    """Returns {text_original, text_english, language_code, live} or None if it cannot transcribe."""
    if is_live() and audio:
        try:
            orig, lang, ms = _call(audio, filename, mime, "codemix")
            logger.log("sarvam", f"{MODEL} mode=codemix → {lang} ({ms:.0f} ms) · {len(audio) // 1024} KB {(mime or 'audio/webm').split('/')[-1]}")
            eng, _, ms2 = _call(audio, filename, mime, "translate")
            logger.log("sarvam", f'mode=translate → "{eng}" ({ms2:.0f} ms)')
            return {"text_original": orig, "text_english": eng, "language_code": lang, "live": True}
        except Exception as e:
            logger.log("sarvam", f"live call failed ({e.__class__.__name__}: {str(e)[:80]}) → falling back to mock", level="warn")
    t0 = time.perf_counter()
    sc = SCENARIOS.get(scenario_id) or next((s for s in SCENARIOS.values() if s["merchant_id"] == merchant_id), None)
    if not sc:
        logger.log("sarvam", "no seeded transcript for this clip (mock) → ask merchant to type", level="warn")
        return None
    lang = lang if lang in sc["texts"] else sc["lang"]
    code = LANGUAGES[lang]["code"]
    ms = (time.perf_counter() - t0) * 1000
    size = f" · {len(audio) // 1024} KB clip" if audio else ""
    logger.log("sarvam", f"{MODEL} mode=codemix → {code} ({ms:.1f} ms){size} (mock)")
    logger.log("sarvam", f'mode=translate → "{sc["text_en"]}" (mock)')
    return {"text_original": sc["texts"][lang], "text_english": sc["text_en"], "language_code": code, "live": False}


# ------------------------------------------------------------------ text-to-speech (Saathi's replies)
TTS_URL = "https://api.sarvam.ai/text-to-speech"
TTS_MODEL, TTS_SPEAKER = "bulbul:v3", "kavya"
VOICES = {"kavya": "Kavya (female)", "shreya": "Shreya (female)", "ritu": "Ritu (female)", "priya": "Priya (female)",
          "rahul": "Rahul (male)", "aditya": "Aditya (male)", "amit": "Amit (male)"}


def tts(text, lang, speaker=None):
    """Returns base64 WAV from Sarvam Bulbul (live), or None -> the browser speaks it instead."""
    speaker = speaker if speaker in VOICES else TTS_SPEAKER
    code = LANGUAGES.get(lang, LANGUAGES["en"])["code"]
    if not is_live():
        logger.log("sarvam", f"tts {code} {len(text)} chars → browser speech synthesis (mock)")
        return None
    import requests
    t0 = time.perf_counter()
    try:
        r = requests.post(TTS_URL, headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                          json={"text": text[:1500], "target_language_code": code, "speaker": speaker, "model": TTS_MODEL,
                                "speech_sample_rate": 24000, "pace": 1.25},
                          timeout=60)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        audio = r.json()["audios"][0]
        logger.log("sarvam", f"tts {TTS_MODEL} voice={speaker} {code} {len(text)} chars → {len(audio) * 3 // 4 // 1024} KB wav "
                             f"({(time.perf_counter() - t0) * 1000:.0f} ms)")
        return audio
    except Exception as e:
        logger.log("sarvam", f"tts failed ({e.__class__.__name__}: {str(e)[:80]}) → browser speech", level="warn")
        return None
