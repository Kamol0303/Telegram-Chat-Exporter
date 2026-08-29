#!/usr/bin/env python3
"""
Telegram Chat Exporter

Exports private one-to-one Telegram chats from the currently authenticated
Telegram user account. Data is written only to the local exports/ directory.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from telethon import TelegramClient, errors
from telethon.tl.types import User
from telethon.utils import get_display_name


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_TITLE = "TELEGRAM CHAT EXPORTER"
SESSION_NAME = os.getenv("SESSION_NAME", "telegram_exporter")
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "exports"))
DOWNLOAD_MEDIA = os.getenv("DOWNLOAD_MEDIA", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_BASE_SECONDS = float(os.getenv("RETRY_BASE_SECONDS", "3"))


def load_local_env(path: Path = Path(".env")) -> None:
    """Load a minimal .env file without requiring python-dotenv."""
    if not path.exists():
        return

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"⚠ Could not read {path}: {exc}")
        return

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> tuple[int, str]:
    """Read API credentials from the environment or local .env file."""
    global SESSION_NAME, EXPORT_DIR, DOWNLOAD_MEDIA, MAX_RETRIES
    global RETRY_BASE_SECONDS
    load_local_env()
    SESSION_NAME = os.getenv("SESSION_NAME", SESSION_NAME)
    EXPORT_DIR = Path(os.getenv("EXPORT_DIR", str(EXPORT_DIR)))
    DOWNLOAD_MEDIA = os.getenv("DOWNLOAD_MEDIA", str(DOWNLOAD_MEDIA)).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", str(MAX_RETRIES)))
    RETRY_BASE_SECONDS = float(
        os.getenv("RETRY_BASE_SECONDS", str(RETRY_BASE_SECONDS))
    )
    raw_api_id = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()

    if not raw_api_id or not api_hash:
        raise RuntimeError(
            "API_ID va API_HASH topilmadi. .env faylini yarating yoki "
            "environment variable sifatida belgilang."
        )

    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise RuntimeError("API_ID faqat raqam bo‘lishi kerak.") from exc

    if not 1 <= api_id <= 2_147_483_647:
        raise RuntimeError(
            "API_ID noto‘g‘ri diapazonda. my.telegram.org/apps saytidagi "
            "API ID qiymatini kiriting; u 1 dan 2147483647 gacha bo‘lishi kerak."
        )
    return api_id, api_hash


# ---------------------------------------------------------------------------
# Terminal UI and small utilities
# ---------------------------------------------------------------------------

def print_banner() -> None:
    print(
        "\n"
        "╔══════════════════════════════════════════╗\n"
        "║        TELEGRAM CHAT EXPORTER            ║\n"
        "║             v1.0                         ║\n"
        "╚══════════════════════════════════════════╝"
    )


def print_success(message: str) -> None:
    print(f"✓ {message}")


def print_warning(message: str) -> None:
    print(f"⚠ {message}")


def print_error(message: str) -> None:
    print(f"✗ {message}")


def prompt_choice(prompt: str, minimum: int, maximum: int) -> int:
    while True:
        try:
            value = int(input(prompt).strip())
            if minimum <= value <= maximum:
                return value
        except (EOFError, KeyboardInterrupt):
            raise
        except ValueError:
            pass
        print_warning(f"{minimum} dan {maximum} gacha bo‘lgan raqam kiriting.")


def sanitize_filename(value: str, fallback: str = "Unknown") -> str:
    """Make a readable, cross-platform-safe folder name."""
    value = value.strip()
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*\x7f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    return value[:80].rstrip(" .") or fallback


def display_name(user: User) -> str:
    if getattr(user, "deleted", False):
        return f"Unknown_{user.id}"
    name = get_display_name(user).strip()
    return name or f"Unknown_{user.id}"


def username_text(user: User) -> str:
    return f"@{user.username}" if user.username else "—"


def chat_folder_name(user: User, used: set[str]) -> str:
    base = sanitize_filename(display_name(user), f"Unknown_{user.id}")
    candidate = base
    if candidate in used:
        candidate = sanitize_filename(f"{base}_{user.id}", f"Unknown_{user.id}")
    used.add(candidate)
    return candidate


def parse_date(value: str, label: str) -> Optional[date]:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{label} noto‘g‘ri. Sana YYYY-MM-DD ko‘rinishida bo‘lsin."
        ) from exc


def message_local_date(message: Any) -> Optional[date]:
    if not message.date:
        return None
    return message.date.astimezone().date()


def in_date_range(message: Any, start: Optional[date], end: Optional[date]) -> bool:
    message_date = message_local_date(message)
    if message_date is None:
        return False
    if start and message_date < start:
        return False
    if end and message_date > end:
        return False
    return True


def date_range_signature(start: Optional[date], end: Optional[date]) -> str:
    return f"{start.isoformat() if start else ''}:{end.isoformat() if end else ''}"


# ---------------------------------------------------------------------------
# Telegram media and message formatting
# ---------------------------------------------------------------------------

def media_label(message: Any) -> Optional[str]:
    """Return a stable human-readable marker for non-text media."""
    if getattr(message, "photo", None):
        return "[PHOTO]"
    if getattr(message, "video", None):
        return "[VIDEO]"
    if getattr(message, "voice", None):
        return "[VOICE MESSAGE]"
    if getattr(message, "sticker", None):
        return "[STICKER]"
    if getattr(message, "gif", None):
        return "[GIF]"
    if getattr(message, "document", None):
        return "[DOCUMENT]"
    if getattr(message, "contact", None):
        return "[CONTACT]"
    if getattr(message, "geo", None) or getattr(message, "venue", None):
        return "[LOCATION]"
    if getattr(message, "media", None):
        return "[MEDIA]"
    return None


def message_text(message: Any) -> str:
    return (getattr(message, "message", None) or "").strip()


def sender_for_message(message: Any, other_user: User, me: User) -> str:
    if getattr(message, "out", False):
        return "MEN"
    sender = getattr(message, "sender", None)
    if isinstance(sender, User):
        return display_name(sender)
    if getattr(message, "sender_id", None) == me.id:
        return "MEN"
    return display_name(other_user)


def json_message(
    message: Any,
    other_user: User,
    me: User,
    downloaded_media: Optional[str] = None,
) -> dict[str, Any]:
    label = media_label(message)
    result: dict[str, Any] = {
        "message_id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "sender": sender_for_message(message, other_user, me),
        "sender_id": getattr(message, "sender_id", None),
        "text": message_text(message),
        "reply_to": getattr(message, "reply_to_msg_id", None),
        "media": label,
    }
    if downloaded_media:
        result["media_file"] = downloaded_media
    return result


def text_message(message: Any, other_user: User, me: User) -> str:
    body = message_text(message)
    label = media_label(message)
    sender = sender_for_message(message, other_user, me)
    timestamp = message.date.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    if body and label:
        body = f"{body}\n{label}"
    elif not body:
        body = label or "[EMPTY MESSAGE]"
    return f"[{timestamp}] {sender}:\n{body}\n"


# ---------------------------------------------------------------------------
# Chat discovery and resilient message iteration
# ---------------------------------------------------------------------------

async def get_private_chats(client: TelegramClient) -> list[User]:
    """Return non-bot, one-to-one user chats only."""
    me = await client.get_me()
    users: list[User] = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, User):
            continue
        if getattr(entity, "bot", False):
            continue
        if entity.id == me.id:
            continue
        users.append(entity)

    users.sort(key=lambda item: display_name(item).casefold())
    return users


def print_chat_list(chats: list[User]) -> None:
    if not chats:
        print_warning("Private 1-to-1 chat topilmadi.")
        return
    print("\nAvailable private chats:")
    for index, user in enumerate(chats, start=1):
        print(f"{index:>3}. {display_name(user)} — {username_text(user)}")


async def resilient_messages(
    client: TelegramClient,
    entity: User,
    minimum_id: int = 0,
) -> AsyncIterator[Any]:
    """
    Stream messages oldest-to-newest and recreate the iterator after a
    FloodWait or transient network failure.
    """
    last_id = minimum_id
    retries = 0

    while True:
        try:
            iterator = client.iter_messages(
                entity,
                limit=None,
                min_id=last_id,
                reverse=True,
                wait_time=0,
            )
            yielded = False
            async for message in iterator:
                if message.id <= last_id:
                    continue
                yielded = True
                last_id = message.id
                yield message
            return
        except errors.FloodWaitError as exc:
            retries = 0
            print_warning(f"Telegram FloodWait: {exc.seconds} soniya kutiladi...")
            await asyncio.sleep(exc.seconds)
        except (OSError, ConnectionError, asyncio.TimeoutError, errors.ServerError) as exc:
            retries += 1
            if retries > MAX_RETRIES:
                raise RuntimeError(
                    f"Network xatosi sabab {MAX_RETRIES} marta urinish tugadi: {exc}"
                ) from exc
            delay = RETRY_BASE_SECONDS * (2 ** (retries - 1))
            print_warning(
                f"Network xatosi. {delay:g} soniyadan keyin qayta uriniladi "
                f"({retries}/{MAX_RETRIES})..."
            )
            await asyncio.sleep(delay)
            if not client.is_connected():
                await client.connect()
        except errors.RPCError as exc:
            retries += 1
            if retries > MAX_RETRIES:
                raise RuntimeError(
                    f"Telegram RPC xatosi sabab eksport to‘xtadi: {exc}"
                ) from exc
            delay = RETRY_BASE_SECONDS * (2 ** (retries - 1))
            print_warning(f"Telegram xatosi. {delay:g} soniyadan keyin qayta urinish...")
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Streaming export and resumable checkpoints
# ---------------------------------------------------------------------------

def export_header(user: User) -> str:
    return (
        "=" * 60
        + f"\nCHAT: {display_name(user)}"
        + f"\nUSERNAME: {username_text(user)}"
        + f"\nUSER ID: {user.id}"
        + "\n"
        + "=" * 60
        + "\n\n"
    )


def read_progress(path: Path, signature: str) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("signature") == signature:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def save_progress(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(data, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def finalize_json(jsonl_path: Path, output_path: Path) -> None:
    """Convert streamed JSONL into the requested messages.json array."""
    temporary_path = output_path.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as output:
        output.write("[\n")
        first = True
        if jsonl_path.exists():
            with jsonl_path.open("r", encoding="utf-8") as source:
                for line in source:
                    line = line.strip()
                    if not line:
                        continue
                    if not first:
                        output.write(",\n")
                    output.write("  " + line)
                    first = False
        output.write("\n]\n")
    os.replace(temporary_path, output_path)


async def export_chat(
    client: TelegramClient,
    user: User,
    me: User,
    start: Optional[date] = None,
    end: Optional[date] = None,
    text_only: bool = False,
    folder_name: Optional[str] = None,
) -> dict[str, Any]:
    """Export one chat without holding all messages in RAM."""
    folder = EXPORT_DIR / (
        folder_name or sanitize_filename(display_name(user), f"Unknown_{user.id}")
    )
    folder.mkdir(parents=True, exist_ok=True)

    txt_part = folder / "messages.txt.part"
    jsonl_part = folder / "messages.jsonl.part"
    progress_path = folder / "export_progress.json"
    signature = "|".join(
        [
            date_range_signature(start, end),
            f"text_only={text_only}",
            f"download_media={DOWNLOAD_MEDIA}",
        ]
    )
    previous = read_progress(progress_path, signature)
    resume = bool(
        previous
        and txt_part.exists()
        and jsonl_part.exists()
        and previous.get("last_message_id", 0)
    )
    last_message_id = int(previous.get("last_message_id", 0)) if resume else 0
    mode = "a" if resume else "w"

    stats = {
        "total_messages": int(previous.get("total_messages", 0)) if resume else 0,
        "my_messages": int(previous.get("my_messages", 0)) if resume else 0,
        "their_messages": int(previous.get("their_messages", 0)) if resume else 0,
        "total_characters": int(previous.get("total_characters", 0)) if resume else 0,
        "photos": int(previous.get("photos", 0)) if resume else 0,
        "videos": int(previous.get("videos", 0)) if resume else 0,
        "voice_messages": int(previous.get("voice_messages", 0)) if resume else 0,
        "first_message": previous.get("first_message") if resume else None,
        "last_message": previous.get("last_message") if resume else None,
    }

    if resume:
        print(f"  ↻ Davom ettirilmoqda: {display_name(user)}")

    media_dir = folder / "media"
    if DOWNLOAD_MEDIA:
        media_dir.mkdir(parents=True, exist_ok=True)

    try:
        with txt_part.open(mode, encoding="utf-8") as txt_file, jsonl_part.open(
            mode, encoding="utf-8"
        ) as jsonl_file:
            if not resume:
                txt_file.write(export_header(user))

            async for message in resilient_messages(client, user, last_message_id):
                if not in_date_range(message, start, end):
                    message_date = message_local_date(message)
                    # Since iteration is oldest-to-newest, a message after the
                    # end date means all later messages are outside the range.
                    if end and message_date and message_date > end:
                        break
                    continue

                body = message_text(message)
                label = media_label(message)
                if text_only and not body:
                    continue

                downloaded_media: Optional[str] = None
                if DOWNLOAD_MEDIA and label:
                    try:
                        downloaded = await message.download_media(file=str(media_dir))
                        if downloaded:
                            downloaded_media = os.path.relpath(downloaded, folder)
                    except (OSError, ValueError, errors.RPCError) as exc:
                        print_warning(
                            f"Media yuklanmadi (message {message.id}): {exc}"
                        )

                record = json_message(message, user, me, downloaded_media)
                txt_file.write(text_message(message, user, me) + "\n")
                jsonl_file.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                txt_file.flush()
                jsonl_file.flush()

                stats["total_messages"] += 1
                stats["total_characters"] += len(body)
                if getattr(message, "out", False):
                    stats["my_messages"] += 1
                else:
                    stats["their_messages"] += 1
                if label == "[PHOTO]":
                    stats["photos"] += 1
                elif label == "[VIDEO]":
                    stats["videos"] += 1
                elif label == "[VOICE MESSAGE]":
                    stats["voice_messages"] += 1
                message_date = message_local_date(message)
                if message_date:
                    if not stats["first_message"]:
                        stats["first_message"] = message_date.isoformat()
                    stats["last_message"] = message_date.isoformat()

                last_message_id = message.id
                save_progress(
                    progress_path,
                    {"signature": signature, "last_message_id": last_message_id, **stats},
                )
    except KeyboardInterrupt:
        print_warning(
            f"{display_name(user)} to‘xtatildi. Keyingi ishga tushirishda davom etadi."
        )
        raise

    finalize_json(jsonl_part, folder / "messages.json")
    os.replace(txt_part, folder / "messages.txt")
    jsonl_part.unlink(missing_ok=True)
    progress_path.unlink(missing_ok=True)
    return stats


async def export_all(
    client: TelegramClient,
    chats: list[User],
    me: User,
    start: Optional[date] = None,
    end: Optional[date] = None,
    text_only: bool = False,
) -> None:
    if not chats:
        print_warning("Eksport qilish uchun chat yo‘q.")
        return

    print(f"\nExport boshlandi: {len(chats)} ta chat")
    used_folder_names: set[str] = set()
    for index, user in enumerate(chats, start=1):
        print(f"\n[{index}/{len(chats)}] {display_name(user)}")
        try:
            folder_name = chat_folder_name(user, used_folder_names)
            stats = await export_chat(
                client, user, me, start, end, text_only, folder_name
            )
            print_success(f"{stats['total_messages']:,} ta xabar saqlandi.")
        except errors.ChatAdminRequiredError:
            print_error(f"{display_name(user)} uchun ruxsat yetarli emas.")
        except errors.RPCError as exc:
            print_error(f"{display_name(user)} Telegram xatosi: {exc}")
        except OSError as exc:
            print_error(f"{display_name(user)} disk xatosi: {exc}")

    print_success(f"Export tugadi. Natija: {EXPORT_DIR.resolve()}")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

async def chat_statistics(
    client: TelegramClient, user: User, me: User
) -> dict[str, Any]:
    result = {
        "name": display_name(user),
        "username": username_text(user),
        "total_messages": 0,
        "my_messages": 0,
        "their_messages": 0,
        "first_message": None,
        "last_message": None,
        "total_characters": 0,
        "photos": 0,
        "videos": 0,
        "voice_messages": 0,
    }
    async for message in resilient_messages(client, user):
        result["total_messages"] += 1
        body = message_text(message)
        result["total_characters"] += len(body)
        if getattr(message, "out", False):
            result["my_messages"] += 1
        else:
            result["their_messages"] += 1
        label = media_label(message)
        if label == "[PHOTO]":
            result["photos"] += 1
        elif label == "[VIDEO]":
            result["videos"] += 1
        elif label == "[VOICE MESSAGE]":
            result["voice_messages"] += 1
        message_date = message_local_date(message)
        if message_date:
            if not result["first_message"]:
                result["first_message"] = message_date.isoformat()
            result["last_message"] = message_date.isoformat()
    return result


def print_statistics(stats: dict[str, Any]) -> None:
    print(
        "\nCHAT STATISTICS\n"
        "================\n"
        f"Name: {stats['name']}\n"
        f"Username: {stats['username']}\n"
        f"Total messages: {stats['total_messages']:,}\n"
        f"My messages: {stats['my_messages']:,}\n"
        f"Their messages: {stats['their_messages']:,}\n"
        f"First message: {stats['first_message'] or '—'}\n"
        f"Last message: {stats['last_message'] or '—'}\n"
        f"Total characters: {stats['total_characters']:,}\n"
        f"Photos: {stats['photos']:,}\n"
        f"Videos: {stats['videos']:,}\n"
        f"Voice messages: {stats['voice_messages']:,}\n"
    )


async def show_statistics(
    client: TelegramClient, chats: list[User], me: User
) -> None:
    if not chats:
        print_warning("Statistika uchun chat yo‘q.")
        return
    print("\nStatistika yig‘ilmoqda. Katta chatlarda bu vaqt olishi mumkin.")
    for index, user in enumerate(chats, start=1):
        print(f"[{index}/{len(chats)}] {display_name(user)}")
        try:
            stats = await chat_statistics(client, user, me)
            print_statistics(stats)
        except (errors.RPCError, RuntimeError) as exc:
            print_error(f"{display_name(user)}: {exc}")


# ---------------------------------------------------------------------------
# Authentication and menu
# ---------------------------------------------------------------------------

async def login(client: TelegramClient) -> User:
    try:
        await client.connect()
    except errors.ApiIdInvalidError as exc:
        raise RuntimeError("API_ID yoki API_HASH noto‘g‘ri.") from exc

    if await client.is_user_authorized():
        me = await client.get_me()
        print_success(f"Session orqali kirdingiz: {display_name(me)}")
        return me

    for attempt in range(1, 4):
        phone = input("Phone number (+998...): ").strip()
        try:
            await client.send_code_request(phone)
            code = getpass.getpass("Login code: ").strip()
            try:
                await client.sign_in(phone=phone, code=code)
            except errors.SessionPasswordNeededError:
                password = getpass.getpass("2FA password: ")
                await client.sign_in(password=password)
            me = await client.get_me()
            print_success(f"Logged in as: {display_name(me)}")
            print_success(f"Session saqlandi: {SESSION_NAME}.session")
            return me
        except errors.PhoneNumberInvalidError:
            print_error("Phone number noto‘g‘ri.")
        except errors.PhoneCodeInvalidError:
            print_error("Login code noto‘g‘ri.")
        except errors.PhoneCodeExpiredError:
            print_error("Login code muddati tugagan. Qayta urinib ko‘ring.")
        except errors.PasswordHashInvalidError:
            print_error("2FA password noto‘g‘ri.")
        except errors.ApiIdInvalidError as exc:
            raise RuntimeError("API_ID yoki API_HASH noto‘g‘ri.") from exc
        except errors.RPCError as exc:
            print_error(f"Telegram login xatosi: {exc}")
        if attempt < 3:
            print_warning(f"Qayta urinish: {attempt + 1}/3")

    raise RuntimeError("Login muvaffaqiyatsiz tugadi.")


async def ask_date_range() -> tuple[Optional[date], Optional[date]]:
    while True:
        try:
            start = parse_date(input("Start date (YYYY-MM-DD, blank = all): "), "Start date")
            end = parse_date(input("End date (YYYY-MM-DD, blank = all): "), "End date")
            if start and end and start > end:
                raise ValueError("Start date End date'dan keyin bo‘lishi mumkin emas.")
            return start, end
        except ValueError as exc:
            print_error(str(exc))


async def application(client: TelegramClient, me: User) -> None:
    chats = await get_private_chats(client)
    print_success(f"Private chats found: {len(chats)}")

    while True:
        print(
            "\n"
            "╔══════════════════════════════════════╗\n"
            "║      TELEGRAM CHAT EXPORTER          ║\n"
            "╠══════════════════════════════════════╣\n"
            "║ 1. Export ALL private chats          ║\n"
            "║ 2. Export ONE person                 ║\n"
            "║ 3. Export by date                    ║\n"
            "║ 4. Export text messages only         ║\n"
            "║ 5. Show statistics                   ║\n"
            "║ 0. Exit                              ║\n"
            "╚══════════════════════════════════════╝"
        )
        choice = prompt_choice("Select: ", 0, 5)
        if choice == 0:
            return
        if choice == 1:
            await export_all(client, chats, me)
        elif choice == 2:
            print_chat_list(chats)
            if chats:
                index = prompt_choice("Select person: ", 1, len(chats)) - 1
                await export_all(client, [chats[index]], me)
        elif choice == 3:
            start, end = await ask_date_range()
            await export_all(client, chats, me, start, end)
        elif choice == 4:
            await export_all(client, chats, me, text_only=True)
        elif choice == 5:
            await show_statistics(client, chats, me)


async def main() -> None:
    print_banner()
    try:
        api_id, api_hash = load_config()
        client = TelegramClient(SESSION_NAME, api_id, api_hash)
        try:
            me = await login(client)
            await application(client, me)
        finally:
            await client.disconnect()
    except KeyboardInterrupt:
        print("\n\nTo‘xtatildi. Mavjud `.part` fayllar keyingi ishga tushirishda davom ettiriladi.")
    except PermissionError as exc:
        print_error(f"Fayl yoki papkaga ruxsat yo‘q: {exc}")
    except RuntimeError as exc:
        print_error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())