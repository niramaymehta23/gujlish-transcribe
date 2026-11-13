"""Search words and spelling keys.

A talk is searchable in Gujarati script, in Gujlish (Gujarati typed in Latin letters) and in English, and people
spell the same word many ways: કરીને, karine, kareene. Every word therefore also gets a *spelling key*: Gujarati is
written in plain Latin letters, then Gujarati and Latin spellings are folded alike (aspiration, vowel length, sh/s,
w/v, z/j, ph/f, a nasal before b/p, short a, doubled letters), so all of those reach "krine". Keys four letters or
longer also match keys one edit away (a typo), checked with `similar()`.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Gujarati vowel signs, virama, anusvara, nukta and other combining signs, plus the zero-width (non-)joiners. Python's
# \w and SQLite's unicode61 tokenizer both split words at these, so સ્વામીજી would become સ, વ, મ, જ.
GUJARATI_SIGNS = "".join(chr(code) for code in [0xA81, 0xA82, 0xA83, 0xABC, *range(0xABE, 0xACE), 0xAE2, 0xAE3])
GUJARATI_SIGNS += "".join(chr(code) for code in range(0xAFA, 0xB00))
TOKEN_CHARS = GUJARATI_SIGNS + "‌‍"
WORD = re.compile(rf"(?:[^\W_]|[{TOKEN_CHARS}])+")
GUJARATI = re.compile(r"[઀-૿]")
GUJARATI_DIGITS = str.maketrans("૦૧૨૩૪૫૬૭૮૯", "0123456789")

CONSONANTS = {
    "ક": "k", "ખ": "kh", "ગ": "g", "ઘ": "gh", "ઙ": "n", "ચ": "ch", "છ": "chh", "જ": "j", "ઝ": "jh", "ઞ": "n",
    "ટ": "t", "ઠ": "th", "ડ": "d", "ઢ": "dh", "ણ": "n", "ત": "t", "થ": "th", "દ": "d", "ધ": "dh", "ન": "n",
    "પ": "p", "ફ": "ph", "બ": "b", "ભ": "bh", "મ": "m", "ય": "y", "ર": "r", "લ": "l", "ળ": "l", "વ": "v",
    "શ": "sh", "ષ": "sh", "સ": "s", "હ": "h", "\x01": "gy",  # \x01 stands for જ્ઞ, which people write "gy" (gyan)
}  # fmt: skip
VOWELS = {
    "અ": "a", "આ": "aa", "ઇ": "i", "ઈ": "ii", "ઉ": "u", "ઊ": "uu", "ઋ": "ru", "ૠ": "ruu", "ઌ": "lu", "ૡ": "luu",
    "ઍ": "e", "એ": "e", "ઐ": "ai", "ઑ": "o", "ઓ": "o", "ઔ": "au", "ૐ": "om",
}  # fmt: skip
VOWEL_SIGNS = {
    "ા": "aa", "િ": "i", "ી": "ii", "ુ": "u", "ૂ": "uu", "ૃ": "ru", "ૄ": "ruu", "ૢ": "lu", "ૣ": "luu", "ૅ": "e",
    "ે": "e", "ૈ": "ai", "ૉ": "o", "ો": "o", "ૌ": "au",
}  # fmt: skip
VIRAMA, ANUSVARA, VISARGA = "્", "ં", "ઃ"
SILENT = {"ઁ", "઼", "ઽ", "‌", "‍"}  # candrabindu, nukta, avagraha and the joiners add no letters

# Applied in order to a lowercase Latin spelling. Both Gujarati (romanised) and typed Latin words go through all of
# them, so what matters is that the two arrive at the same key, not that the key is readable.
FOLDS = [("x", "ks"), ("q", "k"), ("z", "j"), ("w", "v"), ("ph", "f"), ("chh", "ch"), ("ch", "c"), ("sh", "s")]
ASPIRATED = re.compile(r"([kgcjtdb])h")
LONG_VOWELS = [("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u")]
NASAL_BEFORE_LIP = re.compile(r"m(?=[pb])")
DOUBLED = re.compile(r"(.)\1+")

NEAR_MIN = 4  # keys this long or longer also match keys one edit away; shorter ones would match too much


def fold(word: str) -> str:
    """One word as the index stores it: lowercase, Gujarati digits as ASCII, accents off Latin letters."""
    word = unicodedata.normalize("NFC", word).translate(GUJARATI_DIGITS).casefold()
    if word.isascii() or GUJARATI.search(word):
        return word
    plain = "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", plain)


def words(text: str) -> list[str]:
    """The words of a text, folded, in order. Gujarati words keep their signs; punctuation never reaches a query."""
    text = unicodedata.normalize("NFC", text or "")
    return [fold(match) for match in WORD.findall(text)]


def romanise(word: str) -> str:
    """Gujarati letters in plain Latin letters ("સ્વામીજી" → "svaamiijii"); anything else is folded and kept."""
    chars = [c for c in fold(word).replace("જ્ઞ", "\x01") if c not in SILENT]
    out = []
    for at, char in enumerate(chars):
        after = chars[at + 1] if at + 1 < len(chars) else ""
        if char in CONSONANTS:
            out.append(CONSONANTS[char])
            if after != VIRAMA and after not in VOWEL_SIGNS:
                out.append("a")  # the vowel every consonant carries unless a sign replaces or removes it
        elif char in VOWELS:
            out.append(VOWELS[char])
        elif char in VOWEL_SIGNS:
            out.append(VOWEL_SIGNS[char])
        elif char == ANUSVARA:
            if after in CONSONANTS:
                out.append("n")  # સંગ → sang; at the end it is dropped, as Gujlish writes હું as "hu"
        elif char == VISARGA:
            out.append("h")
        elif char != VIRAMA:
            out.append(char)
    return "".join(out)


@lru_cache(maxsize=65536)
def sound_key(word: str) -> str:
    """The spelling key shared by every common way of writing `word`; "" when nothing distinctive is left."""
    key = romanise(word)
    for old, new in FOLDS:
        key = key.replace(old, new)
    key = ASPIRATED.sub(r"\1", key)
    for old, new in LONG_VOWELS:
        key = key.replace(old, new)
    key = NASAL_BEFORE_LIP.sub("n", key).replace("a", "")
    return DOUBLED.sub(r"\1", key)


def edit_variants(key: str) -> set[str]:
    """The key and every way of deleting one letter from it. Two keys one edit apart share at least one variant."""
    return {key} | {key[:at] + key[at + 1 :] for at in range(len(key))}


def similar(a: str, b: str) -> bool:
    """True when `a` and `b` differ by at most one inserted, deleted or changed letter, or two swapped letters."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [at for at in range(len(a)) if a[at] != b[at]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    shorter, longer = sorted((a, b), key=len)
    return any(longer[:at] + longer[at + 1 :] == shorter for at in range(len(longer)))
