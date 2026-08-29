# Telegram Chat Exporter

Local, single-file Python CLI that exports **private one-to-one user chats**
from the Telegram account you authenticate yourself.

It excludes groups, supergroups, channels, bots, and your own Saved Messages
chat. Exported data is written only to the local `exports/` directory.

## Features

- Telethon User API login with phone, login code, and optional 2FA password.
- Local session reuse after the first successful login.
- Export all private chats, one selected person, a date range, or text-only
  messages.
- Per-chat `messages.txt` and `messages.json`.
- Media markers such as `[PHOTO]`, `[VIDEO]`, and `[VOICE MESSAGE]`.
- Media downloading disabled by default; enable with `DOWNLOAD_MEDIA=true`.
- Streaming export instead of loading a whole chat into RAM.
- FloodWait and transient network retry handling.
- Interrupted exports keep `.part` files and resume on the next run when the
  same filters are selected.

## Install

```bash
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your own API credentials:

```text
API_ID=12345678
API_HASH=your_api_hash
```

Create API credentials at <https://my.telegram.org>. Do not put these values
in source code or commit `.env`.

`API_ID` aynan **my.telegram.org/apps** sahifasidagi raqam bo‘lishi kerak.
Bot tokeni, telefon raqami yoki boshqa uzun ID’ni `API_ID` sifatida kiritmang.
Telegram API ID signed 32-bit integer diapazonida bo‘ladi
(`1`–`2147483647`).

## Run

```bash
python telegram_exporter.py
```

On the first run the program asks for:

1. Phone number.
2. Telegram login code.
3. 2FA password, if enabled.

After login, Telethon creates `telegram_exporter.session`. This file is
equivalent to an authenticated local session: keep it private, never upload
it, and never commit it to Git.

## Output

```text
exports/
└── Person Name/
    ├── messages.txt
    └── messages.json
```

The JSON file contains an array of message records with message ID, date,
sender, sender ID, text, reply ID, and media marker. Date filtering uses the
computer's local timezone.

## Security

This tool is intended only for your own Telegram account and does not send
exported messages to a third-party server. Use it only where you have the
right to access and retain the data. Protect the generated session and
exported files like private account data.

## Troubleshooting

If you see `struct.error: 'i' format requires ...`, check `API_ID` in `.env`.
Replace it with the numeric API ID from <https://my.telegram.org/apps>, then
run the program again. Never share your `API_HASH` or session file publicly.