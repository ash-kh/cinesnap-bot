import tempfile
import unittest

from moviebot import Store, caption_tags, clean_title, extract_titles, text_entry_title, unique_titles
from bookbot import BookStore


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

    def test_supports_caption_labels_and_prioritizes_caption_titles(self):
        self.assertEqual(extract_titles("Movie: Lady Bird"), ["Lady Bird"])
        self.assertEqual(
            unique_titles(extract_titles("Lady Bird"), extract_titles("Lady Bird\nThe Matrix")),
            ["Lady Bird", "The Matrix"],
        )

    def test_caption_tags_and_pending_multi_select(self):
        self.assertEqual(caption_tags("Watch this #coming_of_age #Favorite"), ["coming of age", "Favorite"])
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database:
            store = Store(database.name)
            token = store.pending(1, [{"title": "Lady Bird"}, {"title": "Moonlight"}], "title", ["favorite"])
            pending = store.toggle_pending(1, token, 1)
            self.assertEqual(pending["selected"], [1])
            chosen = store.take_pending(1, token)
            self.assertEqual(chosen["items"], [{"title": "Moonlight"}])

    def test_stores_year_and_tags(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database:
            store = Store(database.name)
            store.add(1, [{"title": "Lady Bird", "year": 2017, "tags": ["favorite"]}])
            self.assertEqual(store.list(1)[0]["year"], 2017)
            self.assertEqual(store.list(1)[0]["tags"], ["favorite"])

    def test_prevents_tmdb_and_book_id_duplicates(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database:
            store = Store(database.name)
            first = {"title": "Dune", "year": 2021, "tmdb_id": 438631}
            self.assertEqual(len(store.add(1, [first])), 1)
            self.assertTrue(store.is_duplicate(1, {"title": "Dune", "year": 2021, "tmdb_id": 438631}))
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database:
            store = BookStore(database.name)
            book = {"title": "Dune", "authors": "Frank Herbert", "year": 1965, "book_id": "abc"}
            self.assertEqual(len(store.add(1, [book])), 1)
            self.assertEqual(store.add(1, [book]), [])

    def test_manual_text_entry_removes_tags(self):
        self.assertEqual(text_entry_title("Lady Bird #favorite"), "Lady Bird")
        self.assertEqual(text_entry_title("lady bird"), "Lady bird")
        self.assertIsNone(text_entry_title("&"))

    def test_details_and_statistics_keep_provider_metadata(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database:
            store = Store(database.name)
            store.add(1, [{"title": "Lady Bird", "year": 2017, "tmdb_id": 391713, "overview": "A coming-of-age story.", "poster_url": "https://example.com/poster.jpg", "online_rating": 7.3, "tags": ["favorite"]}])
            self.assertEqual(store.details(1, 1)["overview"], "A coming-of-age story.")
            self.assertEqual(store.stats(1)[:2], (1, 0))


if __name__ == "__main__":
    unittest.main()
