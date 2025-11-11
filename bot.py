#!/usr/bin/env python3
import os
import re
import math
import time
import tempfile
import subprocess
from io import BytesIO

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Пути к PNG-водяным знакам (должны лежать рядом с bot.py)
WATERMARKS = {
    "ve": ("ВЕСЕЛЬЕ.РУ", "VESELIE_RU_watermark_transparent.png"),
    "fr": ("ФРИКИ РЕДАНА 18+", "FRIKI_REDANA_18_plus_transparent.png"),
}

# Параметры «летающей» марки для видео
VIDEO_SCALE_W = 0.70
WAVE_TX, WAVE_TY = 6.0, 5.0
WAVE_AMPL_X, WAVE_AMPL_Y = 0.25, 0.25

# Параметры диагональной марки для фото
PHOTO_ANGLE_DEG = 35
PHOTO_ALPHA_MULT = 1.0

# Скачивание по URL (лимиты/таймауты)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB «на всякий», можно уменьшить
HTTP_TIMEOUT = 30
CHUNK = 1024 * 1024

# Память выбора марки по чату
user_choice: dict[int, str] = {}  # chat_id -> key ("ve"/"fr")
# ==================================================


# ---------- утилиты водяных знаков ----------
def ensure_wm_exists(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Не найден watermark '{path}' рядом с bot.py")

def get_wm_path(key: str) -> str:
    key = key if key in WATERMARKS else "ve"
    _, fn = WATERMARKS[key]
    ensure_wm_exists(fn)
    return fn


# ---------- ffmpeg обработка ----------
def ffmpeg_overlay_flying(in_path: str, out_path: str, wm_path: str) -> None:
    filter_str = (
        f"[1:v][0:v]scale2ref=w=iw*{VIDEO_SCALE_W}:h=ow/mdar[wm][vid];"
        f"[vid][wm]overlay="
        f"x=(W-w)/2 + (W*{WAVE_AMPL_X})*sin(2*PI*t/{WAVE_TX}):"
        f"y=(H-h)/2 + (H*{WAVE_AMPL_Y})*cos(2*PI*t/{WAVE_TY}):"
        f"format=auto"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-i", in_path, "-i", wm_path,
        "-filter_complex", filter_str,
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        out_path
    ]
    subprocess.run(cmd, check=True)


def pil_overlay_diagonal(photo_bytes: bytes, wm_path: str) -> bytes:
    ensure_wm_exists(wm_path)
    base = Image.open(BytesIO(photo_bytes)).convert("RGBA")
    W, H = base.size

    wm = Image.open(wm_path).convert("RGBA")
    if PHOTO_ALPHA_MULT != 1.0:
        r, g, b, a = wm.split()
        a = a.point(lambda p: int(p * PHOTO_ALPHA_MULT))
        wm = Image.merge("RGBA", (r, g, b, a))

    diag = int(math.sqrt(W * W + H * H))
    scale = diag / wm.width
    wm = wm.resize((int(wm.width * scale), int(wm.height * scale)), Image.LANCZOS)
    wm = wm.rotate(PHOTO_ANGLE_DEG, expand=True, resample=Image.BICUBIC)

    x = (W - wm.width) // 2
    y = (H - wm.height) // 2

    out = base.copy()
    out.alpha_composite(wm, (x, y))
    buf = BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


# ---------- нормализация ссылок ----------
URL_RE = re.compile(r'(https?://\S+)', re.IGNORECASE)

def normalize_url(url: str) -> str:
    """Чинит популярные шар-ссылки → прямые скачивания, если возможно."""
    u = url.strip()

    # Dropbox: https://www.dropbox.com/s/<id>/<name>?dl=0 → dl=1
    if "dropbox.com" in u:
        # вариант 1: подменить хост на dl.dropboxusercontent.com
        u = u.replace("www.dropbox.com", "dl.dropboxusercontent.com")
        u = re.sub(r"[?&]dl=\d", "", u)
        return u

    # Google Drive: https://drive.google.com/file/d/<ID>/view?… → прямой usercontent
    m = re.search(r"drive\.google\.com/file/d/([^/]+)/", u)
    if m:
        file_id = m.group(1)
        # usercontent линк стабильнее для больших файлов
        return f"https://drive.usercontent.google.com/uc?id={file_id}&export=download"

    # Если уже похоже на прямую ссылку на файл (mp4/mov/m4v/webm)
    if re.search(r"\.(mp4|mov|m4v|webm)(\?|$)", u, re.IGNORECASE):
        return u

    # Files.fm, Direct CDN и прочие — оставляем как есть (ffmpeg/requests разберутся)
    return u


def download_url_to_file(url: str, dest_path: str) -> None:
    """Качаем с нормализованного URL в файл (полностью), с лимитами."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TelegramWatermarkBot/1.0)"
    }
    with requests.get(url, headers=headers, stream=True, timeout=HTTP_TIMEOUT, allow_redirects=True) as r:
        r.raise_for_status()
        total = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Слишком большой файл (превысил лимит на сервере).")
                f.write(chunk)


# ---------- кнопки выбора водяного знака ----------
def wm_keyboard(current: str | None = None) -> InlineKeyboardMarkup:
    buttons = []
    for key, (title, _) in WATERMARKS.items():
        txt = f"{'✅ ' if current == key else ''}{title}"
        buttons.append([InlineKeyboardButton(txt, callback_data=f"wm:{key}")])
    return InlineKeyboardMarkup(buttons)


# =================== handlers ===================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_choice:
        user_choice[chat_id] = "ve"
    await update.message.reply_text(
        "Отправь видео/фото **или ссылку на видео**.\n\n"
        "• Видео: большая прозрачная метка по центру «летает».\n"
        "• Фото: метка диагонально на весь кадр.\n"
        "• Большие файлы: пришли ссылку — бот сам скачает.\n\n"
        "Выбери, какой знак ставить:",
        reply_markup=wm_keyboard(user_choice.get(chat_id))
    )


async def on_wm_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    _, key = q.data.split(":")
    user_choice[chat_id] = key
    await q.edit_message_text(
        f"Ок, выбран: {WATERMARKS[key][0]}.\nТеперь пришли видео/фото или ссылку.",
    )


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    key = user_choice.get(chat_id, "ve")
    wm_path = get_wm_path(key)

    photo = update.message.photo[-1] if update.message.photo else None
    if not photo and update.message.document:
        if not (update.message.document.mime_type or "").startswith("image"):
            return await update.message.reply_text("Пришли фото или image-документ.")
        tgfile = await update.message.document.get_file()
        raw = await tgfile.download_as_bytearray()
    else:
        if not photo:
            return await update.message.reply_text("Пришли фото.")
        tgfile = await photo.get_file()
        raw = await tgfile.download_as_bytearray()

    out_bytes = pil_overlay_diagonal(bytes(raw), wm_path)
    await update.message.reply_photo(photo=out_bytes, caption="Готово ✅")


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приём маленьких видео через Telegram (<= ~20 МБ)."""
    chat_id = update.effective_chat.id
    key = user_choice.get(chat_id, "ve")
    wm_path = get_wm_path(key)

    f = update.message.document or update.message.video
    if not f:
        return await update.message.reply_text("Пришли видео как файл (Document) или как Video.")

    # Telegram ограничивает скачивание файлов ботом примерно до 20 МБ
    if getattr(f, "file_size", 0) and f.file_size > 19 * 1024 * 1024:
        return await update.message.reply_text(
            "Файл слишком большой для скачивания через Bot API (> ~20 МБ).\n"
            "Пришли **ссылку** (dropbox/drive/прямая) на это видео — я сам скачаю и обработаю."
        )

    status = await update.message.reply_text("Скачиваю видео…")
    tgfile = await f.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.mp4")
        dst = os.path.join(tmp, "out.mp4")
        await tgfile.download_to_drive(src)

        await status.edit_text("Обрабатываю видео…")
        try:
            ffmpeg_overlay_flying(src, dst, wm_path)
        except subprocess.CalledProcessError:
            return await status.edit_text("Ошибка ffmpeg при обработке.")
        await status.edit_text("Отправляю результат…")
        await update.message.reply_video(video=dst, caption="Готово ✅", supports_streaming=True)
        await status.delete()


async def on_text_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Поймать ссылку в тексте, «починить», скачать, обработать."""
    chat_id = update.effective_chat.id
    key = user_choice.get(chat_id, "ve")
    wm_path = get_wm_path(key)

    text = update.message.text or ""
    m = URL_RE.search(text)
    if not m:
        return  # обычный текст игнорим

    raw_url = m.group(1)
    url = normalize_url(raw_url)

    status = await update.message.reply_text("Проверяю ссылку, скачиваю видео…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            dst = os.path.join(tmp, "out.mp4")

            # скачиваем полностью на диск
            try:
                download_url_to_file(url, src)
            except Exception as e:
                return await status.edit_text(f"Не смог скачать файл по ссылке:\n{e}")

            # обрабатываем
            await status.edit_text("Обрабатываю видео…")
            try:
                ffmpeg_overlay_flying(src, dst, wm_path)
            except subprocess.CalledProcessError:
                return await status.edit_text("Ошибка ffmpeg при обработке.")

            await status.edit_text("Отправляю результат…")
            await update.message.reply_video(video=dst, caption="Готово ✅", supports_streaming=True)
            await status.delete()

    except Exception as e:
        await status.edit_text(f"Ошибка: {e}")


def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_wm_choice, pattern=r"^wm:"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_url))

    print("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
