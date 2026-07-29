#!/usr/bin/env python3
"""Telegram bot that turns book screenshots into a tagged reading list."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from moviebot import Telegram, caption_tags, extract_titles, gemini_titles, grok_titles, ocr, text_entry_title, vision_titles


LOG = logging.getLogger("bookbot")


def book_label(book: dict) -> str:
    label = book["title"]
    if book.get("authors"):
        label += f" — {book['authors']}"
    if book.get("year"):
        label += f" ({book['year']})"
    if book.get("tags"):
        label += " — " + ", ".join(f"#{tag.replace(' ', '_')}" for tag in book["tags"])
    return label


class BookStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS books (
            chat_id INTEGER NOT NULL, title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '',
            year INTEGER, book_id TEXT, book_key TEXT NOT NULL, added_at INTEGER NOT NULL,
            read INTEGER NOT NULL DEFAULT 0, rating INTEGER, tags TEXT NOT NULL DEFAULT '[]',
            description TEXT, cover_url TEXT, online_rating REAL,
            PRIMARY KEY (chat_id, book_key)
        )""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(books)")}
        for name, definition in (("description", "TEXT"), ("cover_url", "TEXT"), ("online_rating", "REAL")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE books ADD COLUMN {name} {definition}")
        self.db.execute("""CREATE TABLE IF NOT EXISTS pending_choices (
            token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, items TEXT NOT NULL,
            kind TEXT NOT NULL, selected TEXT NOT NULL DEFAULT '[]', tags TEXT NOT NULL DEFAULT '[]'
        )""")
        self.db.commit()

    def is_duplicate(self, chat_id: int, book: dict) -> bool:
        if book.get("book_id"):
            return bool(self.db.execute("SELECT 1 FROM books WHERE chat_id=? AND book_id=? LIMIT 1", (chat_id, book["book_id"])).fetchone())
        return bool(self.db.execute(
            "SELECT 1 FROM books WHERE chat_id=? AND lower(title)=? AND lower(authors)=? AND (year=? OR year IS NULL OR ? IS NULL) LIMIT 1",
            (chat_id, book["title"].casefold(), book.get("authors", "").casefold(), book.get("year"), book.get("year")),
        ).fetchone())

    def add(self, chat_id: int, books: list[dict]) -> list[str]:
        added = []
        for book in books:
            if self.is_duplicate(chat_id, book):
                continue
            key = f"{book['title'].casefold()}:{book.get('authors', '').casefold()}:{book.get('year') or ''}"
            cur = self.db.execute(
                "INSERT OR IGNORE INTO books (chat_id,title,authors,year,book_id,book_key,added_at,tags,description,cover_url,online_rating) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (chat_id, book["title"], book.get("authors", ""), book.get("year"), book.get("book_id"), key, int(time.time()), json.dumps(sorted(set(book.get("tags", [])), key=str.casefold)), book.get("description"), book.get("cover_url"), book.get("online_rating")),
            )
            if cur.rowcount:
                added.append(book_label(book))
        self.db.commit()
        return added

    def list(self, chat_id: int, read: bool | None = None, query_text: str | None = None, tag: str | None = None) -> list[dict]:
        query, params = "SELECT title,authors,year,rating,tags,book_id,description,cover_url,online_rating FROM books WHERE chat_id=?", [chat_id]
        if read is not None:
            query += " AND read=?"
            params.append(int(read))
        if query_text:
            query += " AND (lower(title) LIKE ? OR lower(authors) LIKE ?)"
            params.extend([f"%{query_text.casefold()}%", f"%{query_text.casefold()}%"])
        query += " ORDER BY added_at, book_key"
        return [{"title": row[0], "authors": row[1], "year": row[2], "rating": row[3], "tags": json.loads(row[4]), "book_id": row[5], "description": row[6], "cover_url": row[7], "online_rating": row[8]} for row in self.db.execute(query, params) if not tag or tag.casefold() in {item.casefold() for item in json.loads(row[4] or "[]")}]

    def details(self, chat_id: int, index: int) -> dict | None:
        books = self.list(chat_id)
        return books[index - 1] if 1 <= index <= len(books) else None

    def update_metadata(self, chat_id: int, index: int, book: dict) -> None:
        row = self.by_index(chat_id, index)
        if not row: return
        self.db.execute("UPDATE books SET description=?,cover_url=?,online_rating=? WHERE chat_id=? AND book_key=?", (book.get("description"), book.get("cover_url"), book.get("online_rating"), chat_id, row[1]))
        self.db.commit()

    def by_index(self, chat_id: int, index: int) -> tuple[str, str] | None:
        return self.db.execute("SELECT title,book_key FROM books WHERE chat_id=? ORDER BY added_at,book_key LIMIT 1 OFFSET ?", (chat_id, index - 1)).fetchone()

    def mark_read(self, chat_id: int, index: int, value: bool) -> str | None:
        row = self.by_index(chat_id, index)
        if not row:
            return None
        self.db.execute("UPDATE books SET read=? WHERE chat_id=? AND book_key=?", (int(value), chat_id, row[1]))
        self.db.commit()
        return row[0]

    def rate(self, chat_id: int, index: int, rating: int) -> str | None:
        row = self.by_index(chat_id, index)
        if not row:
            return None
        self.db.execute("UPDATE books SET rating=?,read=1 WHERE chat_id=? AND book_key=?", (rating, chat_id, row[1]))
        self.db.commit()
        return row[0]

    def tag(self, chat_id: int, index: int, tag: str) -> str | None:
        row = self.by_index(chat_id, index)
        if not row:
            return None
        tags = json.loads(self.db.execute("SELECT tags FROM books WHERE chat_id=? AND book_key=?", (chat_id, row[1])).fetchone()[0])
        if tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
            self.db.execute("UPDATE books SET tags=? WHERE chat_id=? AND book_key=?", (json.dumps(tags), chat_id, row[1]))
            self.db.commit()
        return row[0]

    def remove(self, chat_id: int, index: int) -> str | None:
        row = self.by_index(chat_id, index)
        if not row: return None
        self.db.execute("DELETE FROM books WHERE chat_id=? AND book_key=?", (chat_id, row[1]))
        self.db.commit()
        return row[0]

    def edit(self, chat_id: int, index: int, title: str) -> str | None:
        row = self.by_index(chat_id, index)
        if not row or self.is_duplicate(chat_id, {"title": title}): return None
        self.db.execute("UPDATE books SET title=?,book_key=?,authors='',year=NULL,book_id=NULL,description=NULL,cover_url=NULL,online_rating=NULL WHERE chat_id=? AND book_key=?", (title, f"{title.casefold()}::", chat_id, row[1]))
        self.db.commit()
        return title

    def stats(self, chat_id: int) -> tuple[int, int, float | None, list[str]]:
        total, read, average = self.db.execute("SELECT count(*),sum(read),avg(rating) FROM books WHERE chat_id=?", (chat_id,)).fetchone()
        tags = [tag for row in self.db.execute("SELECT tags FROM books WHERE chat_id=?", (chat_id,)) for tag in json.loads(row[0] or "[]")]
        top = sorted(set(tags), key=lambda item: (-sum(tag.casefold() == item.casefold() for tag in tags), item.casefold()))[:3]
        return total, read or 0, average, top

    def pending(self, chat_id: int, items: list[dict], kind: str, tags: list[str]) -> str:
        token = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO pending_choices (token,chat_id,items,kind,selected,tags) VALUES (?,?,?,?,?,?)", (token, chat_id, json.dumps(items), kind, "[]", json.dumps(tags)))
        self.db.commit()
        return token

    def pending_data(self, chat_id: int, token: str) -> dict | None:
        row = self.db.execute("SELECT items,kind,selected,tags FROM pending_choices WHERE token=? AND chat_id=?", (token, chat_id)).fetchone()
        return {"items": json.loads(row[0]), "kind": row[1], "selected": json.loads(row[2]), "tags": json.loads(row[3])} if row else None

    def toggle(self, chat_id: int, token: str, index: int) -> dict | None:
        data = self.pending_data(chat_id, token)
        if not data or not 0 <= index < len(data["items"]):
            return None
        selected = set(data["selected"])
        selected.symmetric_difference_update({index})
        data["selected"] = sorted(selected)
        self.db.execute("UPDATE pending_choices SET selected=? WHERE token=?", (json.dumps(data["selected"]), token))
        self.db.commit()
        return data

    def take(self, chat_id: int, token: str, all_items: bool = False) -> dict | None:
        data = self.pending_data(chat_id, token)
        if not data:
            return None
        self.db.execute("DELETE FROM pending_choices WHERE token=? AND chat_id=?", (token, chat_id))
        self.db.commit()
        if not all_items:
            data["items"] = [data["items"][i] for i in data["selected"]]
        return data

    def discard(self, chat_id: int, token: str) -> None:
        self.db.execute("DELETE FROM pending_choices WHERE token=? AND chat_id=?", (token, chat_id))
        self.db.commit()

    def count(self, chat_id: int) -> int:
        return self.db.execute("SELECT count(*) FROM books WHERE chat_id=?", (chat_id,)).fetchone()[0]

    def clear(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM books WHERE chat_id=?", (chat_id,))
        self.db.commit()


class BookTelegram(Telegram):
    def configure_menu(self) -> None:
        commands = [
            {"command": "menu", "description": "Show menu options"},
            {"command": "list", "description": "Show all books"},
            {"command": "unread", "description": "Show books to read"},
            {"command": "read", "description": "Show books read"},
            {"command": "rate", "description": "Rate a book: /rate 3 8"},
            {"command": "add", "description": "Add a book by title"},
            {"command": "tag", "description": "Tag a book: /tag 3 favorite"},
            {"command": "remove", "description": "Remove a book: /remove 3"},
            {"command": "edit", "description": "Rename a book: /edit 3 Title"},
            {"command": "search", "description": "Search your books"},
            {"command": "stats", "description": "Show reading statistics"},
            {"command": "info", "description": "Show book details: /info 3"},
            {"command": "group", "description": "Explain shared group lists"},
            {"command": "clear", "description": "Clear your book list"},
        ]
        self.call("setMyCommands", commands=json.dumps(commands))
        self.call("setChatMenuButton", menu_button=json.dumps({"type": "commands"}))

    def markup(self, token: str, data: dict) -> dict:
        selected = set(data["selected"])
        rows = [[{"text": f"{'✅' if i in selected else '☐'} {book_label(item) if data['kind'] == 'book' else item['title']}", "callback_data": f"toggle:{token}:{i}"}] for i, item in enumerate(data["items"])]
        rows += [[{"text": "Add selected", "callback_data": f"confirm:{token}"}, {"text": "Add all", "callback_data": f"all:{token}"}], [{"text": "Cancel", "callback_data": f"cancel:{token}"}]]
        return {"inline_keyboard": rows}

    def choices(self, chat_id: int, token: str, data: dict) -> None:
        text = "Google Books found several matches. Select the edition(s) to save:" if data["kind"] == "book" else "I found several possible book titles. Select one or more:"
        self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=json.dumps(self.markup(token, data)))

    def edit_choices(self, chat_id: int, message_id: int, token: str, data: dict) -> None:
        self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=json.dumps(self.markup(token, data)))

    def send_menu(self, chat_id: int, text: str = "Choose an option:") -> None:
        self.send(chat_id, text, {"inline_keyboard": [[{"text":"📚 All books","callback_data":"menu:list"},{"text":"📖 To read","callback_data":"menu:unread"}], [{"text":"✅ Read","callback_data":"menu:read"},{"text":"⭐ Rate a book","callback_data":"menu:rate"}], [{"text":"❓ Help","callback_data":"menu:help"}]]})


def google_books(title: str) -> list[dict]:
    params = {"q": f"intitle:{title}", "maxResults": 5, "printType": "books"}
    if os.getenv("GOOGLE_BOOKS_API_KEY"):
        params["key"] = os.getenv("GOOGLE_BOOKS_API_KEY")
    query = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"https://www.googleapis.com/books/v1/volumes?{query}", timeout=20) as response:
            items = json.loads(response.read()).get("items", [])
        books = []
        for item in items:
            info = item.get("volumeInfo", {})
            name = info.get("title")
            if name:
                date = info.get("publishedDate", "")
                images = info.get("imageLinks", {})
                cover = images.get("thumbnail") or images.get("smallThumbnail")
                books.append({"title": name, "authors": ", ".join(info.get("authors", [])), "year": int(date[:4]) if date[:4].isdigit() else None, "book_id": item.get("id"), "description": info.get("description") or "", "cover_url": cover.replace("http://", "https://", 1) if cover else None, "online_rating": info.get("averageRating")})
        return books
    except Exception:
        LOG.exception("Google Books search failed")
        return []


def list_text(store: BookStore, chat_id: int, read: bool | None = None) -> str:
    books = store.list(chat_id, read)
    if not books:
        return "No books here yet. Send me a screenshot to add one."
    heading = "Your books" if read is None else ("Books read" if read else "Books to read")
    return heading + ":\n" + "\n".join(f"{i}. {book_label(book)}" + (f" — rated {book['rating']}/10" if book["rating"] else "") for i, book in enumerate(books, 1))


def send_book_list(bot: BookTelegram, store: BookStore, chat_id: int, read: bool | None = None) -> None:
    bot.send(chat_id, list_text(store, chat_id, read))
    if read is None:
        books = store.list(chat_id)
        if books:
            keyboard = [[{"text": f"ℹ️ {i}. {book['title'][:34]}", "callback_data": f"info:{i}"}] for i, book in enumerate(books[:20], 1)]
            bot.send(chat_id, "Select a book for its cover, description, and rating:", {"inline_keyboard": keyboard})


def send_book_detail(bot: BookTelegram, store: BookStore, chat_id: int, index: int) -> None:
    book = store.details(chat_id, index)
    if not book:
        bot.send(chat_id, "I couldn’t find that book number. Use /list first.")
        return
    if book.get("book_id") and not (book.get("cover_url") or book.get("description") or book.get("online_rating") is not None):
        try:
            with urllib.request.urlopen(f"https://www.googleapis.com/books/v1/volumes/{book['book_id']}", timeout=20) as response:
                payload = json.loads(response.read())
            info, images = payload.get("volumeInfo", {}), payload.get("volumeInfo", {}).get("imageLinks", {})
            cover = images.get("thumbnail") or images.get("smallThumbnail")
            store.update_metadata(chat_id, index, {"description": info.get("description") or "", "cover_url": cover.replace("http://", "https://", 1) if cover else None, "online_rating": info.get("averageRating")})
            book = store.details(chat_id, index) or book
        except Exception:
            LOG.exception("Google Books detail lookup failed")
    text = book_label(book)
    if book.get("online_rating") is not None:
        text += f"\n\nGoogle Books community rating: {float(book['online_rating']):.1f}/5"
    if book.get("description"):
        text += "\n\n" + book["description"]
    if book.get("cover_url"):
        try:
            bot.send_photo(chat_id, book["cover_url"], text)
            return
        except Exception:
            LOG.exception("Could not send Google Books cover")
    bot.send(chat_id, text)


def stats_text(store: BookStore, chat_id: int) -> str:
    total, read, average, tags = store.stats(chat_id)
    if not total: return "No reading statistics yet."
    return f"Reading stats:\nTotal: {total}\nRead: {read}\nTo read: {total - read}" + (f"\nYour average rating: {average:.1f}/10" if average is not None else "") + (f"\nTop tags: {', '.join('#' + tag.replace(' ', '_') for tag in tags)}" if tags else "")


def help_text() -> str:
    return ("Send a book screenshot; I’ll find the title amid the extra text.\n\n/list — all books\n/read — books read\n/unread — books to read\n/read <number> — mark read\n/unread <number> — mark unread\n/rate <number> <1-10> — rate a book\n/add <book title> — add a book by typing its title\n/tag <number> <tag> — add a tag\n/tag <tag> — filter by a tag\n/search <words> — search your list\n/info <number> — cover, description, and online rating\n/remove <number> — remove a book\n/edit <number> <title> — rename a book\n/stats — reading statistics\n/group — shared-list help\n/clear — clear your list\n/menu — show options")


def save_or_choose(bot: BookTelegram, store: BookStore, chat_id: int, titles: list[str], tags: list[str]) -> None:
    books = []
    for title in titles:
        books.extend(google_books(title) or [{"title": title}])
    for book in books:
        book["tags"] = tags
    if len(books) > 1:
        token = store.pending(chat_id, books[:12], "book", tags)
        bot.choices(chat_id, token, store.pending_data(chat_id, token))
        return
    added = store.add(chat_id, books)
    bot.send(chat_id, "Added:\n" + "\n".join(f"• {item}" for item in added) if added else "That book is already on your list.")


def handle_callback(bot: BookTelegram, store: BookStore, callback: dict) -> None:
    message = callback.get("message", {})
    chat_id, data = (message.get("chat") or {}).get("id"), callback.get("data", "")
    if chat_id is None or ":" not in data:
        return
    action, token, *rest = data.split(":")
    if action == "info" and token.isdigit():
        send_book_detail(bot, store, chat_id, int(token)); bot.answer_callback(callback["id"], "Opening book details."); return
    if action == "menu":
        if token == "list": send_book_list(bot, store, chat_id)
        elif token == "read": bot.send(chat_id, list_text(store, chat_id, True))
        elif token == "unread": bot.send(chat_id, list_text(store, chat_id, False))
        elif token == "rate": bot.send(chat_id, "Use /list, then /rate <number> <1-10>.")
        else: bot.send_menu(chat_id, help_text())
        bot.answer_callback(callback["id"], "Done")
        return
    if action == "cancel":
        store.discard(chat_id, token); bot.answer_callback(callback["id"], "Cancelled."); return
    if action == "toggle" and rest and rest[0].isdigit():
        pending = store.toggle(chat_id, token, int(rest[0]))
        if pending:
            bot.edit_choices(chat_id, message["message_id"], token, pending); bot.answer_callback(callback["id"], "Selection updated.")
        else: bot.answer_callback(callback["id"], "That choice has expired.")
        return
    preview = store.pending_data(chat_id, token)
    if action == "confirm" and preview is not None and not preview["selected"]:
        bot.answer_callback(callback["id"], "Select at least one first."); return
    pending = store.take(chat_id, token, all_items=action == "all")
    if not pending:
        bot.answer_callback(callback["id"], "That choice has expired."); return
    if pending["kind"] == "title":
        save_or_choose(bot, store, chat_id, [item["title"] for item in pending["items"]], pending["tags"])
    else:
        for book in pending["items"]: book["tags"] = pending["tags"]
        added = store.add(chat_id, pending["items"])
        bot.send(chat_id, "Added:\n" + "\n".join(f"• {item}" for item in added) if added else "Those books are already on your list.")
    bot.answer_callback(callback["id"], "Saved.")


def handle(bot: BookTelegram, store: BookStore, message: dict) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None: return
    text = (message.get("text") or "").strip()
    parts, command = text.split(), ""
    if parts: command = parts[0].split("@", 1)[0]
    if command in ("/start", "/help", "/menu"): bot.send_menu(chat_id, help_text()); return
    if command == "/list": send_book_list(bot, store, chat_id); return
    if command == "/read" and len(parts) == 1: bot.send(chat_id, list_text(store, chat_id, True)); return
    if command == "/unread" and len(parts) == 1: bot.send(chat_id, list_text(store, chat_id, False)); return
    if command in ("/read", "/unread") and len(parts) == 2 and parts[1].isdigit():
        title = store.mark_read(chat_id, int(parts[1]), command == "/read"); bot.send(chat_id, f"Marked “{title}” as {'read' if command == '/read' else 'unread'}" if title else "I couldn’t find that book number."); return
    if command == "/rate" and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit() and 1 <= int(parts[2]) <= 10:
        title = store.rate(chat_id, int(parts[1]), int(parts[2])); bot.send(chat_id, f"Rated “{title}” {parts[2]}/10." if title else "I couldn’t find that book number."); return
    if command == "/info" and len(parts) == 2 and parts[1].isdigit():
        send_book_detail(bot, store, chat_id, int(parts[1])); return
    if command == "/remove" and len(parts) == 2 and parts[1].isdigit():
        title = store.remove(chat_id, int(parts[1])); bot.send(chat_id, f"Removed “{title}”." if title else "I couldn’t find that book number."); return
    if command == "/edit" and len(parts) >= 3 and parts[1].isdigit():
        title = text_entry_title(" ".join(parts[2:])); edited = store.edit(chat_id, int(parts[1]), title) if title else None; bot.send(chat_id, f"Renamed the book to “{edited}”." if edited else "I couldn’t edit that book. Check the number and title, or it may already be on your list."); return
    if command == "/search" and len(parts) >= 2:
        books = store.list(chat_id, query_text=" ".join(parts[1:])); bot.send(chat_id, "Search results:\n" + "\n".join(f"{i}. {book_label(book)}" for i, book in enumerate(books, 1)) if books else "No matching books."); return
    if command == "/tag" and len(parts) == 2:
        books = store.list(chat_id, tag=parts[1].lstrip("#")); bot.send(chat_id, "Tagged books:\n" + "\n".join(f"{i}. {book_label(book)}" for i, book in enumerate(books, 1)) if books else "No books have that tag."); return
    if command == "/tag" and len(parts) >= 3 and parts[1].isdigit():
        tag, title = " ".join(parts[2:]).lstrip("#"), store.tag(chat_id, int(parts[1]), " ".join(parts[2:]).lstrip("#")); bot.send(chat_id, f"Added #{tag.replace(' ', '_')} to “{title}”." if title else "I couldn’t find that book number."); return
    if command == "/stats": bot.send(chat_id, stats_text(store, chat_id)); return
    if command == "/group": bot.send(chat_id, "In a Telegram group, every member uses the same shared book list for that group. In a private chat, your list stays private."); return
    if command == "/clear": store.clear(chat_id); bot.send(chat_id, "Cleared your book list."); return
    if command == "/add":
        title = text_entry_title(" ".join(parts[1:]))
        if title: save_or_choose(bot, store, chat_id, [title], caption_tags(text))
        else: bot.send(chat_id, "Use /add followed by a book title. Example: /add The Left Hand of Darkness")
        return
    file_id = (message.get("photo") or [{}])[-1].get("file_id")
    document = message.get("document") or {}
    if not file_id and document.get("mime_type", "").startswith("image/"): file_id = document.get("file_id")
    if not file_id:
        if text:
            title = text_entry_title(text)
            if title: save_or_choose(bot, store, chat_id, [title], caption_tags(text))
            else: bot.send_menu(chat_id, "I didn’t understand that. Send a book screenshot, type a book title, or choose an option:")
        return
    with tempfile.TemporaryDirectory(prefix="bookbot-") as temp:
        path = str(Path(temp) / "screenshot")
        bot.download_file(file_id, path)
        caption = message.get("caption", "")
        titles = extract_titles(caption)
        ai = gemini_titles(path, caption, "book") or grok_titles(path, caption, "book") or vision_titles(path, caption, "book")
        if ai: titles = list(dict.fromkeys(ai + titles))
        elif not any(os.getenv(key) for key in ("GEMINI_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY")):
            try: titles = list(dict.fromkeys(titles + extract_titles(ocr(path))))
            except Exception: LOG.exception("OCR failed")
        tags = caption_tags(caption)
    if len(titles) > 1:
        token = store.pending(chat_id, [{"title": title} for title in titles[:6]], "title", tags); bot.choices(chat_id, token, store.pending_data(chat_id, token))
    elif titles: save_or_choose(bot, store, chat_id, titles, tags)
    else: bot.send(chat_id, "I couldn’t confidently identify a book title. Add it in the caption and try again.")


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("CINESNAP_BOOK_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOOK_TOKEN")
    if not token: raise SystemExit("Set CINESNAP_BOOK_TELEGRAM_TOKEN before starting the bot.")
    bot, store, offset = BookTelegram(token), BookStore(os.getenv("BOOK_DB", "/data/books.sqlite3")), 0
    try: bot.configure_menu()
    except Exception: LOG.exception("Could not configure Telegram command menu; continuing")
    LOG.info("Book list bot is running")
    while True:
        try: updates = bot.call("getUpdates", offset=offset, timeout=25, allowed_updates='["message","callback_query"]')
        except Exception: LOG.exception("getUpdates failed; retrying in 5 seconds"); time.sleep(5); continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if update.get("callback_query"): handle_callback(bot, store, update["callback_query"])
                else: handle(bot, store, update.get("message", {}))
            except Exception: LOG.exception("Could not process update %s", update.get("update_id"))


if __name__ == "__main__": main()
