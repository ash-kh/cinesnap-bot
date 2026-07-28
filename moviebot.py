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
            PRIMARY KEY (chat_id, title_key)
        )""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(movies)")}
        if "seen" not in columns:
            self.db.execute("ALTER TABLE movies ADD COLUMN seen INTEGER NOT NULL DEFAULT 0")
        if "rating" not in columns:
            self.db.execute("ALTER TABLE movies ADD COLUMN rating INTEGER")
        self.db.execute("""CREATE TABLE IF NOT EXISTS pending_choices (
            token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, titles TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )""")
        self.db.commit()

    def add(self, chat_id: int, titles: list[str]) -> list[str]:
        added = []
        for title in titles:
            key = title.casefold()
            cur = self.db.execute(
                "INSERT OR IGNORE INTO movies (chat_id, title, title_key, added_at) VALUES (?, ?, ?, ?)",
                (chat_id, title, key, int(time.time())),
            )
            if cur.rowcount:
                added.append(title)
        self.db.commit()
        return added

    def list(self, chat_id: int, seen: bool | None = None) -> list[tuple[str, int | None]]:
        query = "SELECT title, rating FROM movies WHERE chat_id = ?"
        params: list[object] = [chat_id]
        if seen is not None:
            query += " AND seen = ?"
            params.append(int(seen))
        query += " ORDER BY added_at, title_key"
        return list(self.db.execute(query, params))

    def get_by_index(self, chat_id: int, index: int) -> str | None:
        row = self.db.execute(
            "SELECT title FROM movies WHERE chat_id = ? ORDER BY added_at, title_key LIMIT 1 OFFSET ?",
            (chat_id, index - 1),
        ).fetchone()
        return row[0] if row else None

    def mark_seen(self, chat_id: int, index: int, value: bool) -> str | None:
        title = self.get_by_index(chat_id, index)
        if title:
            self.db.execute("UPDATE movies SET seen = ? WHERE chat_id = ? AND title_key = ?", (int(value), chat_id, title.casefold()))
            self.db.commit()
        return title

    def rate(self, chat_id: int, index: int, rating: int) -> str | None:
        title = self.get_by_index(chat_id, index)
        if title:
            self.db.execute("UPDATE movies SET rating = ?, seen = 1 WHERE chat_id = ? AND title_key = ?", (rating, chat_id, title.casefold()))
            self.db.commit()
        return title

    def pending(self, chat_id: int, titles: list[str]) -> str:
        token = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO pending_choices VALUES (?, ?, ?, ?)", (token, chat_id, json.dumps(titles), int(time.time())))
        self.db.commit()
        return token

    def take_pending(self, chat_id: int, token: str) -> list[str] | None:
        row = self.db.execute("SELECT titles FROM pending_choices WHERE token = ? AND chat_id = ?", (token, chat_id)).fetchone()
        if not row:
            return None
        self.db.execute("DELETE FROM pending_choices WHERE token = ?", (token,))
        self.db.commit()
        return json.loads(row[0])

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

    def send(self, chat_id: int, text: str) -> None:
        self.call("sendMessage", chat_id=chat_id, text=text)

    def send_choices(self, chat_id: int, token: str, titles: list[str]) -> None:
        keyboard = [[{"text": title, "callback_data": f"pick:{token}:{i}"}] for i, title in enumerate(titles)]
        keyboard.append([{"text": "Add all", "callback_data": f"pickall:{token}"}])
        self.call("sendMessage", chat_id=chat_id, text="I found several possible movie titles. Which one should I add?", reply_markup=json.dumps({"inline_keyboard": keyboard}))

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


def vision_titles(path: str, caption: str = "") -> list[str]:
    """Use an optional vision model when local OCR cannot read the screenshot."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return []
    image_data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    prompt = (
        "Identify movie titles visible in this screenshot. Ignore the phone status bar, "
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


def grok_titles(path: str, caption: str = "") -> list[str]:
    """Use xAI's OpenAI-compatible image understanding endpoint."""
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        return []
    image_data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    prompt = (
        "Identify movie titles visible in this screenshot. Ignore phone status bars, "
        "social-media usernames, buttons, likes, comments, captions, actors, directors, "
        "years, and descriptions. Return only likely movie titles, one per line, with "
        "no bullets or explanation. Return an empty response if uncertain."
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
        return extract_titles(content)
    except Exception:
        LOG.exception("Grok vision fallback failed; continuing with local OCR")
        return []


def list_text(store: Store, chat_id: int, seen: bool | None = None) -> str:
    rows = store.list(chat_id, seen)
    if not rows:
        return "No movies here yet. Send me a screenshot to add one."
    heading = "Your movies" if seen is None else ("Movies seen" if seen else "Movies not seen")
    return heading + ":\n" + "\n".join(f"{i}. {title}" + (f" — rated {rating}/10" if rating else "") for i, (title, rating) in enumerate(rows, 1))


def command_parts(text: str) -> list[str]:
    return text.split()


def handle_callback(bot: Telegram, store: Store, callback: dict) -> None:
    message = callback.get("message", {})
    chat_id = (message.get("chat") or {}).get("id")
    data = callback.get("data", "")
    if chat_id is None or ":" not in data:
        return
    action, token, *rest = data.split(":")
    titles = store.take_pending(chat_id, token)
    if not titles:
        bot.answer_callback(callback["id"], "That choice has expired.")
        return
    chosen = titles if action == "pickall" else ([titles[int(rest[0])] ] if rest and int(rest[0]) < len(titles) else [])
    added = store.add(chat_id, chosen)
    bot.answer_callback(callback["id"], "Added to your list." if added else "Already on your list.")
    bot.send(chat_id, "Added:\n" + "\n".join(f"• {title}" for title in added) if added else "Those movies were already on your list.")


def handle(bot: Telegram, store: Store, message: dict) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = (message.get("text") or "").strip()
    if text.startswith("/start") or text.startswith("/help"):
        bot.send(chat_id, "Send me a screenshot and I’ll find the movie title in the extra text.\n\nCommands:\n/list — all movies\n/seen — movies you have seen\n/unseen — movies you have not seen\n/seen <number> — mark a movie seen\n/rate <number> <1-10> — rate and mark seen\n/clear — clear your list")
        return
    parts = command_parts(text)
    command = parts[0].split("@", 1)[0] if parts else ""
    if command == "/list":
        bot.send(chat_id, list_text(store, chat_id))
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
    if command == "/clear":
        store.clear(chat_id)
        bot.send(chat_id, "Cleared your movie list.")
        return

    file_id = photo_file_id(message)
    if not file_id:
        if text:
            bot.send(chat_id, "Please send a screenshot, or use /list to see your saved movies.")
        return
    with tempfile.TemporaryDirectory(prefix="moviebot-") as temp_dir:
        image_path = str(Path(temp_dir) / "screenshot")
        bot.download_file(file_id, image_path)
        # Telegram captions are separate from the image payload. Prefer them
        # because a creator's caption often contains the exact movie title.
        caption = message.get("caption", "")
        caption_titles = extract_titles(caption)
        ai_titles = grok_titles(image_path, caption) if os.getenv("XAI_API_KEY") else vision_titles(image_path, caption)
        if ai_titles:
            titles = unique_titles(ai_titles, caption_titles)
        else:
            try:
                image_titles = extract_titles(ocr(image_path))
            except Exception:
                LOG.exception("Local OCR failed")
                image_titles = []
            titles = unique_titles(caption_titles, image_titles)
    if len(titles) > 1:
        titles = titles[:6]
        token = store.pending(chat_id, titles)
        bot.send_choices(chat_id, token, titles)
        return
    added = store.add(chat_id, titles)
    if added:
        bot.send(chat_id, "Added:\n" + "\n".join(f"• {title}" for title in added) + f"\n\nYou have {store.count(chat_id)} movie(s). Use /seen <number> or /rate <number> <1-10> when you watch it.")
    else:
        bot.send(chat_id, "I couldn’t find a new movie title in that screenshot. Try a clearer crop with the title visible.")


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN before starting the bot.")
    bot = Telegram(token)
    store = Store(os.getenv("MOVIE_DB", "movies.sqlite3"))
    offset = 0
    LOG.info("Movie list bot is running")
    while True:
        updates = bot.call("getUpdates", offset=offset, timeout=25, allowed_updates='["message","callback_query"]')
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
