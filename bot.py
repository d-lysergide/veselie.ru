#!/usr/bin/env python3
import os
import re
import sys
import tempfile
import subprocess
from io import BytesIO
from urllib.parse import urlparse, parse_qs

import requests
import validators
from PIL import Image, ImageOps
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WATERMARK_VESELIE = "VESELIE_RU_watermark_transparent.png"
WATERMARK_FRIKI   = "FRIKI_REDANA_18_plus_transparent.png"

# Видео: большая метка «летает» по центру
VIDEO_SCALE_W = 0.70
WAVE_TX = 6.0
WAVE_TY = 5.0
WAVE_AMPL_X = 0.25
WAVE_AMPL_Y = 0.25

# Фото: диагонально
PHOTO_ANGLE_DEG = 35
PHOTO_ALPHA_MULT = 1.0

# Сетевые лимиты
HTTP_TIMEOUT = 30          # секунд на запрос
MAX_HTTP_REDIRECTS = 5
CHUNK = 1024 * 512         # 512KB
ACCEPTED_CT = (
    "video/", "image/", "application/octet-stream"
)

# ========== УТИЛИТЫ ==========

def wm_exists(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Нет файла '{path}' рядом с bot.py")

def drive_direct(url: str) -> str | None:
    """
    Превращает drive-ссылки в прямую:
      - https://drive.google.com/file/d/<id>/view?...
      - https://drive.google.com/open?id=<id>
      - https://drive.google.com/uc?id=<id>&export=download (оставляем как есть)
    -> https://drive.usercontent.google.com/uc?id=<id>&export=download
    """
    u = urlparse(url)
    if u.netloc not in {"drive.google.com", "docs.google.com"}:
        return None

    # /file/d/<id>/view
    m = re.search(r"/file/d/([^/]+)/", u.path)
    if m:
        file_id = m.group(1)
        return f"https://drive.usercontent.google.com/uc?id={file_id}&export=download"

    # open?id=<id>
    q = parse_qs(u.query)
    if "id" in q and q["id"]:
        file_id = q["id"][0]
        return f"https://drive.usercontent.google.com/uc?id={file_id}&export=download"

    # уже прямая? оставим
    if u.path.startswith("/uc") and "id" in q:
        return url

    return None

def dropbox_direct(url: str) -> str | None:
    """
    Dropbox:
      - https://www.dropbox.com/s/<...>?dl=0 -> dl=1
      - https://www.dropbox.com/s/.. -> dl.dropboxusercontent.com/s/..
    """
    u = urlparse(url)
    if u.netloc not in {"www.dropbox.com", "dropbox.com"}:
        return None
    # Преобразуем хост
    direct = url.replace("www.dropbox.com", "dl.dropboxusercontent.com").replace("dropbox.com", "dl.dropboxusercontent.com")
    # Убедимся, что dl=1
    if "dl=" in direct:
        direct = re.sub(r"dl=\d", "dl=1", direct)
    elif "?" in direct:
        direct += "&dl=1"
    else:
        direct += "?dl=1"
    return direct

def normalize_url(url: str) -> str | None:
    """
    Возвращает прямой URL на файл если узнаем хост.
    Сейчас поддержка: Google Drive, Dropbox.
    Иначе — если URL валиден, вернем как есть.
    """
    url = url.strip().strip("<>")  # на случай, если Telegram обрамил
    if not validators.url(url):
        return None

    for fixer in (drive_direct, dropbox_direct):
        fixed = fixer(url)
        if fixed:
            return fixed
    return url

def looks_like_media(url: str) -> bool:
    """HEAD-запрос: проверим, что это точно файл и тип похож на медиа."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
        ct = r.headers.get("content-type", "").lower()
        return any(ct.startswith(p) for p in ACCEPTED_CT)
    except Exception:
        return False

def http_download(url: str, dest_path: str) -> None:
    """Потоковая загрузка в файл."""
    with requests.get(url, stream=True, timeout=HTTP_TIMEOUT, allow_redirects=True) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(CHUNK):
                if chunk:
                    f.write(chunk)

def ffmpeg_overlay_flying(in_path: str, out_path: str, wm_path: str) -> None:
    filter_str = (
        f"[1:v][0:v]scale2ref=w=iw*{VIDEO_SCALE_W}:h=ow/mdar[wm][vid];"
        f"[vid][wm]overlay="
        f"x=(W-w)/2 + (W*{WAVE_AMPL_X})*sin(2*PI*t/{WAVE_TX}):"
        f"y=(H-h)/2 + (H*{WAVE_AMPL_Y})*cos(2*PI*t/{WAVE_TY}):"
        f"format=auto"
    )
    cmd = [
        "ffmpeg","-y",
        "-i", in_path,
        "-i", wm_path,
        "-filter_complex", filter_str,
        "-map","0:a?",
        "-c:v","libx264","-preset","veryfast","-crf","23",
        "-c:a","copy",
        out_path
    ]
    subprocess.run(cmd, check=True)

def pil_overlay_diagonal(photo_bytes: bytes, wm_path: str) -> bytes:
    wm_exists(wm_path)
    base = Image.open(BytesIO(photo_bytes)).convert("RGBA")
    W, H = base.size

    wm = Image.open(wm_path).convert("RGBA")
    if PHOTO_ALPHA_MULT != 1.0:
        r,g,b,a = wm.split()
        a = a.point(lambda p: int(p * PHOTO_ALPHA_MULT))
        wm = Image.merge("RGBA",(r,g,b,a))

    import math
    diag = int(math.sqrt(W*W + H*H))
    scale = diag / wm.width
    wm = wm.resize((int(wm.width*scale), int(wm.height*scale)), Image.LANCZOS)
    wm = wm.rotate(PHOTO_ANGLE_DEG, expand=True, resample=Image.BICUBIC)

    x = (W - wm.width)//2
    y = (H - wm.height)//2

    out = base.copy()
    out.alpha_composite(wm, (x,y))
    buf = BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ========== UI ВЫБОР ВОДЯНОГО ЗНАКА ==========
WM_VESELIE = "wm_veselie"
WM_FRIKI   = "wm_friki"

def wm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ВЕСЕЛЬЕ.РУ", callback_data=WM_VESELIE)],
        [InlineKeyboardButton("ФРИКИ РЕДАНА 18+", callback_data=WM_FRIKI)],
    ])

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "Отправь видео/фото **или ссылку на видео**.\n\n"
        "• Видео: большая прозрачная метка по центру «летает».\n"
        "• Фото: метка диагонально на весь кадр.\n"
        "• Большие файлы: пришли ссылку — бот сам скачает.\n\n"
        "Выбери, какой знак ставить:",
        reply_markup=wm_keyboard()
    )

async def on_pick(u: Update, c: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query
    await query.answer()
    if query.data in (WM_VESELIE, WM_FRIKI):
        c.user_data["wm"] = query.data
        await query.edit_message_text(
            ("Метка: ВЕСЕЛЬЕ.РУ ✅" if query.data==WM_VESELIE else "Метка: ФРИКИ РЕДАНА 18+ ✅")
            + "\nТеперь пришли видео/фото **или ссылку на видео**."
        )

def chosen_wm_path(c: ContextTypes.DEFAULT_TYPE) -> str:
    code = c.user_data.get("wm", WM_VESELIE)
    return WATERMARK_VESELIE if code == WM_VESELIE else WATERMARK_FRIKI

# ========== ОБРАБОТКА МЕДИА И ССЫЛОК ==========
async def handle_photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    wm_path = chosen_wm_path(c); wm_exists(wm_path)
    photo = u.message.photo[-1] if u.message.photo else None
    if not photo and u.message.document:
        if not (u.message.document.mime_type or "").startswith("image"):
            return await u.message.reply_text("Пришли фото или image-документ.")
        tgfile = await u.message.document.get_file()
        raw = await tgfile.download_as_bytearray()
    else:
        if not photo:
            return await u.message.reply_text("Пришли фото.")
        tgfile = await photo.get_file()
        raw = await tgfile.download_as_bytearray()

    out_bytes = pil_overlay_diagonal(bytes(raw), wm_path)
    await u.message.reply_photo(photo=out_bytes, caption="Готово ✅")

async def _process_video_file(u: Update, c: ContextTypes.DEFAULT_TYPE, src_path: str):
    wm_path = chosen_wm_path(c); wm_exists(wm_path)
    status = await u.message.reply_text("Обрабатываю видео…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "out.mp4")
            ffmpeg_overlay_flying(src_path, dst, wm_path)
            await status.edit_text("Отправляю результат…")
            await u.message.reply_video(video=dst, caption="Готово ✅", supports_streaming=True)
    except subprocess.CalledProcessError:
        await status.edit_text("Ошибка ffmpeg при обработке.")
    except Exception as e:
        await status.edit_text(f"Не удалось обработать: {e!r}")

async def handle_video(u: Update, c: ContextTypes.DEFAULT_TYPE):
    # Прямое медиа из TG
    f = u.message.document or u.message.video
    if not f:
        return await u.message.reply_text("Пришли видео как файл/видео или ссылку.")
    status = await u.message.reply_text("Скачиваю видео…")
    tgfile = await f.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.mp4")
        await tgfile.download_to_drive(src)
    await status.delete()
    await _process_video_file(u, c, src)

URL_REGEX = re.compile(r"https?://\S+")

async def handle_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    Пытаемся найти в тексте ссылку → нормализуем → проверяем, что это файл → качаем → обработка.
    """
    if not u.message or not u.message.text:
        return
    text = u.message.text.strip()
    m = URL_REGEX.search(text)
    if not m:
        return await u.message.reply_text("Не нашла ссылку в сообщении 🥺 Пришли ссылку на файл видео.")
    raw_url = m.group(0)
    fixed = normalize_url(raw_url)
    if not fixed:
        return await u.message.reply_text("Ссылка выглядит криво. Пришли нормальный URL.")

    if not looks_like_media(fixed):
        return await u.message.reply_text("Ошибка: ссылка ведёт не на файл видео (или доступ закрыт).")

    status = await u.message.reply_text("Качаю по ссылке…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            http_download(fixed, src)
            await status.edit_text("Видео скачано. Обрабатываю…")
            await _process_video_file(u, c, src)
    except requests.HTTPError as e:
        await status.edit_text(f"HTTP ошибка при скачивании: {e.response.status_code}")
    except Exception as e:
        await status.edit_text(f"Не получилось скачать/прочитать видео: {e!r}")

# ========== MAIN ==========
def main():
    if not BOT_TOKEN:
        print("Нет BOT_TOKEN. Задай переменную окружения.", file=sys.stderr)
        raise SystemExit(1)

    for p in (WATERMARK_VESELIE, WATERMARK_FRIKI):
        wm_exists(p)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=f"^{WM_VESELIE}$|^{WM_FRIKI}$"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    print("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
