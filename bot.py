#!/usr/bin/env python3
import os
import tempfile
import subprocess
from io import BytesIO

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from PIL import Image, ImageDraw, ImageFont, ImageOps

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "")                       # задай переменную окружения на сервере/локально
WATERMARK_PATH = "VESELIE_RU_watermark_transparent.png"      # очень прозрачный PNG рядом с bot.py

# Видео: большая, «летает» по центру
VIDEO_SCALE_W = 0.70   # watermark ≈70% ширины видео
WAVE_TX = 6.0          # период колебаний по X (сек)
WAVE_TY = 5.0          # период колебаний по Y (сек)
WAVE_AMPL_X = 0.25     # доля ширины экрана для амплитуды X
WAVE_AMPL_Y = 0.25     # доля высоты экрана для амплитуды Y

# Фото: диагонально на весь кадр
PHOTO_ANGLE_DEG = 35
PHOTO_ALPHA_MULT = 1.0  # PNG уже прозрачная; можно уменьшать (0.8 = ещё прозрачнее)

def ensure_wm_exists() -> None:
    if not os.path.exists(WATERMARK_PATH):
        raise FileNotFoundError(f"Не найден watermark '{WATERMARK_PATH}' рядом с bot.py")

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
    ensure_wm_exists()
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

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "Отправь видео или фото.\n"
        "🎬 Видео — большая, очень прозрачная метка «ВЕСЕЛЬЕ.РУ» по центру, плавно двигается.\n"
        "🖼 Фото — метка наискосок на весь кадр."
    )

async def on_video(u: Update, c: ContextTypes.DEFAULT_TYPE):
    ensure_wm_exists()
    f = u.message.document or u.message.video
    if not f:
        return await u.message.reply_text("Пришли видео как файл (Document) или как Video.")
    status = await u.message.reply_text("Скачиваю видео…")
    tgfile = await f.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.mp4")
        dst = os.path.join(tmp, "out.mp4")
        await tgfile.download_to_drive(src)
        await status.edit_text("Обрабатываю видео (летящая метка)…")
        try:
            ffmpeg_overlay_flying(src, dst, WATERMARK_PATH)
        except subprocess.CalledProcessError:
            return await status.edit_text("Ошибка ffmpeg при обработке.")
        await status.edit_text("Отправляю результат…")
        await u.message.reply_video(video=dst, caption="Готово ✅", supports_streaming=True)
        await status.delete()

async def on_photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    ensure_wm_exists()
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

    out_bytes = pil_overlay_diagonal(bytes(raw), WATERMARK_PATH)
    await u.message.reply_photo(photo=out_bytes, caption="Готово ✅")

def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN (задай переменную окружения или вставь токен в код).")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    print("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
