"""Gujlish: the Gujarati transcript written in Latin letters, the way people type Gujarati in messages.

Words are transliterated one at a time, so every Gujlish word lines up with one Gujarati word and a fix made in
either view maps onto the other. Spellings come from, in order: your dictionary, the casual spellings of frequent
words below, then the engine. The engine is AI4Bharat's IndicXlit (MIT, trained on the Aksharantar word pairs) as a
CTranslate2 conversion that runs on the CPU in milliseconds and needs neither PyTorch nor Fairseq. Gemini is the
backup engine (--engine gemini).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import unicodedata
from pathlib import Path
from threading import Lock

from .storage import stage_paths
from .subtitles import write_srt
from .textutil import write_json

XLIT_REPO = "Singla0009/all-indic-transliteration"
XLIT_REVISION = "23279e441f195f6a45296fcee84ac89b541254ff"  # weights only (no code); pinned so spellings stay stable
TO_LATIN = "indicxlit_indic_en_ct2_fp32"
TO_GUJARATI = "indicxlit_ct2_fp32"
LANGUAGE_TAG = "__gu__"
BATCH_WORDS = 256
GEMINI_BATCH_WORDS = 200

GUJARATI_WORD = re.compile(r"[઀-૥૰-૿‌‍]+")  # letters and signs; digits excluded
GUJARATI_DIGITS = str.maketrans("૦૧૨૩૪૫૬૭૮૯", "0123456789")
LATIN_WORD = re.compile(r"[a-z]+")

# IndicXlit was trained on careful romanizations, so it writes "kareene" and "hatee", and on short words it can
# land on English ("curry" for કરી). Frequent words therefore get the casual spellings people actually type.
COMMON = {
    "હું": "hu", "મેં": "me", "મને": "mane", "મારું": "maru", "મારા": "mara", "મારી": "mari", "મારો": "maro",
    "અમે": "ame", "અમને": "amne", "અમારું": "amaru", "અમારા": "amara", "આપણે": "aapne", "આપણું": "aapnu",
    "આપણા": "aapna", "આપણી": "aapni", "આપણો": "aapno", "તું": "tu", "તને": "tane", "તારું": "taru", "તારા": "tara",
    "તમે": "tame", "તમને": "tamne", "તમારું": "tamaru", "તમારા": "tamara", "તમારી": "tamari", "એ": "e", "એને": "ene",
    "એનું": "enu", "એના": "ena", "એની": "eni", "એનો": "eno", "એમ": "em", "એમને": "emne", "એમના": "emna",
    "એમણે": "emne", "તે": "te", "તેને": "tene", "તેનું": "tenu", "તેના": "tena", "તેમ": "tem", "તેઓ": "teo",
    "આ": "aa", "આને": "aane", "જે": "je", "જેને": "jene", "જેમ": "jem", "કોણ": "kon", "કોઈ": "koi", "કંઈ": "kai",
    "કાંઈ": "kai", "કશું": "kashu", "બધા": "badha", "બધાં": "badha", "બધું": "badhu", "બધી": "badhi",
    "છે": "che", "છું": "chu", "છો": "cho", "છીએ": "chiye", "હતો": "hato", "હતી": "hati", "હતું": "hatu",
    "હતા": "hata", "હશે": "hashe", "હોય": "hoy", "થાય": "thay", "થઈ": "thai", "થયો": "thayo", "થયું": "thayu",
    "થયા": "thaya", "કરે": "kare", "કરો": "karo", "કરી": "kari", "કરીને": "karine", "કર્યું": "karyu",
    "કર્યો": "karyo", "કરવું": "karvu", "કરવાનું": "karvanu", "જાય": "jay", "જવું": "javu", "ગયો": "gayo",
    "ગયા": "gaya", "ગયું": "gayu", "આવે": "aave", "આવ્યો": "aavyo", "આવ્યા": "aavya", "આવ્યું": "aavyu",
    "લાગે": "lage", "જોઈએ": "joie", "જુઓ": "juo", "જો": "jo", "કહે": "kahe", "કહ્યું": "kahyu", "બોલો": "bolo",
    "રહે": "rahe", "નથી": "nathi", "નહીં": "nahi", "નહિ": "nahi", "ના": "na", "હા": "ha", "અને": "ane", "કે": "ke",
    "તો": "to", "પણ": "pan", "જ": "j", "પછી": "pachi", "ત્યારે": "tyare", "ત્યાં": "tya", "અહીં": "ahi",
    "ક્યાં": "kya", "કેમ": "kem", "શું": "shu", "શા": "sha", "માટે": "mate", "સાથે": "sathe", "વગર": "vagar",
    "ઉપર": "upar", "નીચે": "niche", "અંદર": "andar", "બહાર": "bahar", "હવે": "have", "પહેલા": "pehla",
    "પહેલાં": "pehla", "ખૂબ": "khub", "બહુ": "bahu", "બસ": "bas", "અરે": "are", "હજી": "haji", "હજુ": "haju",
    "ફરી": "fari", "એટલે": "etle", "એટલું": "etlu", "કેટલું": "ketlu", "કેટલા": "ketla", "કેવું": "kevu",
    "એવું": "evu", "આવું": "aavu", "જેવું": "jevu", "તેવું": "tevu", "સારું": "saru", "સરસ": "saras",
    "મોટું": "motu", "નાનું": "nanu", "થોડું": "thodu", "વધારે": "vadhare", "ઓછું": "ochu", "માં": "ma",
    "ની": "ni", "નો": "no", "નું": "nu", "ને": "ne", "થી": "thi", "દિવસ": "divas", "વાત": "vaat", "વખતે": "vakhte",
    "ભાઈ": "bhai", "બેન": "ben", "મા": "maa", "બાપા": "bapa", "જય": "jay", "ભગવાન": "Bhagwan",
    "સ્વામી": "Swami", "સ્વામીજી": "Swamiji", "મહારાજ": "Maharaj", "સત્સંગ": "satsang", "દર્શન": "darshan",
    "સેવા": "seva", "ભક્તિ": "bhakti", "બાળકો": "balko", "લોકો": "loko", "ઘર": "ghar", "કામ": "kaam",
    "સમય": "samay", "આજે": "aaje", "કાલે": "kaale", "રોજ": "roj", "જીવન": "jivan", "મન": "man", "પ્રેમ": "prem",
    "ગુરુ": "guru", "બંધ": "bandh",
}  # fmt: skip

GEMINI_SYSTEM = """You write Gujarati words in Latin letters the way Gujarati speakers casually type them in \
messages ("Gujlish"): "kem cho", "Maharaj", "Swamiji", "che", "pachi", "aapne", "vaat". Use simple everyday \
spellings: "i" rather than "ee", "u" rather than "oo", "ch" for both છ and ચ, and no accents or diacritics. \
The input is a JSON list of words. Reply with only a JSON object that maps every input word, exactly as given, \
to its spelling."""


def casual(spelling: str, word: str = "") -> str:
    """Bring IndicXlit's careful romanization closer to how people type: "kareene" → "karine", "aneey" → "ane"."""
    text = spelling.lower()
    if not word.endswith("ય"):  # a final "y" is real in આત્મીય ("aatmiy"), padding in અને ("aneey")
        text = re.sub(r"e+y$", "e", text)
    text = text.replace("ee", "i").replace("oo", "u").replace("chh", "ch")
    return re.sub(r"(.)\1\1+", r"\1\1", text)


def gujarati_words(text: str) -> list[str]:
    return GUJARATI_WORD.findall(unicodedata.normalize("NFC", text))


def latin_token(token: str, spellings: dict[str, str]) -> str:
    """One transcript token in Latin letters. Punctuation, digits and English words are kept as they are."""
    token = unicodedata.normalize("NFC", token).translate(GUJARATI_DIGITS)
    return GUJARATI_WORD.sub(lambda match: spellings.get(match.group(), match.group()), token)


def xlit_installed() -> bool:
    return importlib.util.find_spec("ctranslate2") is not None


class XlitEngine:
    name = "indicxlit"

    def __init__(self):
        self._models: dict[str, object] = {}
        self._lock = Lock()

    def _model(self, folder: str):
        with self._lock:
            if folder not in self._models:
                import ctranslate2
                from huggingface_hub import snapshot_download

                path = snapshot_download(XLIT_REPO, revision=XLIT_REVISION, allow_patterns=[f"{folder}/*"])
                self._models[folder] = ctranslate2.Translator(str(Path(path) / folder), device="cpu")
            return self._models[folder]

    def _run(self, folder: str, words: list[str], options: int = 1) -> list[list[str]]:
        found = []
        for start in range(0, len(words), BATCH_WORDS):
            batch = [[LANGUAGE_TAG, *word] for word in words[start : start + BATCH_WORDS]]
            results = self._model(folder).translate_batch(batch, beam_size=max(4, options), num_hypotheses=options)
            found += [["".join(hypothesis) for hypothesis in result.hypotheses] for result in results]
        return found

    def latin(self, words: list[str]) -> dict[str, str]:
        return {word: casual(options[0], word) for word, options in zip(words, self._run(TO_LATIN, words)) if options}

    def gujarati_options(self, word: str, count: int) -> list[str]:
        return list(dict.fromkeys(self._run(TO_GUJARATI, [word], count)[0]))


class GeminiEngine:
    name = "gemini"

    def __init__(self, llm=None):
        from .llm import GeminiLLM

        self.llm = llm or GeminiLLM()

    def latin(self, words: list[str]) -> dict[str, str]:
        from .llm import parse_json_object

        found = {}
        for start in range(0, len(words), GEMINI_BATCH_WORDS):
            batch = words[start : start + GEMINI_BATCH_WORDS]
            reply = self.llm.complete(GEMINI_SYSTEM, json.dumps(batch, ensure_ascii=False), max_tokens=16 * len(batch))
            for word, spelling in parse_json_object(reply.text).items():
                if word in batch and isinstance(spelling, str) and spelling.strip() and not gujarati_words(spelling):
                    found[word] = spelling.strip()
        return found

    def gujarati_options(self, word: str, count: int) -> list[str]:
        return []


class KnownWordsOnly:
    """Stand-in when no engine is installed: only your spellings and the common words get Latin letters."""

    name = "known-words"

    def latin(self, words: list[str]) -> dict[str, str]:
        return {}

    def gujarati_options(self, word: str, count: int) -> list[str]:
        return []


def choose_engine(name: str = "auto"):
    if name == "gemini":
        return GeminiEngine()
    if name == "indicxlit" or xlit_installed():
        return XlitEngine()
    from .llm import gemini_available

    if gemini_available():
        return GeminiEngine()
    raise RuntimeError("Gujlish needs the ctranslate2 package (pip install -r requirements.txt) or a Gemini key.")


class Transliterator:
    def __init__(self, engine=None, overrides: dict[str, str] | None = None, fallback=None):
        self.engine = engine or choose_engine()
        self.overrides = overrides or {}  # your spellings: Gujarati word → Gujlish
        self.fallback = fallback  # tried for words the engine left out (Gemini can skip a few)

    def _known(self, word: str) -> str | None:
        return self.overrides.get(word) or COMMON.get(word)

    def spellings(self, words) -> dict[str, str]:
        found, missing = {}, []
        for word in dict.fromkeys(words):
            known = self._known(word)
            if known:
                found[word] = known
            else:
                missing.append(word)
        if missing:
            found.update(self.engine.latin(missing))
        missing = [word for word in missing if word not in found]
        if missing and self.fallback is not None:
            found.update(self.fallback.latin(missing))
        return found

    def latin_text(self, text: str) -> str:
        spellings = self.spellings(gujarati_words(text))
        return " ".join(latin_token(token, spellings) for token in text.split())

    def gujarati_options(self, text: str, count: int = 4) -> list[str]:
        """Gujarati spellings for Gujlish typed in the editor: your own words first, then the engine's guesses."""
        words = LATIN_WORD.findall(text.lower())
        if not words:
            return []
        known: dict[str, list[str]] = {}
        for gujarati, latin in [*COMMON.items(), *self.overrides.items()]:
            known.setdefault(latin.lower(), []).insert(0, gujarati)
        if len(words) == 1:
            options = known.get(words[0], []) + self.engine.gujarati_options(words[0], count)
            return list(dict.fromkeys(options))[:count]
        pieces = []
        for word in words:
            choices = known.get(word) or self.engine.gujarati_options(word, 1)
            pieces.append(choices[0] if choices else word)
        return [" ".join(pieces)]


def gujlish_segments(segments: list[dict], transliterator: Transliterator) -> list[list[str]]:
    """One list of Gujlish words per transcript segment, matching its Gujarati words one to one."""
    spellings = transliterator.spellings(word for segment in segments for word in gujarati_words(segment["text"]))
    return [[latin_token(token, spellings) for token in segment.get("text", "").split()] for segment in segments]


def write_gujlish(transcript_json: Path, data: dict, tokens: list[list[str]], engine: str) -> dict[str, Path]:
    paths = stage_paths(Path(transcript_json))
    segments = [{"start": s["start"], "end": s["end"], "tokens": t} for s, t in zip(data["segments"], tokens)]
    write_json(
        paths["gujlish_json"], {"kind": "gujlish", "source": data.get("source"), "engine": engine, "segments": segments}
    )
    texts = [" ".join(words) for words in tokens]
    paths["gujlish_txt"].write_text(" ".join(text for text in texts if text) + "\n", encoding="utf-8")
    write_srt(paths["gujlish_srt"], [{**segment, "text": text} for segment, text in zip(segments, texts)])
    return {key: paths[key] for key in ("gujlish_txt", "gujlish_srt", "gujlish_json")}


def run_gujlish(transcript_json: Path, transliterator: Transliterator, progress=None) -> dict[str, Path]:
    data = json.loads(Path(transcript_json).read_text(encoding="utf-8"))
    if progress:
        progress({"stage": "transliterating", "done": 0, "total": 1})
    tokens = gujlish_segments(data.get("segments", []), transliterator)
    paths = write_gujlish(transcript_json, data, tokens, transliterator.engine.name)
    if progress:
        progress({"stage": "transliterating", "done": 1, "total": 1})
    return paths


def main() -> None:
    from .dictionary import load
    from .enrich import STAGE_ERRORS, resolve_transcript

    parser = argparse.ArgumentParser(description="Write a finished transcript in Gujlish (Gujarati in Latin letters)")
    parser.add_argument(
        "transcript", type=Path, help="The <name>.<model>.json file of a transcription run, or its folder"
    )
    parser.add_argument("--engine", choices=["auto", "indicxlit", "gemini"], default="auto")
    parser.add_argument("--dictionary", type=Path, help="Correction dictionary whose Gujlish spellings take priority")
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        overrides = load(args.dictionary)["gujlish"] if args.dictionary else {}
        engine = choose_engine(args.engine)
        fallback = XlitEngine() if engine.name == "gemini" and xlit_installed() else None
        progress = (lambda event: print("ASR_EVENT " + json.dumps(event), flush=True)) if args.progress_json else None
        paths = run_gujlish(resolve_transcript(args.transcript), Transliterator(engine, overrides, fallback), progress)
    except STAGE_ERRORS as error:
        parser.exit(1, f"Error: {error}\n")
    print("Done:")
    for path in paths.values():
        print(f"  {path}")
