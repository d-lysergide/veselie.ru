#!/usr/bin/env python3
import os
import re
import math
import tempfile
import subprocess
from io import BytesIO

import requests
from PIL import Image
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# =========================
# НАСТРОЙКИ / ПЕРЕМЕННЫЕ
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# пути до PNG-водяных знаков (лежат рядом с bot.py)
WM_VESELIE = "VESELIE_RU_watermark_transparent.png"
WM_FRIKI   = "FRIKI_REDANA_18_plus_transparent.png"

# что выбрано по умолчанию
DEFAULT_WM = "VESELIE"  # VESELIE | FRIKI

# Лимит Телеграм на скачивание ботом (файлы больше — Telegram не отдаёт file_path)
TG_MAX_DOWNLOAD_MB = int(os.getenv("TG_MAX_DOWNLOAD_MB", "20"))
TG_MAX_DOWNLOAD    = TG_MAX_DOWNLOAD_MB * 1024 * 1024

# Ограничения на итоговый файл, чтобы точно отправить обратно
OUTPUT_MAX_MB = int(os.getenv("OUTPUT_MAX_MB", "45"))     # целим ~45 МБ
MAX_W         = int(os.getenv("MAX_W", "960"))            # макс. ширина для даунскейла

# «летающая» метка на видео
VIDEO_SCALE_W = 0.70
WAVE_TX, WAVE_TY = 6.0, 5.0
WAVE_AMPL_X, WAVE_AMPL_Y = 0.25, 0.25

# Фото: диагонально на весь кадр
PHOTO_ANGLE_DEG  = 35
PHOTO_ALPHA_MULT = 1.0

URL_RE = re.compile(r"https?://[^\s]+")


# =========================
# ВСПОМОГАТЕЛЬНОЕ
# =========================

def ensure_wm(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Не найден watermark '{path}' рядом с bot.py")

def pick_wm(name: str) -> str:
    name = (name or DEFAULT_WM).upper()
    if name == "FRIKI":
        ensure_wm(WM_FRIKI);   return WM_FRIKI
    ensure_wm(WM_VESELIE);     return WM_VESELIE

def current_wm(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("wm", DEFAULT_WM)

def set_wm(context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    context.user_data["wm"] = name

def wm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ВЕСЕЛЬЕ.РУ", callback_data="wm:VESELIE"),
         InlineKeyboardButton("ФРИКИ РЕДАНА 18+", callback_data="wm:FRIKI")]
    ])

def scale_filter_flying(wm_scale=VIDEO_SCALE_W):
    return (
        f"[1:v][0:v]scale2ref=w=iw*{wm_scale}:h=ow/mdar[wm][vid];"
        f"[vid][wm]overlay="
        f"x=(W-w)/2 + (W*{WAVE_AMPL_X})*sin(2*PI*t/{WAVE_TX}):"
        f"y=(H-h)/2 + (H*{WAVE_AMPL_Y})*cos(2*PI*t/{WAVE_TY}):"
        f"format=auto"
    )

def ffprobe_duration(path: str) -> float | None:
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path
        ], text=True)
        return float(out.strip())
    except Exception:
        return None

def run_ffmpeg(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)

def compress_video_with_cap(src: str, dst: str, wm_path: str) -> None:
    """
    1) Накладываем метку + даунскейлим до MAX_W
    2) Пробуем CRF 23, если файл > OUTPUT_MAX_MB — CRF 28
    """
    temp1 = os.path.join(os.path.dirname(dst), "stage.mp4")

    # Наложение + масштаб по ширине
    vf = (
        f"scale='min({MAX_W},iw)':-2:flags=bicubic,"
        f"format=yuv420p"
    )
    filter_complex = f"{scale_filter_flying()},{vf}"

    run_ffmpeg([
        "ffmpeg","-hide_banner","-loglevel","error","-y",
        "-i", src, "-i", wm_path,
        "-filter_complex", filter_complex,
        "-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","23",
        "-c:a","aac","-b:a","128k", temp1
    ])

    # Проверим размер
    if os.path.getsize(temp1) <= OUTPUT_MAX_MB * 1024 * 1024:
        os.replace(temp1, dst)
        return

    # Ещё ужать
    run_ffmpeg([
        "ffmpeg","-hide_banner","-loglevel","error","-y",
        "-i", temp1,
        "-c:v","libx264","-preset","veryfast","-crf","28",
        "-c:a","aac","-b:a","96k", dst
    ])

def overlay_diagonal_photo(photo_bytes: bytes, wm_path: str) -> bytes:
    base = Image.open(BytesIO(photo_bytes)).convert("RGBA")
    W, H = base.size
    wm = Image.open(wm_path).convert("RGBA")

    if PHOTO_ALPHA_MULT != 1.0:
        r,g,b,a = wm.split()
        a = a.point(lambda p: int(p * PHOTO_ALPHA_MULT))
        wm = Image.merge("RGBA",(r,g,b,a))

    # тянем по диагонали
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

def download_http_to(path: str, url: str, max_bytes: int | None = None) -> None:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024*64):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        raise RuntimeError("Слишком большой файл по ссылке")

# =========================
# ХЕНДЛЕРЫ
# =========================

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not current_wm(c):
        set_wm(c, DEFAULT_WM)
    await u.message.reply_text(
        "Отправь видео или фото.\n"
        "• Большие файлы, из-за которых Телеграм ругается — пришли **ссылкой (http/https)**.\n"
        "• Кнопками ниже выбери водяной знак.",
        reply_markup=wm_keyboard()
    )

async def change_wm(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data and q.data.startswith("wm:"):
        name = q.data.split(":",1)[1]
        set_wm(c, name)
        await q.edit_message_text(
            f"Готово. Текущая метка: **{name}**.\nОтправь видео/фото или пришли ссылку.",
            reply_markup=wm_keyboard(), parse_mode="Markdown"
        )

def too_big_msg():
    return (
        f"Файл слишком большой для скачивания через Bot API (> {TG_MAX_DOWNLOAD_MB} МБ).\n"
        "Пришли **ссылку (http/https)** на файл — я скачаю напрямую, обработаю и верну результат."
    )

async def handle_video(u: Update, c: ContextTypes.DEFAULT_TYPE):
    f = u.message.document or u.message.video
    if not f:
        return

    # Ранний чек лимита Telegram
    if getattr(f, "file_size", 0) > TG_MAX_DOWNLOAD:
        return await u.message.reply_text(too_big_msg())

    status = await u.message.reply_text("Скачиваю видео…")
    file = await f.get_file()

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.mp4")
        out = os.path.join(tmp, "out.mp4")
        await file.download_to_drive(src)
        await status.edit_text("Обрабатываю видео…")

        wm_path = pick_wm(current_wm(c))
        try:
            compress_video_with_cap(src, out, wm_path)
        except subprocess.CalledProcessError:
            return await status.edit_text("Ошибка ffmpeg при обработке.")
        await status.edit_text("Отправляю результат…")
        await u.message.reply_video(video=out, caption="Готово ✅", supports_streaming=True)
        await status.delete()

async def handle_photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    photo = u.message.photo[-1] if u.message.photo else None
    doc = u.message.document if (u.message.document and (u.message.document.mime_type or "").startswith("image")) else None
    if not photo and not doc:
        return
    size = getattr(doc, "file_size", 0) if doc else getattr(photo, "file_size", 0)
    if size > TG_MAX_DOWNLOAD:
        return await u.message.reply_text(too_big_msg())

    status = await u.message.reply_text("Скачиваю фото…")
    tgfile = await (doc.get_file() if doc else photo.get_file())
    raw = await tgfile.download_as_bytearray()

    wm_path = pick_wm(current_wm(c))
    try:
        out_bytes = overlay_diagonal_photo(bytes(raw), wm_path)
    except Exception:
        return await status.edit_text("Ошибка обработки изображения.")
    await status.edit_text("Отправляю результат…")
    await u.message.reply_photo(photo=out_bytes, caption="Готово ✅")
    await status.delete()

async def handle_url(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Ловим сообщения со ссылкой: качаем напрямую и обрабатываем как видео/фото."""
    text = u.message.text or ""
    m = URL_RE.search(text)
    if not m:
        return
    url = m.group(0).strip()

    status = await u.message.reply_text("Качаю по ссылке…")
    with tempfile.TemporaryDirectory() as tmp:
        # Пробуем понять тип по расширению (очень грубо)
        low = url.lower()
        is_image = any(low.endswith(ext) for ext in (".png",".jpg",".jpeg",".webp"))
        is_video = any(low.endswith(ext) for ext in (".mp4",".mov",".m4v",".webm",".mkv",".avi"))

        try:
            if is_image:
                dst = os.path.join(tmp, "img")
                download_http_to(dst, url, max_bytes=None)
                wm_path = pick_wm(current_wm(c))
                with open(dst, "rb") as f:
                    raw = f.read()
                out_bytes = overlay_diagonal_photo(raw, wm_path)
                await status.edit_text("Отправляю результат…")
                await u.message.reply_photo(photo=out_bytes, caption="Готово ✅")
                return

            # считаем видео по умолчанию
            src = os.path.join(tmp, "in.mp4")
            out = os.path.join(tmp, "out.mp4")
            download_http_to(src, url, max_bytes=None)

            await status.edit_text("Обрабатываю видео…")
            wm_path = pick_wm(current_wm(c))
            compress_video_with_cap(src, out, wm_path)

            await status.edit_text("Отправляю результат…")
            await u.message.reply_video(video=out, caption="Готово ✅", supports_streaming=True)

        except requests.HTTPError:
            await status.edit_text("Не удалось скачать по ссылке (HTTP ошибка).")
        except RuntimeError as e:
            await status.edit_text(str(e))
        except subprocess.CalledProcessError:
            await status.edit_text("Ошибка ffmpeg при обработке.")
        except Exception:
            await status.edit_text("Не удалось обработать файл по ссылке.")
        finally:
            try: await status.delete()
            except: pass

# =========================
# MAIN
# =========================

def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(change_wm, pattern=r"^wm:"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))  # ловим ссылки
    print("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
