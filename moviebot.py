#!/usr/bin/env python3
"""Telegram bot that turns movie screenshots into a per-chat movie list."""

from __future__ import annotations

import json
import base64
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageEnhance, ImageOps
except ImportError:  # Local runs can still use the basic OCR path.
    Image = None


LOG = logging.getLogger("moviebot")
API_ROOT = "https://api.telegram.org/bot{token}"


def clean_title(line: str) -> str | None:
    """Turn one OCR line into a plausible movie title, or discard it."""
    line = re.sub(r"\s+", " ", line).strip(" -–—|•·:;,.!?()[]{}\t")
    line = re.sub(r"^(?:movie|film|title)\s*[:\-]\s*", "", line, flags=re.I)
    if not line or len(line) < 2 or len(line) > 120:
        return None
    if re.search(r"https?://|www\.|@\w+|\.(com|net|org)\b", line, re.I):
        return None
    lower = line.casefold()
    if any(marker in lower for marker in (
        "directed by", "must-watch", "watchlist", "add comment", "follow",
        "save this list", "cinephile", "like", "comment", "share", "repost",
    )):
        return None
    if "=" in line or re.search(r"\b\d[\d,.]*\b", line) and len(line.split()) <= 3:
        return None
    if re.fullmatch(r"[\d\W_]+", line) or re.fullmatch(r"[^A-Za-z]+", line):
        return None
    noise = {
        "watch now", "watchlist", "my list", "continue watching", "play",
        "trailer", "netflix", "hulu", "prime video", "disney+", "home",
        "search", "movies", "movie", "series", "tv", "originals",
    }
    if lower in noise:
        return None
    # OCR often leaves an isolated rating, year, or button label behind.
    if re.fullmatch(r"(?:19|20)\d{2}", line) or re.fullmatch(r"\d+(?:\.\d+)?/10", line):
        return None
    line = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", line)
    return line[:1].upper() + line[1:] if line else None


def extract_titles(text: str) -> list[str]:
    """Extract likely title-shaped lines from noisy OCR text."""
    results: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        # Descriptions and UI copy are usually long sentences; title cards are
        # short, contain at most eight words, and rarely end in punctuation.
        if len(raw.strip()) > 80 or len(raw.split()) > 8 or raw.rstrip().endswith((".", ";")):
            continue
        title = clean_title(raw)
        if not title or not title_candidate(title):
            continue
        if title.casefold() not in seen:
            seen.add(title.casefold())
            results.append(title)
    return results


def title_candidate(title: str) -> bool:
    """Reject OCR fragments that are very unlikely to be movie titles."""
    words = re.findall(r"[A-Za-z][A-Za-z'&]*", title)
    if not words or len(title) < 4:
        return False
    if any(len(word) < 3 for word in words):
        return False
    if re.search(r"\s[-–—]\s", title) or re.search(r"[{}=|]", title):
        return False
    # Short title lines should look like title case or all caps. OCR noise is
    # commonly a lowercase fragment such as "la" or "hifi iibide".
    if len(words) <= 4 and not (title.isupper() or all(word[0].isupper() for word in words)):
        return False
    return True


def text_entry_title(text: str) -> str | None:
    """Turn a manual Telegram message into one title, preserving hashtags as tags elsewhere."""
    if text.lstrip().startswith("/"):
        return None
    text = re.sub(r"(?<!\w)#[\w-]+", "", text).strip()
    title = clean_title(text)
    return title if title and title_candidate(title) else None


class Store:
    def __init__(self, path: str):
        # Railway mounts persistent storage at runtime; ensure the directory
        # also works when running locally or before a volume is attached.
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS movies (
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            title_key TEXT NOT NULL,
            added_at INTEGER NOT NULL,
            seen INTEGER NOT NULL DEFAULT 0,
            rating INTEGER,
            year INTEGER,
            tags TEXT NOT NULL DEFAULT '[]',
            tmdb_id INTEGER,
            overview TEXT,
            poster_url TEXT,
            online_rating REAL,
            PRIMARY KEY (chat_id, title_key)
        )""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(movies)")}
        if "seen" not in columns:
            self.db.execute("ALTER TABLE movies ADD COLUMN seen INTEGER NOT NULL DEFAULT 0")
        if "rating" not in columns:
            self.db.execute("ALTER TABLE movies ADD COLUMN rating INTEGER")
        if "year" not in columns:
            self.db.execute("ALTER TABLE movies ADD COLUMN year INTEGER")
        if "tags" not in columns:
            self.db.execute("ALTER TABLE movies ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "tmdb_id" not in columns:
            self.db.execute("ALTER TABLE movies ADD COLUMN tmdb_id INTEGER")
        for name, definition in (("overview", "TEXT"), ("poster_url", "TEXT"), ("online_rating", "REAL")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE movies ADD COLUMN {name} {definition}")
        self.db.execute("""CREATE TABLE IF NOT EXISTS pending_choices (
            token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, titles TEXT NOT NULL,
            created_at INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'title',
            selected TEXT NOT NULL DEFAULT '[]', tags TEXT NOT NULL DEFAULT '[]'
        )""")
        pending_columns = {row[1] for row in self.db.execute("PRAGMA table_info(pending_choices)")}
        if "kind" not in pending_columns:
            self.db.execute("ALTER TABLE pending_choices ADD COLUMN kind TEXT NOT NULL DEFAULT 'title'")
        if "selected" not in pending_columns:
            self.db.execute("ALTER TABLE pending_choices ADD COLUMN selected TEXT NOT NULL DEFAULT '[]'")
        if "tags" not in pending_columns:
            self.db.execute("ALTER TABLE pending_choices ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        self.db.commit()

    def add(self, chat_id: int, movies: list[dict]) -> list[str]:
        added = []
        for movie in movies:
            title = movie["title"]
            year = movie.get("year")
            key = f"{title.casefold()}:{year or ''}"
            tags = sorted(set(movie.get("tags", [])), key=str.casefold)
            if self.is_duplicate(chat_id, movie):
                continue
            cur = self.db.execute(
                "INSERT OR IGNORE INTO movies (chat_id, title, title_key, added_at, year, tags, tmdb_id, overview, poster_url, online_rating) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, title, key, int(time.time()), year, json.dumps(tags), movie.get("tmdb_id"), movie.get("overview"), movie.get("poster_url"), movie.get("online_rating")),
            )
            if cur.rowcount:
                added.append(movie_label(movie))
        self.db.commit()
        return added

    def is_duplicate(self, chat_id: int, movie: dict) -> bool:
        """Treat the same TMDb item, or the same title/year, as a duplicate."""
        tmdb_id = movie.get("tmdb_id")
        if tmdb_id is not None:
            return bool(self.db.execute(
                "SELECT 1 FROM movies WHERE chat_id = ? AND tmdb_id = ? LIMIT 1", (chat_id, tmdb_id)
            ).fetchone())
        title = movie["title"].casefold()
        year = movie.get("year")
        return bool(self.db.execute(
            "SELECT 1 FROM movies WHERE chat_id = ? AND lower(title) = ? AND (year = ? OR year IS NULL OR ? IS NULL) LIMIT 1",
            (chat_id, title, year, year),
        ).fetchone())

    def list(self, chat_id: int, seen: bool | None = None, query_text: str | None = None, tag: str | None = None) -> list[dict]:
        query = "SELECT title, year, rating, tags, tmdb_id, overview, poster_url, online_rating FROM movies WHERE chat_id = ?"
        params: list[object] = [chat_id]
        if seen is not None:
            query += " AND seen = ?"
            params.append(int(seen))
        if query_text:
            query += " AND lower(title) LIKE ?"
            params.append(f"%{query_text.casefold()}%")
        query += " ORDER BY added_at, title_key"
        return [
            {"title": row[0], "year": row[1], "rating": row[2], "tags": json.loads(row[3] or "[]"), "tmdb_id": row[4], "overview": row[5], "poster_url": row[6], "online_rating": row[7]}
            for row in self.db.execute(query, params)
            if not tag or tag.casefold() in {item.casefold() for item in json.loads(row[3] or "[]")}
        ]

    def details(self, chat_id: int, index: int) -> dict | None:
        rows = self.list(chat_id)
        return rows[index - 1] if 1 <= index <= len(rows) else None

    def get_by_index(self, chat_id: int, index: int) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT title, title_key FROM movies WHERE chat_id = ? ORDER BY added_at, title_key LIMIT 1 OFFSET ?",
            (chat_id, index - 1),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def mark_seen(self, chat_id: int, index: int, value: bool) -> str | None:
        movie = self.get_by_index(chat_id, index)
        if movie:
            title, key = movie
            self.db.execute("UPDATE movies SET seen = ? WHERE chat_id = ? AND title_key = ?", (int(value), chat_id, key))
            self.db.commit()
            return title
        return None

    def rate(self, chat_id: int, index: int, rating: int) -> str | None:
        movie = self.get_by_index(chat_id, index)
        if movie:
            title, key = movie
            self.db.execute("UPDATE movies SET rating = ?, seen = 1 WHERE chat_id = ? AND title_key = ?", (rating, chat_id, key))
            self.db.commit()
            return title
        return None

    def tag(self, chat_id: int, index: int, tag: str) -> str | None:
        movie = self.get_by_index(chat_id, index)
        if not movie:
            return None
        title, key = movie
        row = self.db.execute("SELECT tags FROM movies WHERE chat_id = ? AND title_key = ?", (chat_id, key)).fetchone()
        tags = json.loads(row[0] or "[]")
        if tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
            self.db.execute("UPDATE movies SET tags = ? WHERE chat_id = ? AND title_key = ?", (json.dumps(tags), chat_id, key))
            self.db.commit()
        return title

    def remove(self, chat_id: int, index: int) -> str | None:
        movie = self.get_by_index(chat_id, index)
        if not movie:
            return None
        self.db.execute("DELETE FROM movies WHERE chat_id = ? AND title_key = ?", (chat_id, movie[1]))
        self.db.commit()
        return movie[0]

    def edit(self, chat_id: int, index: int, title: str) -> str | None:
        movie = self.get_by_index(chat_id, index)
        if not movie or self.is_duplicate(chat_id, {"title": title}):
            return None
        key = f"{title.casefold()}:"
        self.db.execute("UPDATE movies SET title = ?, title_key = ?, year = NULL, tmdb_id = NULL, overview = NULL, poster_url = NULL, online_rating = NULL WHERE chat_id = ? AND title_key = ?", (title, key, chat_id, movie[1]))
        self.db.commit()
        return title

    def stats(self, chat_id: int) -> tuple[int, int, float | None, list[str]]:
        total, seen, average = self.db.execute("SELECT count(*), sum(seen), avg(rating) FROM movies WHERE chat_id = ?", (chat_id,)).fetchone()
        tags = [tag for row in self.db.execute("SELECT tags FROM movies WHERE chat_id = ?", (chat_id,)) for tag in json.loads(row[0] or "[]")]
        top = sorted(set(tags), key=lambda item: (-sum(tag.casefold() == item.casefold() for tag in tags), item.casefold()))[:3]
        return total, seen or 0, average, top

    def pending(self, chat_id: int, items: list[dict], kind: str, tags: list[str]) -> str:
        token = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO pending_choices (token, chat_id, titles, created_at, kind, selected, tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, chat_id, json.dumps(items), int(time.time()), kind, "[]", json.dumps(tags)),
        )
        self.db.commit()
        return token

    def pending_data(self, chat_id: int, token: str) -> dict | None:
        row = self.db.execute("SELECT titles, kind, selected, tags FROM pending_choices WHERE token = ? AND chat_id = ?", (token, chat_id)).fetchone()
        if not row:
            return None
        return {"items": json.loads(row[0]), "kind": row[1], "selected": json.loads(row[2]), "tags": json.loads(row[3])}

    def toggle_pending(self, chat_id: int, token: str, index: int) -> dict | None:
        pending = self.pending_data(chat_id, token)
        if not pending or not 0 <= index < len(pending["items"]):
            return None
        selected = set(pending["selected"])
        if index in selected:
            selected.remove(index)
        else:
            selected.add(index)
        self.db.execute("UPDATE pending_choices SET selected = ? WHERE token = ?", (json.dumps(sorted(selected)), token))
        self.db.commit()
        pending["selected"] = sorted(selected)
        return pending

    def take_pending(self, chat_id: int, token: str, selected_only: bool = True) -> dict | None:
        pending = self.pending_data(chat_id, token)
        if not pending:
            return None
        self.db.execute("DELETE FROM pending_choices WHERE token = ?", (token,))
        self.db.commit()
        if selected_only:
            pending["items"] = [pending["items"][i] for i in pending["selected"]]
        return pending

    def discard_pending(self, chat_id: int, token: str) -> None:
        self.db.execute("DELETE FROM pending_choices WHERE token = ? AND chat_id = ?", (token, chat_id))
        self.db.commit()

    def count(self, chat_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM movies WHERE chat_id = ?", (chat_id,)).fetchone()[0]

    def clear(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM movies WHERE chat_id = ?", (chat_id,))
        self.db.commit()


class Telegram:
    def __init__(self, token: str):
        self.root = API_ROOT.format(token=token)

    def call(self, method: str, **params):
        data = urllib.parse.urlencode({k: str(v) for k, v in params.items()}).encode()
        with urllib.request.urlopen(f"{self.root}/{method}", data=data, timeout=60) as response:
            payload = json.loads(response.read())
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram API error"))
        return payload["result"]

    def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        params = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
        self.call("sendMessage", **params)

    def send_photo(self, chat_id: int, photo: str, caption: str) -> None:
        self.call("sendPhoto", chat_id=chat_id, photo=photo, caption=caption[:1024])

    def send_menu(self, chat_id: int, text: str = "Choose an option:") -> None:
        self.send(chat_id, text, {"inline_keyboard": [
            [{"text": "🎬 All movies", "callback_data": "menu:list"},
             {"text": "⏳ Not seen", "callback_data": "menu:unseen"}],
            [{"text": "✅ Seen", "callback_data": "menu:seen"},
             {"text": "⭐ Rate a movie", "callback_data": "menu:rate"}],
            [{"text": "🔌 Provider status", "callback_data": "menu:status"},
             {"text": "❓ Help", "callback_data": "menu:help"}],
        ]})

    def configure_menu(self) -> None:
        commands = [
            {"command": "menu", "description": "Show menu options"},
            {"command": "list", "description": "Show all movies"},
            {"command": "unseen", "description": "Show movies not seen"},
            {"command": "seen", "description": "Show movies seen"},
            {"command": "rate", "description": "Rate a movie: /rate 3 8"},
            {"command": "add", "description": "Add a movie by title"},
            {"command": "remove", "description": "Remove a movie: /remove 3"},
            {"command": "edit", "description": "Rename a movie: /edit 3 Title"},
            {"command": "search", "description": "Search your movies"},
            {"command": "stats", "description": "Show list statistics"},
            {"command": "info", "description": "Show movie details: /info 3"},
            {"command": "group", "description": "Explain shared group lists"},
            {"command": "status", "description": "Check vision providers"},
            {"command": "clear", "description": "Clear your movie list"},
            {"command": "help", "description": "Show instructions"},
        ]
        self.call("setMyCommands", commands=json.dumps(commands))
        self.call("setChatMenuButton", menu_button=json.dumps({"type": "commands"}))

    def choice_markup(self, token: str, pending: dict) -> dict:
        selected = set(pending["selected"])
        keyboard = []
        for index, item in enumerate(pending["items"]):
            text = movie_label(item) if pending["kind"] == "movie" else item["title"]
            keyboard.append([{"text": f"{'✅' if index in selected else '☐'} {text}", "callback_data": f"toggle:{token}:{index}"}])
        keyboard.append([
            {"text": "Add selected", "callback_data": f"confirm:{token}"},
            {"text": "Add all", "callback_data": f"all:{token}"},
        ])
        keyboard.append([{"text": "Cancel", "callback_data": f"cancel:{token}"}])
        return {"inline_keyboard": keyboard}

    def send_choices(self, chat_id: int, token: str, pending: dict) -> None:
        if pending["kind"] == "movie":
            text = "TMDb found more than one matching movie. Select the version(s) you want to save:"
        else:
            text = "I found several possible movie titles. Select one or more to add:"
        self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=json.dumps(self.choice_markup(token, pending)))

    def edit_choices(self, chat_id: int, message_id: int, token: str, pending: dict) -> None:
        self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=json.dumps(self.choice_markup(token, pending)))

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def download_file(self, file_id: str, destination: str) -> None:
        info = self.call("getFile", file_id=file_id)
        url = f"https://api.telegram.org/file/bot{self.root.split('bot', 1)[1]}/{info['file_path']}"
        with urllib.request.urlopen(url, timeout=60) as response, open(destination, "wb") as output:
            output.write(response.read())


def photo_file_id(message: dict) -> str | None:
    photos = message.get("photo")
    if photos:
        return photos[-1]["file_id"]
    document = message.get("document") or {}
    if document.get("mime_type", "").startswith("image/"):
        return document.get("file_id")
    return None


def unique_titles(*groups: list[str]) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for title in group:
            if title.casefold() not in seen:
                seen.add(title.casefold())
                results.append(title)
    return results


def caption_tags(caption: str) -> list[str]:
    """Import simple hashtags from an attached caption as movie tags."""
    tags = []
    for tag in re.findall(r"(?<!\w)#([\w-]{1,30})", caption):
        clean = tag.replace("_", " ").strip()
        if clean and clean.casefold() not in {item.casefold() for item in tags}:
            tags.append(clean)
    return tags[:8]


def movie_label(movie: dict) -> str:
    label = movie["title"] + (f" ({movie['year']})" if movie.get("year") else "")
    if movie.get("tags"):
        label += " — " + ", ".join(f"#{tag.replace(' ', '_')}" for tag in movie["tags"])
    return label


def tmdb_matches(title: str) -> list[dict]:
    """Search TMDb and return a short, user-selectable list of movie matches."""
    api_key = os.getenv("TMDB_API_KEY")
    if not api_key:
        return []
    query = urllib.parse.urlencode({"query": title, "include_adult": "false", "language": "en-US"})
    request = urllib.request.Request(
        f"https://api.themoviedb.org/3/search/movie?{query}",
        headers={"Authorization": f"Bearer {api_key}", "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            results = json.loads(response.read()).get("results", [])
        matches = []
        for result in results[:5]:
            name = result.get("title") or result.get("original_title")
            if not name:
                continue
            date = result.get("release_date") or ""
            poster = result.get("poster_path")
            matches.append({
                "title": name, "year": int(date[:4]) if date[:4].isdigit() else None, "tmdb_id": result.get("id"),
                "overview": result.get("overview") or "", "poster_url": f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
                "online_rating": result.get("vote_average"),
            })
        return matches
    except Exception:
        LOG.exception("TMDb search failed for %r; saving without a TMDb match", title)
        return []


def ocr(path: str) -> str:
    """Run Tesseract on the full image and enhanced overlapping crops."""
    sources = [(path, "6"), (path, "11")]
    with tempfile.TemporaryDirectory(prefix="moviebot-ocr-") as work:
        if Image is not None:
            image = Image.open(path).convert("RGB")
            width, height = image.size
            # Instagram screenshots often contain a title in only one band;
            # overlapping crops prevent the surrounding UI from dominating OCR.
            for index in range(4):
                top = max(0, int(height * (index * 0.22)))
                bottom = min(height, int(height * (index * 0.22 + 0.50)))
                crop = image.crop((0, top, width, bottom))
                gray = ImageOps.grayscale(crop)
                gray = ImageOps.autocontrast(gray)
                gray = ImageEnhance.Contrast(gray).enhance(1.8)
                gray = gray.resize((gray.width * 2, gray.height * 2))
                crop_path = str(Path(work) / f"crop-{index}.png")
                gray.save(crop_path)
                sources.append((crop_path, "11"))
                if index in (1, 2):
                    # White title lettering over a photo benefits from a
                    # binary high-contrast pass.
                    threshold = gray.point(lambda pixel: 255 if pixel > 165 else 0)
                    threshold_path = str(Path(work) / f"threshold-{index}.png")
                    threshold.save(threshold_path)
                    sources.append((threshold_path, "11"))
        outputs = []
        for source, psm in sources:
            completed = subprocess.run(
                [os.getenv("TESSERACT_BIN", "tesseract"), source, "stdout", "--psm", psm],
                capture_output=True, text=True, timeout=45, check=False,
            )
            if completed.returncode == 0:
                outputs.append(completed.stdout)
        if not outputs:
            raise RuntimeError("OCR failed")
        return "\n".join(outputs)


def vision_titles(path: str, caption: str = "", subject: str = "movie") -> list[str]:
    """Use an optional vision model when local OCR cannot read the screenshot."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return []
    image_data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    prompt = (
        f"Identify {subject} titles visible in this screenshot. Ignore the phone status bar, "
        "social-media usernames, buttons, likes, comments, captions, actor names, "
        "directors, years, and descriptions. Return only likely movie titles, one per "
        "line, with no bullets or explanation. If there is no confident movie title, "
        "return an empty response."
    )
    if caption:
        prompt += f"\nThe Telegram caption was: {caption[:2000]}"
    body = {
        "model": os.getenv("OPENAI_VISION_MODEL", "gpt-5.6-luna"),
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": f"data:image/png;base64,{image_data}", "detail": "high"},
        ]}],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
        output = payload.get("output_text", "")
        if not output:
            output = "\n".join(
                item.get("text", "")
                for item in payload.get("output", [])
                for item in item.get("content", [])
                if item.get("type") == "output_text"
            )
        return extract_titles(output)
    except Exception:
        LOG.exception("Vision fallback failed; continuing with local OCR")
        return []


def grok_titles(path: str, caption: str = "", subject: str = "movie") -> list[str]:
    """Use xAI's OpenAI-compatible image understanding endpoint."""
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        return []
    image_data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    prompt = (
        f"Identify {subject} titles visible in this screenshot. Ignore phone status bars, "
        "social-media usernames, buttons, likes, comments, captions, actors, directors, "
        "years, and descriptions. If multiple distinct movie titles are visible, return "
        "every one. Return only likely movie titles, one per line, with no bullets or "
        "explanation. Return an empty response if uncertain."
    )
    if caption:
        prompt += f"\nThe Telegram caption was: {caption[:2000]}"
    body = {
        "model": os.getenv("XAI_VISION_MODEL", "grok-4.20-0309-non-reasoning"),
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}", "detail": "high"}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0,
    }
    request = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
        content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
        titles = extract_titles(content)
        LOG.info("Grok vision returned %d candidate(s): %s", len(titles), titles)
        return titles
    except Exception:
        LOG.exception("Grok vision fallback failed; continuing with local OCR")
        return []


def gemini_titles(path: str, caption: str = "", subject: str = "movie") -> list[str]:
    """Use Google's Gemini image understanding endpoint."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return []
    image_data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    prompt = (
        f"Identify {subject} titles visible in this screenshot. Ignore phone status bars, "
        "social-media usernames, buttons, likes, comments, captions, actors, directors, "
        "years, and descriptions. If multiple distinct movie titles are visible, return "
        "every one. Return only likely movie titles, one per line, with no bullets or "
        "explanation. Return an empty response if uncertain."
    )
    if caption:
        prompt += f"\nThe Telegram caption was: {caption[:2000]}"
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "image/png", "data": image_data}},
        {"text": prompt},
    ]}]}
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
        text = "\n".join(
            part.get("text", "")
            for candidate in payload.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
        )
        titles = extract_titles(text)
        LOG.info("Gemini vision returned %d candidate(s): %s", len(titles), titles)
        return titles
    except Exception:
        LOG.exception("Gemini vision fallback failed; trying next provider")
        return []


def list_text(store: Store, chat_id: int, seen: bool | None = None) -> str:
    rows = store.list(chat_id, seen)
    if not rows:
        return "No movies here yet. Send me a screenshot to add one."
    heading = "Your movies" if seen is None else ("Movies seen" if seen else "Movies not seen")
    return heading + ":\n" + "\n".join(
        f"{i}. {movie_label(row)}" + (f" — rated {row['rating']}/10" if row["rating"] else "")
        for i, row in enumerate(rows, 1)
    )


def send_movie_list(bot: Telegram, store: Store, chat_id: int, seen: bool | None = None) -> None:
    bot.send(chat_id, list_text(store, chat_id, seen))
    # Item numbers in the full list are stable and can open a poster/details view.
    if seen is None:
        rows = store.list(chat_id)
        if rows:
            keyboard = [[{"text": f"ℹ️ {i}. {row['title'][:34]}", "callback_data": f"info:{i}"}] for i, row in enumerate(rows[:20], 1)]
            bot.send(chat_id, "Select a movie for its poster, description, and rating:", {"inline_keyboard": keyboard})


def movie_detail_text(movie: dict) -> str:
    lines = [movie_label(movie)]
    if movie.get("online_rating") is not None:
        lines.append(f"TMDb community rating: {float(movie['online_rating']):.1f}/10")
    if movie.get("overview"):
        lines.append(movie["overview"])
    return "\n\n".join(lines)


def send_movie_detail(bot: Telegram, store: Store, chat_id: int, index: int) -> None:
    movie = store.details(chat_id, index)
    if not movie:
        bot.send(chat_id, "I couldn’t find that movie number. Use /list first.")
        return
    text = movie_detail_text(movie)
    if movie.get("poster_url"):
        try:
            bot.send_photo(chat_id, movie["poster_url"], text)
            return
        except Exception:
            LOG.exception("Could not send TMDb poster")
    bot.send(chat_id, text)


def stats_text(store: Store, chat_id: int) -> str:
    total, seen, average, tags = store.stats(chat_id)
    if not total:
        return "No movie statistics yet."
    return f"Movie stats:\nTotal: {total}\nSeen: {seen}\nNot seen: {total - seen}" + (f"\nYour average rating: {average:.1f}/10" if average is not None else "") + (f"\nTop tags: {', '.join('#' + tag.replace(' ', '_') for tag in tags)}" if tags else "")


def provider_status_text() -> str:
    return ("Vision providers configured:\n"
            f"Gemini: {'yes' if os.getenv('GEMINI_API_KEY') else 'no'}\n"
            f"Grok: {'yes' if os.getenv('XAI_API_KEY') else 'no'}\n"
            f"OpenAI: {'yes' if os.getenv('OPENAI_API_KEY') else 'no'}\n"
            f"TMDb matching: {'yes' if os.getenv('TMDB_API_KEY') else 'no'}")


def help_text() -> str:
    return ("Send me a screenshot and I’ll find the movie title in the extra text.\n\n"
            "/list — all movies\n/seen — movies you have seen\n/unseen — movies you have not seen\n"
            "/seen <number> — mark a movie seen\n/unseen <number> — mark it not seen\n"
            "/rate <number> <1-10> — rate and mark seen\n/status — check vision providers\n"
            "/add <movie title> — add a movie by typing its title\n"
            "/tag <number> <tag> — add a tag to a movie\n"
            "/tag <tag> — filter by a tag\n/search <words> — search your list\n"
            "/info <number> — poster, description, and online rating\n"
            "/remove <number> — remove a movie\n/edit <number> <title> — rename a movie\n/stats — list statistics\n/group — shared-list help\n"
            "/clear — clear your list\n/menu — show these buttons")


def command_parts(text: str) -> list[str]:
    return text.split()


def handle_callback(bot: Telegram, store: Store, callback: dict) -> None:
    message = callback.get("message", {})
    chat_id = (message.get("chat") or {}).get("id")
    data = callback.get("data", "")
    if chat_id is None or ":" not in data:
        return
    action, token, *rest = data.split(":")
    if action == "info" and token.isdigit():
        send_movie_detail(bot, store, chat_id, int(token))
        bot.answer_callback(callback["id"], "Opening movie details.")
        return
    if action == "menu":
        if token == "list":
            send_movie_list(bot, store, chat_id)
        elif token == "seen":
            bot.send(chat_id, list_text(store, chat_id, True))
        elif token == "unseen":
            bot.send(chat_id, list_text(store, chat_id, False))
        elif token == "rate":
            bot.send(chat_id, "Use /list to find a movie number, then rate it with /rate <number> <1-10>.\nExample: /rate 3 8")
        elif token == "status":
            bot.send(chat_id, provider_status_text())
        else:
            bot.send_menu(chat_id, help_text())
        bot.answer_callback(callback["id"], "Done")
        return
    if action == "cancel":
        store.discard_pending(chat_id, token)
        bot.answer_callback(callback["id"], "Cancelled.")
        return
    if action == "toggle":
        if not rest or not rest[0].isdigit():
            bot.answer_callback(callback["id"], "Invalid choice.")
            return
        pending = store.toggle_pending(chat_id, token, int(rest[0]))
        if not pending:
            bot.answer_callback(callback["id"], "That choice has expired.")
            return
        bot.edit_choices(chat_id, message.get("message_id"), token, pending)
        bot.answer_callback(callback["id"], "Selection updated.")
        return
    preview = store.pending_data(chat_id, token)
    if action == "confirm" and preview is not None and not preview["selected"]:
        bot.answer_callback(callback["id"], "Select at least one first.")
        return
    pending = store.take_pending(chat_id, token, selected_only=action != "all")
    if not pending:
        bot.answer_callback(callback["id"], "That choice has expired.")
        return
    chosen = pending["items"]
    if pending["kind"] == "title":
        matches = []
        for item in chosen:
            found = tmdb_matches(item["title"])
            matches.extend(found or [{"title": item["title"]}])
        for match in matches:
            match["tags"] = pending["tags"]
        if len(matches) > 1:
            next_token = store.pending(chat_id, matches[:12], "movie", pending["tags"])
            bot.send_choices(chat_id, next_token, store.pending_data(chat_id, next_token))
            bot.answer_callback(callback["id"], "Now choose the matching movie versions.")
            return
        chosen = matches
    else:
        for movie in chosen:
            movie["tags"] = pending["tags"]
    added = store.add(chat_id, chosen)
    bot.answer_callback(callback["id"], "Added to your list." if added else "Already on your list.")
    bot.send(chat_id, "Added:\n" + "\n".join(f"• {title}" for title in added) if added else "Those movies were already on your list.")


def save_or_choose_tmdb(bot: Telegram, store: Store, chat_id: int, titles: list[str], tags: list[str]) -> None:
    """Save an unambiguous result, otherwise ask the user to choose a TMDb match."""
    matches = []
    for title in titles:
        found = tmdb_matches(title)
        matches.extend(found or [{"title": title}])
    for match in matches:
        match["tags"] = tags
    if len(matches) > 1:
        token = store.pending(chat_id, matches[:12], "movie", tags)
        bot.send_choices(chat_id, token, store.pending_data(chat_id, token))
        return
    added = store.add(chat_id, matches)
    if added:
        bot.send(chat_id, "Added:\n" + "\n".join(f"• {title}" for title in added) + f"\n\nYou have {store.count(chat_id)} movie(s). Use /seen <number> or /rate <number> <1-10> when you watch it.")
    else:
        bot.send(chat_id, "That movie is already on your list.")


def handle(bot: Telegram, store: Store, message: dict) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = (message.get("text") or "").strip()
    if text.startswith("/start") or text.startswith("/help") or text.startswith("/menu"):
        bot.send_menu(chat_id, help_text())
        return
    parts = command_parts(text)
    command = parts[0].split("@", 1)[0] if parts else ""
    if command == "/list":
        send_movie_list(bot, store, chat_id)
        return
    if command == "/seen" and len(parts) == 1:
        bot.send(chat_id, list_text(store, chat_id, True))
        return
    if command == "/unseen" and len(parts) == 1:
        bot.send(chat_id, list_text(store, chat_id, False))
        return
    if command in ("/seen", "/unseen") and len(parts) == 2 and parts[1].isdigit():
        title = store.mark_seen(chat_id, int(parts[1]), command == "/seen")
        bot.send(chat_id, f"Marked “{title}” as {'seen' if command == '/seen' else 'not seen'}." if title else "I couldn’t find that movie number. Use /list first.")
        return
    if command == "/rate" and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit() and 1 <= int(parts[2]) <= 10:
        title = store.rate(chat_id, int(parts[1]), int(parts[2]))
        bot.send(chat_id, f"Rated “{title}” {parts[2]}/10 and marked it seen." if title else "I couldn’t find that movie number. Use /list first.")
        return
    if command == "/info" and len(parts) == 2 and parts[1].isdigit():
        send_movie_detail(bot, store, chat_id, int(parts[1]))
        return
    if command == "/remove" and len(parts) == 2 and parts[1].isdigit():
        title = store.remove(chat_id, int(parts[1]))
        bot.send(chat_id, f"Removed “{title}”." if title else "I couldn’t find that movie number. Use /list first.")
        return
    if command == "/edit" and len(parts) >= 3 and parts[1].isdigit():
        title = text_entry_title(" ".join(parts[2:]))
        edited = store.edit(chat_id, int(parts[1]), title) if title else None
        bot.send(chat_id, f"Renamed the movie to “{edited}”." if edited else "I couldn’t edit that movie. Check the number and title, or it may already be on your list.")
        return
    if command == "/search" and len(parts) >= 2:
        rows = store.list(chat_id, query_text=" ".join(parts[1:]))
        bot.send(chat_id, "Search results:\n" + "\n".join(f"{i}. {movie_label(row)}" for i, row in enumerate(rows, 1)) if rows else "No matching movies.")
        return
    if command == "/tag" and len(parts) == 2:
        rows = store.list(chat_id, tag=parts[1].lstrip("#"))
        bot.send(chat_id, "Tagged movies:\n" + "\n".join(f"{i}. {movie_label(row)}" for i, row in enumerate(rows, 1)) if rows else "No movies have that tag.")
        return
    if command == "/tag" and len(parts) >= 3 and parts[1].isdigit():
        tag = " ".join(parts[2:]).lstrip("#").strip()
        if not tag:
            bot.send(chat_id, "Use /tag <movie number> <tag>. Example: /tag 3 favorite")
            return
        title = store.tag(chat_id, int(parts[1]), tag)
        bot.send(chat_id, f"Added #{tag.replace(' ', '_')} to “{title}”." if title else "I couldn’t find that movie number. Use /list first.")
        return
    if command == "/stats":
        bot.send(chat_id, stats_text(store, chat_id))
        return
    if command == "/group":
        bot.send(chat_id, "In a Telegram group, every member uses the same shared movie list for that group. In a private chat, your list stays private.")
        return
    if command == "/clear":
        store.clear(chat_id)
        bot.send(chat_id, "Cleared your movie list.")
        return
    if command == "/status":
        bot.send(chat_id, provider_status_text())
        return
    if command == "/add":
        title = text_entry_title(" ".join(parts[1:]))
        if title:
            save_or_choose_tmdb(bot, store, chat_id, [title], caption_tags(text))
        else:
            bot.send(chat_id, "Use /add followed by a movie title. Example: /add Lady Bird")
        return

    file_id = photo_file_id(message)
    if not file_id:
        if text:
            title = text_entry_title(text)
            if title:
                save_or_choose_tmdb(bot, store, chat_id, [title], caption_tags(text))
            else:
                bot.send_menu(chat_id, "I didn’t understand that. Send a screenshot, type a movie title, or choose an option:")
        return
    with tempfile.TemporaryDirectory(prefix="moviebot-") as temp_dir:
        image_path = str(Path(temp_dir) / "screenshot")
        bot.download_file(file_id, image_path)
        # Telegram captions are separate from the image payload. Prefer them
        # because a creator's caption often contains the exact movie title.
        caption = message.get("caption", "")
        caption_titles = extract_titles(caption)
        tags = caption_tags(caption)
        vision_configured = any(os.getenv(name) for name in ("GEMINI_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY"))
        provider = "local OCR"
        if os.getenv("GEMINI_API_KEY"):
            LOG.info("Trying Gemini vision model %s", os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
        ai_titles = gemini_titles(image_path, caption)
        if not ai_titles:
            if os.getenv("XAI_API_KEY"):
                LOG.info("Trying Grok vision model %s", os.getenv("XAI_VISION_MODEL", "grok-4.20-0309-non-reasoning"))
            ai_titles = grok_titles(image_path, caption)
            if ai_titles:
                provider = "Grok"
        if not ai_titles:
            if os.getenv("OPENAI_API_KEY"):
                LOG.info("Trying OpenAI vision model %s", os.getenv("OPENAI_VISION_MODEL", "gpt-5.6-luna"))
            ai_titles = vision_titles(image_path, caption)
            if ai_titles:
                provider = "OpenAI"
        if ai_titles:
            if provider == "local OCR":
                provider = "Gemini"
            LOG.info("Using %s candidates: %s", provider, ai_titles)
            titles = unique_titles(ai_titles, caption_titles)
        else:
            # Once a vision provider is configured, do not fall back to noisy
            # local OCR after an API failure; that creates false movie names.
            if vision_configured:
                image_titles = []
            else:
                try:
                    image_titles = extract_titles(ocr(image_path))
                except Exception:
                    LOG.exception("Local OCR failed")
                    image_titles = []
            titles = unique_titles(caption_titles, image_titles)
            LOG.info("Using local OCR/caption candidates: %s", titles)
    if len(titles) > 1:
        titles = titles[:6]
        token = store.pending(chat_id, [{"title": title} for title in titles], "title", tags)
        bot.send_choices(chat_id, token, store.pending_data(chat_id, token))
        return
    if titles:
        save_or_choose_tmdb(bot, store, chat_id, titles, tags)
    else:
        bot.send(chat_id, "I couldn’t confidently identify a movie title. Add the title as a caption, or check /status and the Railway logs to confirm a vision provider is configured.")


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    # Use a CineSnap-specific name first to avoid ambiguity with a stale or
    # shared Railway variable. Keep the original name for existing installs.
    token = os.getenv("CINESNAP_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set CINESNAP_TELEGRAM_TOKEN before starting the bot.")
    LOG.info("Using %s for Telegram authentication", "CINESNAP_TELEGRAM_TOKEN" if os.getenv("CINESNAP_TELEGRAM_TOKEN") else "TELEGRAM_BOT_TOKEN")
    bot = Telegram(token)
    try:
        bot.configure_menu()
    except Exception:
        LOG.exception("Could not configure Telegram command menu; continuing")
    store = Store(os.getenv("MOVIE_DB", "movies.sqlite3"))
    offset = 0
    LOG.info("Movie list bot is running")
    while True:
        try:
            updates = bot.call("getUpdates", offset=offset, timeout=25, allowed_updates='["message","callback_query"]')
        except Exception:
            # A temporary Telegram outage or a duplicate long-poll instance
            # must not make the container exit and trigger a crash loop.
            LOG.exception("getUpdates failed; retrying in 5 seconds")
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if update.get("callback_query"):
                    handle_callback(bot, store, update["callback_query"])
                else:
                    handle(bot, store, update.get("message", {}))
            except Exception:
                LOG.exception("Could not process update %s", update.get("update_id"))


if __name__ == "__main__":
    main()
