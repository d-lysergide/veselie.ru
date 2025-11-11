#!/usr/bin/env python3
import os
import re
import math
import tempfile
import asyncio
import subprocess
from io import BytesIO
from urllib.parse import urlparse, parse_qs

import aiohttp
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, filters
)
from PIL import Image

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Файлы водяных знаков (лежать рядом с bot.py)
WM_VESELIE = "VESELIE_RU_watermark_transparent.png"
WM_FRIKI   = "FRIKI_REDANA_18_plus_transparent.png"

# Тексты кнопок
BTN_VESELIE = "✅ ВЕСЕЛЬЕ.РУ"
BTN_FRIKI   = "ФРИКИ РЕДАНА 18+"

# Лимит Bot API на скачивание самим ботом (примерно)
BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # 20 МБ

# Видео: большая метка «летает» по центру
VIDEO_SCALE_W = 0.70
WAVE_TX = 6.0
WAVE_TY = 5.0
WAVE_AMPL_X = 0.25
WAVE_AMPL_Y = 0.25

# Фото: диагональная метка
PHOTO_ANGLE_DEG = 35
PHOTO_ALPHA_MULT = 1.0  # PNG уже прозрачная

# =================================================


def ensure_wm_exists(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Не найден watermark '{path}' рядом с bot.py")


def current_wm_path(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    # храним выбор пользователя в user_data
    wm = ctx.user_data.get("wm", "ve")
    return WM_VESELIE if wm == "ve" else WM_FRIKI


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
    ensure_wm_exists(wm_path)
    base = Image.open(BytesIO(photo_bytes)).convert("RGBA")
    W, H = base.size

    wm = Image.open(wm_path).convert("RGBA")
    if PHOTO_ALPHA_MULT != 1.0:
        r,g,b,a = wm.split()
        a = a.point(lambda p: int(p * PHOTO_ALPHA_MULT))
        wm = Image.merge("RGBA",(r,g,b,a))

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


# ========= парсинг и «починка» ссылок =========

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def find_first_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def _gdrive_to_direct(url: str) -> str | None:
    """
    Поддержка:
      - https://drive.google.com/file/d/<id>/view?...
      - https://drive.google.com/open?id=<id>
      - https://drive.google.com/uc?id=<id>&export=download
    Возвращаем:
      - https://drive.google.com/uc?export=download&id=<id>
    """
    u = urlparse(url)
    if "drive.google.com" not in u.netloc:
        return None

    # вариант file/d/<id>/view
    m = re.search(r"/file/d/([^/]+)/", u.path)
    if m:
        file_id = m.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    # вариант open?id=<id> или uc?id=<id>
    q = parse_qs(u.query)
    for key in ("id", "file_id"):
        if key in q and q[key]:
            return f"https://drive.google.com/uc?export=download&id={q[key][0]}"

    return None


def _dropbox_to_direct(url: str) -> str | None:
    """
    Поддержка:
      - https://www.dropbox.com/s/<id>/<name>?dl=0 -> dl=1
      - https://www.dropbox.com/scl/fi/... ?rlkey=...&dl=0 -> dl=1
      - если уже ?raw=1 / ?dl=1 — оставляем
    """
    u = urlparse(url)
    if "dropbox.com" not in u.netloc:
        return None

    # если уже raw или dl=1 — норм
    if "raw=1" in u.query or "dl=1" in u.query:
        return url

    if u.query:
        q = u.query
        q = re.sub(r"dl=0", "dl=1", q)
        if "dl=" not in q:
            q = (q + "&dl=1") if q else "dl=1"
        new = u._replace(query=q)
        return new.geturl()
    else:
        # без query — добавим
        new = u._replace(query="dl=1")
        return new.geturl()


def normalize_to_direct(url: str) -> str | None:
    url = url.strip()
    # Google Drive
    d = _gdrive_to_direct(url)
    if d:
        return d
    # Dropbox
    d = _dropbox_to_direct(url)
    if d:
        return d
    # Files.fm / прямые .mp4 / .mov — оставляем как есть
    if re.search(r"\.(mp4|mov|m4v|webm)(\?|#|$)", url, re.IGNORECASE):
        return url
    # Иногда шаринг выдает редирект — попробуем как есть
    return url


async def _looks_like_video(session: aiohttp.ClientSession, url: str) -> bool:
    try:
        # сначала HEAD
        async with session.head(url, allow_redirects=True, timeout=20) as r:
            ct = r.headers.get("Content-Type","").lower()
            if ct.startswith("video"):
                return True
            # некоторые отдают octet-stream
            if "octet-stream" in ct:
                return True
        # fallback GET первых байт
        async with session.get(url, allow_redirects=True, timeout=20) as r:
            ct = r.headers.get("Content-Type", "").lower()
            if ct.startswith("video") or "octet-stream" in ct:
                return True
    except Exception:
        return False
    return False


async def download_by_url(url: str, dst_path: str, report_cb=None) -> None:
    CHUNK = 1024 * 512
    async with aiohttp.ClientSession() as session:
        async with session.get(url, allow_redirects=True, timeout=None) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(dst_path, "wb") as f:
                async for chunk in r.content.iter_chunked(CHUNK):
                    f.write(chunk)
                    done += len(chunk)
                    if report_cb and total:
                        await report_cb(done, total)


# =============== Хендлеры Telegram ===============

def wm_keyboard(ctx: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    wm = ctx.user_data.get("wm", "ve")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("✅ " if wm=="ve" else "") + "ВЕСЕЛЬЕ.РУ", callback_data="wm:ve"),
        InlineKeyboardButton(("✅ " if wm=="fr" else "") + "ФРИКИ РЕДАНА 18+", callback_data="wm:fr"),
    ]])


async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data.setdefault("wm", "ve")
    text = (
        "Отправь видео/фото **или ссылку на видео**.\n\n"
        "🎬 *Видео* — большая прозрачная метка по центру «летает».\n"
        "🖼 *Фото* — метка диагонально на весь кадр.\n"
        "📦 *Большие файлы*: пришли **ссылку** — бот сам скачаeт.\n\n"
        "Поддерживаю Google Drive и Dropbox — превращу «поделиться» в прямую ссылку.\n"
        "_Если файл > 20 МБ и прислать его прямо в чат — скачать не смогу (ограничение Telegram Bot API)._"
    )
    await u.message.reply_text(text, reply_markup=wm_keyboard(c), disable_web_page_preview=True)


async def on_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data.startswith("wm:"):
        c.user_data["wm"] = "ve" if q.data.endswith("ve") else "fr"
        await q.edit_message_reply_markup(reply_markup=wm_keyboard(c))


async def on_photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    wm_path = current_wm_path(c)
    ensure_wm_exists(wm_path)

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


async def on_video(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    Если видео <= 20 МБ — скачиваем через Bot API и обрабатываем.
    Если больше — просим ссылку.
    """
    file_obj = u.message.video or u.message.document
    if not file_obj:
        return await u.message.reply_text("Пришли видео как Video или Document.")

    if file_obj.file_size and file_obj.file_size > BOT_API_DOWNLOAD_LIMIT:
        return await u.message.reply_text(
            "Файл слишком большой для скачивания через Bot API (>20 МБ).\n"
            "Пришли **ссылку (Google Drive/Dropbox)** на файл — я сам скачаю, обработаю и пришлю результат.",
            disable_web_page_preview=True
        )

    status = await u.message.reply_text("Скачиваю видео…")
    tgfile = await file_obj.get_file()

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.mp4")
        dst = os.path.join(tmp, "out.mp4")
        await tgfile.download_to_drive(src)

        await status.edit_text("Обрабатываю видео…")
        try:
            ffmpeg_overlay_flying(src, dst, current_wm_path(c))
        except subprocess.CalledProcessError:
            return await status.edit_text("Ошибка ffmpeg при обработке.")

        await status.edit_text("Отправляю результат…")
        await u.message.reply_video(video=dst, caption="Готово ✅", supports_streaming=True)
        await status.delete()


async def on_text_with_url(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    Ловим текст с URL: чиним линк (GDrive/Dropbox), проверяем что это видео,
    качаем в tmp, гоняем через ffmpeg и возвращаем.
    """
    text = u.message.text or u.message.caption or ""
    url = find_first_url(text)
    if not url:
        return

    fixed = normalize_to_direct(url)

    status = await u.message.reply_text("Проверяю ссылку…", disable_web_page_preview=True)
    try:
        async with aiohttp.ClientSession() as session:
            # Быстрый sanity-check: это вообще видео?
            if not await _looks_like_video(session, fixed):
                await status.edit_text("Ошибка: ссылка ведёт не на файл видео (или доступ к нему запрещён).")
                return

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            dst = os.path.join(tmp, "out.mp4")

            async def progress(done, total):
                if total:
                    pct = int(done * 100 / total)
                    # обновляем не чаще раза в ~2 сек
                    if pct in (10,20,30,40,50,60,70,80,90):
                        try:
                            await status.edit_text(f"Скачиваю… {pct}%")
                        except Exception:
                            pass

            await download_by_url(fixed, src, progress)

            await status.edit_text("Обрабатываю видео…")
            try:
                ffmpeg_overlay_flying(src, dst, current_wm_path(c))
            except subprocess.CalledProcessError:
                return await status.edit_text("Ошибка ffmpeg при обработке.")

            await status.edit_text("Отправляю результат…")
            await u.message.reply_video(video=dst, caption="Готово ✅", supports_streaming=True)
            await status.delete()

    except Exception as e:
        await status.edit_text("Ошибка: ссылка недоступна или формат не поддерживается.")
        return


async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    Если в тексте есть URL — обрабатываем как ссылку.
    Иначе — подсказываем, что делать.
    """
    text = u.message.text or ""
    if URL_RE.search(text):
        return await on_text_with_url(u, c)

    await u.message.reply_text(
        "Пришли видео/фото **или ссылку на видео** (Google Drive/Dropbox).\n"
        "Большие файлы — через ссылку.\n"
        "Выбор метки ниже.", reply_markup=wm_keyboard(c),
        disable_web_page_preview=True
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN (задай переменную окружения).")

    # Проверим, что WM-файлы на месте — чтобы падение было сразу, а не в рантайме
    for p in (WM_VESELIE, WM_FRIKI):
        ensure_wm_exists(p)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^wm:"))

    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
