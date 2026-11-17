import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
# Importing core creates its data directory. Keep tests away from real settings.
with tempfile.TemporaryDirectory() as data_dir:
    with patch.dict(os.environ, {"LOCALAPPDATA": data_dir, "HOME": data_dir}):
        import core

from layouts import convert_text


class AutoLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = core.AutoLayout().load()

    def setUp(self):
        self.auto = core.AutoLayout()
        self.auto.d = self.loaded.d
        self.auto.bi = self.loaded.bi

    def decides(self, text, lang="en", protected=None):
        alt, _ = convert_text(text, lang)
        return self.auto.decide(text, alt, lang, protected)

    def test_esche_with_real_dictionaries(self):
        for text in ("tot", "Tot", "tot!", "Tot!"):
            with self.subTest(text=text):
                self.assertTrue(self.decides(text))
                self.assertFalse(self.auto.word_ok(text, "en"))

    def test_phrase_keeps_correct_prefix_and_includes_esche(self):
        for words in (["window", "server", "tot"],
                      ["window", "server", "tot", "ghbdtn"]):
            with self.subTest(words=words):
                self.assertEqual(self.auto.phrase_start(words, "en"), 2)

    def test_common_english_slang_and_acronyms_stay(self):
        for text in ("ok", "no", "it", "the", "you", "and", "for", "window", "server",
                     "wow", "vs", "fr", "ofc", "btw", "LF", "TOT"):
            with self.subTest(text=text):
                self.assertFalse(self.decides(text))
                self.assertTrue(self.auto.word_ok(text, "en"))

    def test_existing_short_corrections_still_work(self):
        for text in ("lf", "yt", "z", "c", "ye", "xt", "gj", "pf"):
            with self.subTest(text=text):
                self.assertTrue(self.decides(text))
                self.assertFalse(self.auto.word_ok(text, "en"))

    def test_correct_russian_stays(self):
        for text in ("еще", "ещё", "да", "привет"):
            with self.subTest(text=text):
                self.assertFalse(self.decides(text, "ru"))
                self.assertTrue(self.auto.word_ok(text, "ru"))

    def test_user_exception_blocks_auto_correction(self):
        self.auto.skip.add("tot")
        self.assertFalse(self.decides("tot"))
        self.assertTrue(self.auto.word_ok("tot", "en"))

    def test_protected_source_or_target_stays(self):
        for protected_word in ("tot", "еще"):
            with self.subTest(word=protected_word):
                protected = lambda w: w == protected_word
                self.assertFalse(self.decides("tot", protected=protected))
                with patch.object(core, "is_protected", protected):
                    self.assertTrue(self.auto.word_ok("tot", "en"))

    def test_ambiguous_short_words_need_strong_frequency_evidence(self):
        for source, target, expected in ((687, 56981, True), (1000, 1000000, False),
                                         (100, 9999, False), (687, 30000, False)):
            with self.subTest(source=source, target=target):
                self.auto.d = {"en": {"tot": source}, "ru": {"еще": target}}
                self.assertEqual(self.decides("tot"), expected)
                self.assertEqual(self.auto.word_ok("tot", "en"), not expected)

    def test_longer_known_english_word_stays_even_with_frequent_alternative(self):
        alt, _ = convert_text("hello", "en")
        self.auto.d = {"en": {"hello": 1}, "ru": {alt: 1000000}}
        self.assertFalse(self.decides("hello"))
        self.assertTrue(self.auto.word_ok("hello", "en"))

    def test_four_letter_collision_uses_the_same_rule(self):
        self.auto.d = {"en": {"tcnm": 50}, "ru": {"есть": 100000}}
        self.assertTrue(self.decides("tcnm"))
        self.assertFalse(self.auto.word_ok("tcnm", "en"))

    def test_unloaded_dictionaries_do_not_auto_correct(self):
        self.auto = core.AutoLayout()
        self.assertFalse(self.decides("tot"))


if __name__ == "__main__":
    unittest.main()
