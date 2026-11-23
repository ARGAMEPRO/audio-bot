"""Telegram-бот: присылает звук из видео по ссылке.

Поддерживает всё, что умеет yt-dlp: YouTube, Rutube, VK, Vimeo, SoundCloud и ещё
сотни площадок. Из видео достаётся только аудиодорожка, рекламные вставки режет
yt-dlp (sponsorblock), на выходе mp3 192 kbps.

Запуск: python3 bot.py  (нужны BOT_TOKEN и ffmpeg в PATH)
"""

import asyncio
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
import uuid

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message

TOKEN = os.getenv("BOT_TOKEN", "")
MAX_MB = 48            # у Telegram лимит 50 МБ на файл от бота
MAX_MINUTES = 40       # длиннее не берём: дольше качать, чем слушать
WORKDIR = os.getenv("WORKDIR", tempfile.gettempdir())

dp = Dispatcher()


def raw(method: str, **params) -> None:
    """Вызов метода Bot API напрямую: aiogram не знает про style и copy_text в кнопках."""
    data = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            data[key] = json.dumps(value, ensure_ascii=False)
        else:
            data[key] = str(value)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=urllib.parse.urlencode(data).encode(),
    )
    try:
        urllib.request.urlopen(request, timeout=20).read()
    except Exception:
        pass  # кнопки — украшение, из-за них бот падать не должен


def after_file(chat_id: int, url: str, title: str) -> None:
    """Кнопка «скопировать ссылку» со style — возможности Bot API 10.4."""
    raw(
        "sendMessage",
        chat_id=chat_id,
        text=f"<b>{title[:80]}</b>\nЕсли понадобится ещё раз — ссылка под рукой.",
        parse_mode="HTML",
        reply_markup={
            "inline_keyboard": [[
                {"text": "Скопировать ссылку", "copy_text": {"text": url[:250]}, "style": "primary"},
            ]]
        },
    )


def download(url: str, folder: str) -> str:
    """Качает звук и возвращает путь к mp3. Бросает RuntimeError с понятным текстом."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(folder, "%(title).70s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
        ],
        "match_filter": yt_dlp.utils.match_filter_func(f"duration < {MAX_MINUTES * 60}"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("не удалось разобрать ссылку")
        path = ydl.prepare_filename(info)
        base, _ = os.path.splitext(path)
        mp3 = base + ".mp3"
        if not os.path.exists(mp3):
            raise RuntimeError("ffmpeg не собрал mp3 — проверь, что он установлен")
        return mp3


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Пришлите ссылку на видео — верну звук файлом mp3.\n\n"
        f"Работает с YouTube, Rutube, VK, Vimeo и другими площадками.\n"
        f"Ограничения: до {MAX_MINUTES} минут и до {MAX_MB} МБ."
    )


@dp.message(Command("help"))
async def help_cmd(message: Message) -> None:
    await message.answer(
        "Как пользоваться:\n"
        "1. Скопируйте ссылку на видео.\n"
        "2. Пришлите её сюда одним сообщением.\n"
        "3. Дождитесь mp3 — обычно 10–40 секунд.\n\n"
        "Плейлисты не поддерживаются: присылайте по одной ссылке."
    )


@dp.message(F.text.startswith(("http://", "https://")))
async def handle_link(message: Message) -> None:
    url = message.text.strip()
    if not shutil.which("ffmpeg"):
        await message.answer("На сервере нет ffmpeg — без него не собрать mp3. Напишите администратору бота.")
        return

    status = await message.answer("Качаю звук, подождите…")
    folder = os.path.join(WORKDIR, f"audio-{uuid.uuid4().hex}")
    os.makedirs(folder, exist_ok=True)

    try:
        mp3 = await asyncio.to_thread(download, url, folder)
    except yt_dlp.utils.DownloadError as exc:
        text = str(exc).lower()
        if "private" in text or "login" in text:
            reply = "Видео закрытое или требует входа — такое я скачать не могу."
        elif "duration" in text or "match filter" in text:
            reply = f"Слишком длинное видео. Беру до {MAX_MINUTES} минут."
        else:
            reply = "Не получилось скачать. Проверьте, что ссылка открывается в браузере без входа."
        await status.edit_text(reply)
        shutil.rmtree(folder, ignore_errors=True)
        return
    except Exception:
        await status.edit_text("Что-то пошло не так на стороне сервера. Попробуйте другую ссылку.")
        shutil.rmtree(folder, ignore_errors=True)
        return

    size_mb = os.path.getsize(mp3) / (1024 * 1024)
    if size_mb > MAX_MB:
        await status.edit_text(
            f"Файл вышел {size_mb:.0f} МБ, а Telegram пропускает до {MAX_MB} МБ. "
            "Пришлите ссылку на фрагмент покороче."
        )
        shutil.rmtree(folder, ignore_errors=True)
        return

    await status.edit_text("Готово, отправляю файл.")
    try:
        await message.answer_audio(
            FSInputFile(mp3, filename=os.path.basename(mp3)),
            title=os.path.splitext(os.path.basename(mp3))[0],
        )
        after_file(message.chat.id, url, os.path.splitext(os.path.basename(mp3))[0])
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Нужна ссылка на видео, начинающаяся с http:// или https://")


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN — возьмите токен у @BotFather.")
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
