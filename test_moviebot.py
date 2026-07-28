import unittest

from moviebot import clean_title, extract_titles


class ExtractionTests(unittest.TestCase):
    def test_removes_ui_noise_and_duplicates(self):
        self.assertEqual(
            extract_titles("Netflix\nDune: Part Two\n2024\nDUNE: PART TWO\nWatch now"),
            ["Dune: Part Two"],
        )

    def test_rejects_urls_and_numeric_lines(self):
        self.assertIsNone(clean_title("https://example.com/movie"))
        self.assertIsNone(clean_title("8.5/10"))

    def test_ignores_long_description_lines(self):
        text = "A movie about a detective who returns home to solve a mystery.\nThe Matrix"
        self.assertEqual(extract_titles(text), ["The Matrix"])


if __name__ == "__main__":
    unittest.main()
