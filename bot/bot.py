#!/usr/bin/env python3
"""Telegram bot: video in, kinetic captions out. Standard library only.

Environment: KIN_TOKEN, KIN_WORK, plus everything kinetic.py reads.
"""
import json, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request
import urllib.parse, uuid, mimetypes, html

TOKEN = os.environ["KIN_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
FILEAPI = f"https://api.telegram.org/file/bot{TOKEN}"
WORK = os.environ.get("KIN_WORK", "/opt/kinetic/work")
KINETIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kinetic.py")
MAX_MB = 48          # Telegram hands bots files up to ~20 MB; we can send up to 50
MAX_SEC = 180

FILE_TTL = 20 * 60    # nothing on disk outlives this
WORK_CAP_MB = 2000    # workdir ceiling; past it we stop accepting jobs

os.makedirs(WORK, exist_ok=True)
jobs: "queue.Queue" = queue.Queue()
busy = threading.Lock()
pending = {}          # chat_id -> (path, message_id, downloaded at)


# ------------------------------------------------- Cleanup: we keep nothing
def purge(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def work_mb():
    total = 0
    for n in os.listdir(WORK):
        try:
            total += os.path.getsize(os.path.join(WORK, n))
        except OSError:
            pass
    return total / 1e6


def sweep(reason):
    """Drop anything stale: crashed jobs, abandoned confirmations."""
    now, killed = time.time(), 0
    for n in os.listdir(WORK):
        p = os.path.join(WORK, n)
        try:
            if now - os.path.getmtime(p) > FILE_TTL and purge(p):
                killed += 1
        except OSError:
            pass
    for chat, item in list(pending.items()):
        if now - item[2] > FILE_TTL:
            pending.pop(chat, None)
            purge(item[0])
    if killed:
        print(f"[cleanup/{reason}] removed {killed} file(s)", flush=True)


def janitor():
    while True:
        time.sleep(60)
        try:
            sweep("timer")
        except Exception as e:
            print(f"[cleanup] {e}", flush=True)


# ---------------------------------------------------------------- Telegram API
def api(method, **params):
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
         for k, v in params.items() if v is not None}).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"{API}/{method}", data, timeout=90) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 2:
                print(f"[api] {method}: {e}", flush=True)
                return {"ok": False}
            time.sleep(1.5 * (attempt + 1))


def send(chat, text, kb=None, mid=None):
    """Send or edit a message. Returns the message_id."""
    p = dict(chat_id=chat, text=text, parse_mode="HTML",
             disable_web_page_preview=True,
             reply_markup=({"inline_keyboard": kb} if kb else None))
    r = api("editMessageText", message_id=mid, **p) if mid else api("sendMessage", **p)
    if not r.get("ok") and mid:                 # unchanged text is not an error
        return mid
    return (r.get("result") or {}).get("message_id", mid)


def send_video(chat, path, caption):
    boundary = uuid.uuid4().hex
    fields = {"chat_id": str(chat), "caption": caption, "parse_mode": "HTML",
              "supports_streaming": "true"}
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    with open(path, "rb") as f:
        blob = f.read()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; "
             f"filename=\"{os.path.basename(path)}\"\r\n"
             f"Content-Type: video/mp4\r\n\r\n").encode() + blob + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{API}/sendVideo", body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print(f"[sendVideo] {e}", flush=True)
        return False


def download(file_id, dest):
    r = api("getFile", file_id=file_id)
    if not r.get("ok"):
        return False
    path = r["result"]["file_path"]
    try:
        with urllib.request.urlopen(f"{FILEAPI}/{path}", timeout=600) as src, \
             open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return True
    except Exception as e:
        print(f"[download] {e}", flush=True)
        return False


# ------------------------------------------------------------------------- UI
BAR = "▰▱"


def bar(done, total=5):
    return BAR[0] * done + BAR[1] * (total - done)


STAGES = ["received", "transcribing", "splitting phrases",
          "drawing text", "encoding"]


def progress_text(step, extra=""):
    lines = [f"<b>Working</b>  {bar(step)}", ""]
    for i, s in enumerate(STAGES):
        mark = "✓" if i < step else ("▸" if i == step else "·")
        lines.append(f"{mark} {s}" if i != step else f"{mark} <b>{s}</b>")
    if extra:
        lines += ["", f"<i>{html.escape(extra)}</i>"]
    return "\n".join(lines)


WELCOME = (
    "<b>Kinetic captions</b>\n\n"
    "Send a video and it comes back with documentary-style captions: "
    "key words large, the rest small, each one timed to your speech.\n\n"
    "• portrait video → text inside the frame\n"
    "• landscape → letterboxed, text in the bar above\n"
    "• layout is detected automatically, audio normalised for the platforms\n\n"
    f"Up to {MAX_SEC}s and {MAX_MB} MB.\n\n"
    "🔒 <b>Nothing is stored.</b> Your video is deleted from the server as soon "
    "as you receive the result. No copies are kept."
)

MODE_KB = [[{"text": "🎬 Kinetic", "callback_data": "go:kinetic"}],
           [{"text": "⚡ Fast, no emphasis picking", "callback_data": "go:plain"}],
           [{"text": "✖️ Cancel", "callback_data": "go:cancel"}]]


# --------------------------------------------------------------------- Worker
def worker():
    while True:
        chat, mid, src, mode = jobs.get()
        with busy:
            run_job(chat, mid, src, mode)
        jobs.task_done()


def run_job(chat, mid, src, mode):
    out = os.path.splitext(src)[0] + "_out.mp4"
    t0 = time.time()
    try:
        send(chat, progress_text(1), mid=mid)
        env = dict(os.environ)
        if mode == "plain":
            env["KIN_NO_EMPHASIS"] = "1"
        proc = subprocess.Popen([sys.executable, KINETIC, src, out],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, bufsize=1)
        info, step = [], 1
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            info.append(line)
            if "words:" in line:
                step = 2; send(chat, progress_text(step, line), mid=mid)
            elif line.startswith("phrases:"):
                step = 3; send(chat, progress_text(step, line), mid=mid)
            elif "ass events" in line:
                step = 4; send(chat, progress_text(step, "rendering frames"), mid=mid)
        proc.wait()
        purge(src)                     # source is no longer needed
        if proc.returncode != 0 or not os.path.exists(out):
            tail = "\n".join(info[-4:]) or "no details"
            send(chat, f"<b>That did not work.</b>\n\n<code>{html.escape(tail)}</code>", mid=mid)
            return

        size = os.path.getsize(out) / 1e6
        if size > MAX_MB:
            send(chat, f"<b>Done, but the file is {size:.0f} MB</b> — "
                       f"Telegram will not accept that from a bot. Send a shorter clip.", mid=mid)
            return

        send(chat, progress_text(5, "sending"), mid=mid)
        took = time.time() - t0
        fmt = next((l for l in info if l.startswith("source")), "")
        cap = (f"<b>Done</b> · {took:.0f}s · {size:.0f} MB\n"
               f"<code>{html.escape(fmt)}</code>")
        if send_video(chat, out, cap):
            api("deleteMessage", chat_id=chat, message_id=mid)
        else:
            send(chat, "Rendered, but could not send it — the file is too large.", mid=mid)
    except Exception as e:
        send(chat, f"<b>Failed:</b> <code>{html.escape(str(e))}</code>", mid=mid)
    finally:
        # whatever happened, nothing is left behind
        purge(src)
        purge(out)
        print(f"[cleanup] job closed, {work_mb():.0f} MB left in workdir", flush=True)


# -------------------------------------------------------------------- Router
def handle_video(chat, msg):
    v = msg.get("video") or msg.get("document") or msg.get("video_note")
    if not v:
        return
    dur = v.get("duration", 0)
    size = v.get("file_size", 0) / 1e6
    if dur and dur > MAX_SEC:
        send(chat, f"That clip is {dur}s — a bit long. I take up to {MAX_SEC}s.")
        return
    if size > 20:
        send(chat, f"File is {size:.0f} MB. Telegram only hands bots files up to 20 MB — "
                   f"send something shorter or compress it.")
        return
    if work_mb() > WORK_CAP_MB:
        sweep("low disk")
        if work_mb() > WORK_CAP_MB:
            send(chat, "Busy right now, disk is full. Try again in a couple of minutes.")
            return
    # clear this user's previous pending file so it cannot linger
    old = pending.pop(chat, None)
    if old:
        purge(old[0])
    mid = send(chat, "Downloading…")
    dest = os.path.join(WORK, f"{chat}_{uuid.uuid4().hex[:8]}.mp4")
    if not download(v["file_id"], dest):
        purge(dest)
        send(chat, "Could not download the file from Telegram. Try again.", mid=mid)
        return
    pending[chat] = (dest, mid, time.time())
    send(chat, "<b>Got it.</b>\nHow should it look?", kb=MODE_KB, mid=mid)


def handle_callback(cb):
    chat = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    data = cb.get("data", "")
    api("answerCallbackQuery", callback_query_id=cb["id"])
    item = pending.pop(chat, None)
    if not item:
        send(chat, "That video is already done. Send a new one.", mid=mid)
        return
    src = item[0]
    if data == "go:cancel":
        purge(src)
        send(chat, "Cancelled, the video was deleted.", mid=mid)
        return
    mode = "plain" if data == "go:plain" else "kinetic"
    ahead = jobs.qsize() + (1 if busy.locked() else 0)
    send(chat, progress_text(0, f"{ahead} ahead of you in the queue" if ahead else ""), mid=mid)
    jobs.put((chat, mid, src, mode))


def main():
    me = api("getMe")
    print(f"bot online: @{(me.get('result') or {}).get('username','?')}", flush=True)
    # a crash or restart can leave orphans behind
    for n in os.listdir(WORK):
        purge(os.path.join(WORK, n))
    print("[cleanup] workdir emptied on start", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    offset = None
    while True:
        r = api("getUpdates", offset=offset, timeout=50,
                allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(3); continue
        for u in r["result"]:
            offset = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    handle_callback(u["callback_query"]); continue
                msg = u.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if not chat:
                    continue
                if msg.get("video") or msg.get("video_note") or (
                        (msg.get("document") or {}).get("mime_type", "").startswith("video")):
                    handle_video(chat, msg)
                elif (msg.get("text") or "").startswith("/start"):
                    send(chat, WELCOME)
                elif msg.get("text"):
                    send(chat, "Send a video file and I handle the rest. /start — what I do.")
            except Exception as e:
                print(f"[update] {e}", flush=True)


if __name__ == "__main__":
    main()
