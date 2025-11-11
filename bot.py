#!/usr/bin/env python3
import os, re, math, tempfile, subprocess, requests
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from PIL import Image

# ========= НАСТРОЙКИ =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WATERMARKS = {
    "ve": ("ВЕСЕЛЬЕ.РУ", "VESELIE_RU_watermark_transparent.png"),
    "fr": ("ФРИКИ РЕДАНА 18+", "FRIKI_REDANA_18_plus_transparent.png"),
}
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
HTTP_TIMEOUT = 30
CHUNK = 1024 * 1024
user_choice: dict[int, str] = {}
# ==============================


def ensure_wm(path: str): 
    if not os.path.exists(path): raise FileNotFoundError(f"no wm {path}")
def get_wm(k: str) -> str: 
    k = k if k in WATERMARKS else "ve"
    _, fn = WATERMARKS[k]
    ensure_wm(fn)
    return fn


def ffmpeg_overlay_flying(inp, out, wm):
    filt = (
        "[1:v][0:v]scale2ref=w=iw*0.7:h=ow/mdar[wm][v];"
        "[v][wm]overlay=x=(W-w)/2+(W*0.25)*sin(2*PI*t/6):"
        "y=(H-h)/2+(H*0.25)*cos(2*PI*t/5):format=auto"
    )
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-i", inp, "-i", wm,
        "-filter_complex", filt, "-map", "0:a?", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "23", "-c:a", "copy", out
    ], check=True)


def pil_overlay(photo, wm_path):
    base = Image.open(BytesIO(photo)).convert("RGBA")
    W, H = base.size
    wm = Image.open(wm_path).convert("RGBA")
    diag = int(math.sqrt(W*W + H*H))
    wm = wm.resize((int(wm.width*diag/wm.width), int(wm.height*diag/wm.width)), Image.LANCZOS)
    wm = wm.rotate(35, expand=True)
    x, y = (W - wm.width)//2, (H - wm.height)//2
    out = base.copy(); out.alpha_composite(wm, (x, y))
    buf = BytesIO(); out.save(buf, format="PNG"); buf.seek(0)
    return buf


# ---------- ссылки ----------
URL_RE = re.compile(r'(https?://\S+)', re.I)
def normalize_url(u: str) -> str:
    u = u.strip()
    if "dropbox.com" in u:
        u = u.replace("www.dropbox.com", "dl.dropboxusercontent.com")
        u = re.sub(r"[?&]dl=\d", "", u)
        if "raw=1" not in u:
            u += ("&" if "?" in u else "?") + "raw=1"
        return u
    m = re.search(r"drive\.google\.com/file/d/([^/]+)/", u)
    if m:
        return f"https://drive.usercontent.google.com/uc?id={m.group(1)}&export=download"
    return u

def download_url(url, dest):
    headers = {"User-Agent": "Mozilla/5.0 (WatermarkBot)"}
    try:
        h = requests.head(url, headers=headers, allow_redirects=True, timeout=HTTP_TIMEOUT)
        if "text/html" in (h.headers.get("Content-Type") or "").lower():
            raise ValueError("по ссылке не файл, а HTML-страница")
    except Exception:
        pass
    with requests.get(url, headers=headers, stream=True, timeout=HTTP_TIMEOUT, allow_redirects=True) as r:
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ct:
            raise ValueError("ссылка ведёт не на файл видео")
        total = 0
        with open(dest, "wb") as f:
            for ch in r.iter_content(CHUNK):
                if not ch: continue
                total += len(ch)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("файл слишком большой")
                f.write(ch)


# ---------- UI ----------
def wm_keyboard(cur=None):
    b = []
    for k,(n,_) in WATERMARKS.items():
        b.append([InlineKeyboardButton(f"{'✅ ' if cur==k else ''}{n}", callback_data=f"wm:{k}")])
    return InlineKeyboardMarkup(b)


async def start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    ch=u.effective_chat.id
    user_choice[ch]="ve"
    await u.message.reply_text(
        "Отправь видео/фото или ссылку.\nБольшие файлы — только ссылкой.\nВыбери водяной знак:",
        reply_markup=wm_keyboard("ve")
    )


async def choose(u:Update,c:ContextTypes.DEFAULT_TYPE):
    q=u.callback_query;await q.answer()
    ch=q.message.chat.id;_,k=q.data.split(":")
    user_choice[ch]=k
    await q.edit_message_text(f"Выбран: {WATERMARKS[k][0]}.\nТеперь пришли видео/фото или ссылку.")


async def photo(u:Update,c:ContextTypes.DEFAULT_TYPE):
    ch=u.effective_chat.id;k=user_choice.get(ch,"ve");wm=get_wm(k)
    f=u.message.photo[-1] if u.message.photo else u.message.document
    raw=await f.get_file();data=await raw.download_as_bytearray()
    out=pil_overlay(data,wm)
    await u.message.reply_photo(photo=out,caption="Готово ✅")


async def video(u:Update,c:ContextTypes.DEFAULT_TYPE):
    ch=u.effective_chat.id;k=user_choice.get(ch,"ve");wm=get_wm(k)
    f=u.message.video or u.message.document
    if f.file_size>19*1024*1024:
        return await u.message.reply_text("Слишком большой файл (>20 МБ). Пришли ссылку на видео.")
    msg=await u.message.reply_text("Обрабатываю…")
    with tempfile.TemporaryDirectory() as t:
        src=os.path.join(t,"in.mp4");dst=os.path.join(t,"out.mp4")
        tg=await f.get_file();await tg.download_to_drive(src)
        try: ffmpeg_overlay_flying(src,dst,wm)
        except: return await msg.edit_text("Ошибка ffmpeg.")
        await u.message.reply_video(video=dst,caption="Готово ✅",supports_streaming=True)
    await msg.delete()


async def link(u:Update,c:ContextTypes.DEFAULT_TYPE):
    ch=u.effective_chat.id;k=user_choice.get(ch,"ve");wm=get_wm(k)
    t=u.message.text or "";m=URL_RE.search(t)
    if not m:return
    url=normalize_url(m.group(1))
    msg=await u.message.reply_text("Качаю видео…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src=os.path.join(tmp,"in.mp4");dst=os.path.join(tmp,"out.mp4")
            download_url(url,src)
            await msg.edit_text("Обрабатываю…")
            ffmpeg_overlay_flying(src,dst,wm)
            await u.message.reply_video(video=dst,caption="Готово ✅",supports_streaming=True)
    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")


def main():
    if not BOT_TOKEN: raise SystemExit("Нет BOT_TOKEN")
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CallbackQueryHandler(choose,pattern="^wm:"))
    app.add_handler(MessageHandler(filters.PHOTO|filters.Document.IMAGE,photo))
    app.add_handler(MessageHandler(filters.VIDEO|filters.Document.VIDEO,video))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND),link))
    print("bot running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)


if __name__=="__main__":main()
