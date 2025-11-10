import os
import tempfile
import subprocess
from io import BytesIO

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from PIL import Image, ImageOps

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # вставь токен строкой или задай переменную окружения
BASE_DIR = os.path.dirname(__file__)

# файлы меток (PNG очень прозрачные) должны лежать рядом с bot.py
WM_VESELIE = os.path.join(BASE_DIR, "VESELIE_RU_watermark_transparent.png")
WM_FRIKI   = os.path.join(BASE_DIR, "FRIKI_REDANA_18_plus_transparent.png")

# Видео: большая метка по центру, плавно «летает»
VIDEO_SCALE_W = 0.70      # доля ширины кадра для watermark (уменьши до 0.60 если крупно)
WAVE_TX = 6.0             # период колебаний по X (сек)
WAVE_TY = 5.0             # период колебаний по Y (сек)
WAVE_AMPL_X = 0.25        # амплитуда по X в долях ширины
WAVE_AMPL_Y = 0.25        # амплитуда по Y в долях высоты

# Фото: диагонально на весь кадр, с вписыванием в рамку чтобы не вылезало
PHOTO_ANGLE_DEG = 35
PHOTO_FIT_RATIO = 0.88    # во сколько части экрана вписывать повернутую метку (0.80..0.95)
PHOTO_ALPHA_MULT = 1.0    # можно ослабить прозрачность PNG дополнительно (0.8 = ещё прозрачнее)
# =====================================================

# ---------------- ВСПОМОГАТЕЛЬНОЕ -------------------
def ensure_wm_exists(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Не найден watermark '{path}' рядом с bot.py")

def get_user_wm(context: ContextTypes.DEFAULT_TYPE) -> str:
    # по умолчанию используем ВЕСЕЛЬЕ.РУ
    return context.user_data.get("wm", WM_VESELIE)

def set_user_wm(context: ContextTypes.DEFAULT_TYPE, which: str) -> str:
    if which == "veselie":
        context.user_data["wm"] = WM_VESELIE
    elif which == "friki":
        context.user_data["wm"] = WM_FRIKI
    return context.user_data["wm"]

def wm_keyboard(current: str) -> InlineKeyboardMarkup:
    mark1 = "✅ " if current == WM_VESELIE else ""
    mark2 = "✅ " if current == WM_FRIKI   else ""
    kb = [
        [InlineKeyboardButton(f"{mark1}ВЕСЕЛЬЕ.РУ", callback_data="wm:veselie")],
        [InlineKeyboardButton(f"{mark2}ФРИКИ РЕДАНА 18+", callback_data="wm:friki")],
    ]
    return InlineKeyboardMarkup(kb)

def ffmpeg_overlay_flying(in_path: str, out_path: str, wm_path: str) -> None:
    """
    Накладывает PNG-метку на видео: масштабирует по ширине кадра и двигает около центра
    по синусам, чтобы метка была заметной и «живой».
    """
    filter_str = (
        f"[1:v][0:v]scale2ref=w=iw*{VIDEO_SCALE_W}:h=ow/mdar[wm][vid];"
        f"[vid][wm]overlay="
        f"x=(W-w)/2 + (W*{WAVE_AMPL_X})*sin(2*PI*t/{WAVE_TX}):"
        f"y=(H-h)/2 + (H*{WAVE_AMPL_Y})*cos(2*PI*t/{WAVE_TY}):"
        f"format=auto"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", in_path,
        "-i", wm_path,
        "-filter_complex", filter_str,
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "copy",
        out_path,
    ]
    subprocess.run(cmd, check=True)

def pil_overlay_diagonal(photo_bytes: bytes, wm_path: str) -> bytes:
    """
    Кладёт watermark по диагонали поверх фото.
    Порядок: поворот -> вписывание в рамку (W*ratio x H*ratio) -> центрирование.
    """
    ensure_wm_exists(wm_path)
    base = Image.open(BytesIO(photo_bytes)).convert("RGBA")
    W, H = base.size

    wm = Image.open(wm_path).convert("RGBA")
    if PHOTO_ALPHA_MULT != 1.0:
        r, g, b, a = wm.split()
        a = a.point(lambda p: int(p * PHOTO_ALPHA_MULT))
        wm = Image.merge("RGBA", (r, g, b, a))

    # поворот
    wm_rot = wm.rotate(PHOTO_ANGLE_DEG, expand=True, resample=Image.BICUBIC)

    # вписать повернутую метку в рамку, чтобы не вылезала за края
    fit_w, fit_h = int(W * PHOTO_FIT_RATIO), int(H * PHOTO_FIT_RATIO)
    wm_fit = ImageOps.contain(wm_rot, (fit_w, fit_h), method=Image.LANCZOS)
# центр
    x = (W - wm_fit.width) // 2
    y = (H - wm_fit.height) // 2

    out = base.copy()
    out.alpha_composite(wm_fit, (x, y))
    buf = BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ---------------------- ХЕНДЛЕРЫ ---------------------
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    cur = get_user_wm(c)
    await u.message.reply_text(
        "Отправь видео или фото.\n"
        "🎬 Видео — большая очень прозрачная метка по центру, плавно двигается.\n"
        "🖼 Фото — метка по диагонали на весь кадр.\n\n"
        "Выбери, какую метку использовать:",
        reply_markup=wm_keyboard(cur),
    )

async def choose_wm(u: Update, c: ContextTypes.DEFAULT_TYPE):
    cur = get_user_wm(c)
    await u.message.reply_text("Выбери водяную метку:", reply_markup=wm_keyboard(cur))

async def on_wm_choice(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data == "wm:veselie":
        set_user_wm(c, "veselie")
    elif q.data == "wm:friki":
        set_user_wm(c, "friki")
    cur = get_user_wm(c)
    await q.edit_message_text("Метка выбрана. Можно отправлять медиа.", reply_markup=wm_keyboard(cur))

async def on_video(u: Update, c: ContextTypes.DEFAULT_TYPE):
    wm = get_user_wm(c)
    ensure_wm_exists(wm)

    f = u.message.document or u.message.video
    if not f:
        return await u.message.reply_text("Пришли видео как файл (Document) или как Video.")
    status = await u.message.reply_text("Скачиваю видео…")
    tgfile = await f.get_file()

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.mp4")
        dst = os.path.join(tmp, "out.mp4")
        await tgfile.download_to_drive(src)

        await status.edit_text("Обрабатываю видео…")
        try:
            ffmpeg_overlay_flying(src, dst, wm)
        except subprocess.CalledProcessError:
            return await status.edit_text("Ошибка ffmpeg при обработке.")

        await status.edit_text("Отправляю результат…")
        await u.message.reply_video(video=dst, caption="Готово ✅", supports_streaming=True)

    await status.delete()

async def on_photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    wm = get_user_wm(c)
    ensure_wm_exists(wm)

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

    out_bytes = pil_overlay_diagonal(bytes(raw), wm)
    await u.message.reply_photo(photo=out_bytes, caption="Готово ✅")

# ----------------------- ЗАПУСК ----------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN (задай переменную окружения или вставь токен в код).")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # кнопки и команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wm", choose_wm))
    app.add_handler(CallbackQueryHandler(on_wm_choice, pattern=r"^wm:"))

    # медиа
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))

    print("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
