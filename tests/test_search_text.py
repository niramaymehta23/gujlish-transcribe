"""Search words and spelling keys: the same word written in Gujarati, Gujlish or English letters, or with a common
spelling difference, must reach the same key so one search finds it in every form."""

import unittest

from web.search.text import edit_variants, romanise, similar, sound_key, words


class SoundKeyTest(unittest.TestCase):
    PAIRS = [
        ("કરીને", "karine", "kareene"),
        ("છે", "che", "chhe"),
        ("સ્વામીજી", "swamiji", "svamiji", "Swamiji"),
        ("ભગવાન", "bhagwan", "bhagvan", "Bhagwan"),
        ("દર્શન", "darshan", "darsan"),
        ("સત્સંગ", "satsang", "Satsang"),
        ("બ્રાહ્મણ", "brahman"),
        ("સેવા", "seva", "sewa"),
        ("વાત", "vaat", "vat", "waat"),
    ]

    def test_each_spelling_of_a_word_shares_one_key(self):
        for spellings in self.PAIRS:
            keys = {sound_key(word) for word in spellings}
            self.assertEqual(len(keys), 1, f"{spellings} gave {keys}")
            self.assertTrue(next(iter(keys)), spellings)

    def test_different_words_keep_different_keys(self):
        self.assertNotEqual(sound_key("સેવા"), sound_key("સ્વામી"))
        self.assertNotEqual(sound_key("darshan"), sound_key("dharma"))

    def test_anusvara_at_the_end_is_dropped_as_gujlish_writes_it(self):
        self.assertEqual(sound_key("હું"), sound_key("hu"))
        self.assertEqual(sound_key("મારું"), sound_key("maru"))

    def test_romanise_writes_gujarati_in_plain_latin_letters(self):
        self.assertEqual(romanise("સ્વામીજી"), "svaamiijii")
        self.assertEqual(romanise("સત્સંગ"), "satsanga")
        self.assertEqual(romanise("૨૦૨૬"), "2026")
        self.assertEqual(romanise("hello"), "hello")


class WordsTest(unittest.TestCase):
    def test_gujarati_words_stay_whole_with_their_signs(self):
        self.assertEqual(words("સ્વામીજી કહે છે, ભગવાન!"), ["સ્વામીજી", "કહે", "છે", "ભગવાન"])

    def test_joiners_stay_inside_a_word(self):
        self.assertEqual(words("ર‍્યો"), ["ર‍્યો"])

    def test_latin_words_are_folded_and_gujarati_digits_become_ascii(self):
        self.assertEqual(words("Swamiji's Café ૧૨ times"), ["swamiji", "s", "cafe", "12", "times"])

    def test_punctuation_and_underscores_split_words(self):
        self.assertEqual(words("seva_vaat—darshan...?"), ["seva", "vaat", "darshan"])


class SimilarTest(unittest.TestCase):
    def test_edit_variants_hold_each_one_letter_deletion(self):
        self.assertEqual(edit_variants("abcd"), {"abcd", "bcd", "acd", "abd", "abc"})

    def test_similar_accepts_one_edit_including_a_swap(self):
        self.assertTrue(similar("svmji", "svmiji"))  # one letter missing
        self.assertTrue(similar("svmjii", "svmiji"))  # two letters swapped
        self.assertTrue(similar("krisn", "krusn"))  # one letter changed
        self.assertFalse(similar("svmiji", "smji"))  # two letters missing
        self.assertFalse(similar("drsn", "drm"))


if __name__ == "__main__":
    unittest.main()
