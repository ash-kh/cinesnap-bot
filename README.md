# Telegram movie-list bot

Send the bot screenshots containing movie names—even when the screenshot also has descriptions, ratings, buttons, or other interface text. It checks the Telegram caption first, then runs OCR on the image, filters out likely UI/description lines, and stores a deduplicated list per Telegram chat.

## Run locally

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Install the system dependency:

   ```bash
   brew install tesseract
   ```

3. Start it:

   ```bash
   export CINESNAP_TELEGRAM_TOKEN="123456:replace-me"
   python3 moviebot.py
   ```

On macOS, this is enough to run the bot in a terminal. Keep that terminal open, or use a process manager for continuous uptime.

The SQLite database is `movies.sqlite3` by default. Set `MOVIE_DB` to change its location. Images can be sent as Telegram photos or image documents. If several title-shaped lines are found, the bot asks which one to add (or lets you add all).

For difficult screenshots, CineSnap uses vision providers in this order: Gemini, Grok, OpenAI, then local OCR. Add `GEMINI_API_KEY` to use Gemini first (the default model is `gemini-3.5-flash-lite`; override it with `GEMINI_MODEL`). If Gemini is unavailable or returns no title, it tries `XAI_API_KEY`, then `OPENAI_API_KEY`. Both Gemini and Grok support image input; their free tiers and usage limits vary by account and model.

Commands:

- `/list` — show every movie, with ratings
- `/seen` — show movies marked seen
- `/unseen` — show movies not yet seen
- `/seen 3` or `/unseen 3` — change the status of movie 3 from `/list`
- `/rate 3 8` — rate movie 3 from 1–10 and mark it seen
- `/clear` — remove all movies
- `/status` — show which vision providers are configured (never shows keys)
- `/menu` — show an interactive menu of common actions

## Deployment

Run the process on any small always-on Linux host with persistent storage. This bot uses long polling, so it does not need a public URL or webhook. A small VPS, home server, or Docker host works.

### Docker

```bash
docker build -t movie-list-bot .
docker volume create movie-list-data
docker run -d --name movie-list-bot --restart unless-stopped \
  -e CINESNAP_TELEGRAM_TOKEN="123456:replace-me" \
  -v movie-list-data:/data \
  movie-list-bot
```

View logs with `docker logs -f movie-list-bot`. The SQLite database is stored in the persistent `/data` volume.

### Directly on a Linux VPS

Install Python 3.10+, Tesseract, and Git, copy this directory to the server, then run:

```bash
export CINESNAP_TELEGRAM_TOKEN="123456:replace-me"
python3 moviebot.py
```

For continuous uptime, run it with `systemd`, `supervisord`, or `tmux`, and back up `movies.sqlite3`.

## Limitations

The first version uses line-based OCR heuristics. Screenshots with several unrelated text elements may need a crop or manual cleanup. The next upgrade could add an approval step, movie database matching, or an LLM vision fallback for harder screenshots.
