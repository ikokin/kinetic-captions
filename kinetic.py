#!/usr/bin/env python3
"""Kinetic captions for vertical video. No API keys, no tokens, no cloud.

    python3 kinetic.py input.mov [output.mp4]

One emphasis word is scaled up, the rest stay small, every word fades in on
the exact millisecond it is spoken. Everything is computed locally with
whisper.cpp and ffmpeg/libass.

The aspect ratio decides the layout:
    landscape (>= 1.5)  -> letterboxed into 9:16, text in the black bar above
    portrait  (<= 0.7)  -> full bleed, text in the upper third
    square              -> 4:5, text above

Environment:
    KIN_LANG        ru | en            (default ru)
    KIN_STYLE       white | yellow | sunny | orange   (default white)
    KIN_FONT        caption font       (default "SF Pro Text")
    KIN_FONT_ACCENT separate font for the emphasis words
    KIN_GRADE       on | off           (default on)
    KIN_PROFANITY   mask | drop | keep (default mask)
    KIN_EMPHASIS    llm                (opt in to model-picked emphasis)
    WHISPER_MODEL   path to a ggml model
    WHISPER_BIN     whisper.cpp binary (default whisper-cli)
"""
import json, os, re, shutil, subprocess, sys, tempfile

MODEL = os.environ.get(
    "WHISPER_MODEL",
    os.path.expanduser("~/.cache/whisper/ggml-large-v3-turbo.bin"))
WHISPER = os.environ.get("WHISPER_BIN", "whisper-cli")
LANG = os.environ.get("KIN_LANG", "ru")
FONT = os.environ.get("KIN_FONT", "SF Pro Text")

OUT_W, OUT_H = 1080, 1920
SIDE, RIGHT = 64, 170     # right margin keeps text clear of the Reels buttons
GAP, SMALL, BIG = 18, 58, 152
ASC = 0.78                # share of the font size above the baseline
LINE = 1.14               # line-height multiplier
MAX_W, MAX_S = 9, 6.0     # ceiling of words / seconds per phrase
MIN_W = 3                 # shorter phrases are merged into their neighbour
PAD = 0.28                # how long a phrase lingers after its last word

# --------------------------------------------------------------- language packs
# `width` is the mean glyph advance as a fraction of the font size, measured
# on SF Pro Bold. It drives the fit-to-width maths, so it is per-script.
PACKS = {
    "ru": dict(
        width=0.52,
        vowels=set("аеёиоуыэюя"),
        stop=set("""так ребят нас уже в я что это и а но или же бы ли не на с к по из у о
            от до за над под при вы мы он она они вам нам их его её тебе мне меня тебя себя
            там тут вот как когда где куда чем чём буду будет будешь были было есть еще ещё
            очень самом деле вообще да нет блин короче ну считаю думаю знаю вижу понимать
            хочу хотите будете просто этот эту всю через потому если чтобы тогда поэтому
            прям""".split()),
        glue=set("""в и на с к по из у о от до за над под при а но или же бы что который
            которые которая еще ещё уже прям вы мы я он она они не это так да нет ну мне
            вам""".split()),
        # roots chosen so that «художник», «хуже», «хутор», «мудрый» stay untouched
        profanity=r"""\b(?: ху[йёя]\w* | хуе(?:в|т|с)\w* | хуйн\w* | нахуй | похуй | дохуя
            | пизд\w* | пезд\w* | (?:[ео]|вы|за|на|по|у|до|при|с)?ёб\w*
            | \bеб(?:а|у|и|л|н|ё|ан)\w* | бля(?:дь|ди|дс|т|\b)\w*
            | муда[кчн]\w* | мудил\w* | пидор\w* | пидар\w* | гандон\w* | залуп\w*
            | мраз[ьи]\w* )\b""",
    ),
    "en": dict(
        width=0.50,
        vowels=set("aeiouy"),
        stop=set("""a an the and or but so if then than that this these those is are was
            were be been being am do does did done have has had will would can could should
            may might must i you he she it we they me him her us them my your his its our
            their to of in on at for with from by as up out off over under just really very
            like well okay ok yeah yes no not now here there what when where who how why
            about into some any all one""".split()),
        glue=set("""a an the and or but of in on at to for with from by as is are was were
            be that this it i you we they my your our their up out off""".split()),
        profanity=r"""\b(?: fuck\w* | shit\w* | bitch\w* | cunt\w* | asshole\w* | bastard\w*
            | dick(?:head|s)? | motherfuck\w* | bullshit | wank\w* | twat\w* )\b""",
    ),
}
PACK = PACKS.get(LANG, PACKS["ru"])
AVG, VOW, STOP, GLUE = PACK["width"], PACK["vowels"], PACK["stop"], PACK["glue"]

norm = lambda s: re.sub(r"[^\wЀ-ӿ-]", "", s.lower())

# ------------------------------------------------------------------- profanity
MAT = re.compile(PACK["profanity"], re.I | re.X)
PROFANITY = os.environ.get("KIN_PROFANITY", "mask")


def is_mat(t):
    return bool(MAT.search(t))


def clean(t):
    """Text as it will appear on screen: profanity masked, dropped or kept."""
    if PROFANITY == "keep" or not is_mat(t):
        return t
    if PROFANITY == "drop":
        return ""
    return MAT.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), t)

# ----------------------------------------------------------------- colour grade
# Measured against reference frames rather than eyeballed. The targets were:
# saturation 0.188 -> 0.276, shadows -7 -> +13 (warmer), highlights +32 -> +17
# (sunlit footage runs too hot). A parameter sweep found that warm shadows
# supply the missing saturation on their own, so no extra saturation is needed.
GRADE = ("colorbalance=rs=0.09:bs=-0.09:rh=-0.05:bh=0.05"
         if os.environ.get("KIN_GRADE", "on") != "off" else "")

# ---------------------------------------------------------------------- styles
# ASS colours are &HAABBGGRR — the byte order is reversed, not RGB.
STYLES = {
    # restrained documentary: white, no outline, a soft shadow
    "white":  dict(base="&H00FFFFFF", accent="&H00FFFFFF",
                   outline="&H00000000", bord=0, shadow=3),
    # the familiar social look: white body, pale yellow accent, black outline
    "yellow": dict(base="&H00FFFFFF", accent="&H006BF0FF",
                   outline="&H00000000", bord=6, shadow=0),
    "sunny":  dict(base="&H006BF0FF", accent="&H006BF0FF",
                   outline="&H00000000", bord=6, shadow=0),
    "orange": dict(base="&H00FFFFFF", accent="&H001A7AFF",
                   outline="&H00000000", bord=6, shadow=0),
}
STYLE = STYLES.get(os.environ.get("KIN_STYLE", "white"), STYLES["white"])
FONT_ACCENT = os.environ.get("KIN_FONT_ACCENT", "")

# Script faces render optically smaller at the same point size — low x-height.
# Without this correction the emphasis word stops reading as an emphasis word.
SCRIPT_SCALE = {
    "signpainter": 1.55, "brush script mt": 1.60, "snell roundhand": 1.75,
    "savoye let": 1.80, "marker felt": 1.25, "bradley hand": 1.45,
    "noteworthy": 1.25, "chalkboard se": 1.20, "comic sans ms": 1.20,
}
ACCENT_SCALE = float(os.environ.get(
    "KIN_ACCENT_SCALE", SCRIPT_SCALE.get(FONT_ACCENT.strip().lower(), 1.0)))
BORD_SMALL = max(0, STYLE["bord"] - 3)   # a full outline on small words is mud
ACC_AVG = 0.38 if ACCENT_SCALE > 1.1 else AVG   # script faces are narrower too


def is_proper(tok):
    """A capitalised Latin word — part of a proper name."""
    t = tok.strip("«»\"'()")
    return bool(t) and t[0].isupper() and t[0].isascii() and t[0].isalpha()


def bind_names(ws, group):
    """Adjacent capitalised Latin words are one unit: App Store, Apple Music."""
    units, cur = [], [group[0]]
    for i in group[1:]:
        if is_proper(ws[cur[-1]]["text"]) and is_proper(ws[i]["text"]):
            cur.append(i)
        else:
            units.append(cur); cur = [i]
    units.append(cur)
    return units


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def probe(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:stream_side_data=rotation",
             "-show_entries", "format=duration", "-of", "json", path])
    d = json.loads(r.stdout)
    st = d["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    # ffprobe reports STORAGE dimensions. Phone footage often carries a 90/270
    # rotation, which swaps them on screen. Skip this and a portrait clip is
    # mistaken for landscape and letterboxed into a tiny strip.
    rot = 0
    for sd in st.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(abs(float(sd["rotation"]))) % 360
    if rot in (90, 270):
        w, h = h, w
    return w, h, float(d["format"]["duration"])


def transcribe(wav, workdir):
    """whisper.cpp with one word per cue, so every word carries its own time."""
    out = os.path.join(workdir, "w")
    run([WHISPER, "-m", MODEL, "-f", wav, "-l", LANG, "-ml", "1", "-sow",
         "-osrt", "-of", out])

    def ts(s):
        h, m, rest = s.split(":"); sec, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000

    words = []
    for b in open(out + ".srt", encoding="utf-8").read().strip().split("\n\n"):
        L = [x for x in b.split("\n") if x.strip()]
        if len(L) < 3:
            continue
        a, c = L[1].split(" --> ")
        t = " ".join(L[2:]).strip()
        if t:
            words.append({"text": t, "start": round(ts(a), 3), "end": round(ts(c), 3)})
    return words


def split_phrases(ws):
    """Cut on pauses and punctuation; never strand a preposition at line end."""
    out, cur = [], [0]
    for i in range(1, len(ws)):
        dur = ws[i]["end"] - ws[cur[0]]["start"]
        hard = ws[i - 1]["text"].rstrip().endswith((".", "!", "?"))
        if hard or len(cur) >= MAX_W or dur > MAX_S:
            best, bs = None, -1
            for j in range(1, len(cur)):
                k = cur[j]
                g = ws[k]["start"] - ws[k - 1]["end"]
                if norm(ws[k - 1]["text"]) in GLUE:
                    g -= 0.30
                if ws[k - 1]["text"].rstrip().endswith((",", ".", "!", "?")):
                    g += 0.18
                if g > bs:
                    bs, best = g, j
            if hard or best is None or best < 2:
                out.append(cur); cur = [i]
            else:
                out.append(cur[:best]); cur = cur[best:] + [i]
        else:
            cur.append(i)
    out.append(cur)
    out = [g for g in out if g]
    merged = []                       # no phrase should be a single orphan word
    for g in out:
        if merged and len(g) < MIN_W and len(merged[-1]) + len(g) <= MAX_W + 2:
            merged[-1].extend(g)
        else:
            merged.append(g)
    return merged


def emphasis(ws, group):
    """Emphasis = the word held longer than its syllable count warrants.

    Speakers stretch the words that matter. Dividing the measured duration by
    an expected duration isolates that from words that are simply long, and
    from the natural lengthening of whatever lands before a pause.
    """
    last = group[-1]

    def score(i):
        w = ws[i]; t = norm(w["text"])
        if not t:
            return 0
        if is_mat(t):
            return 0                   # never blow profanity up across the frame
        syl = max(1, sum(c in VOW for c in t))
        s = (w["end"] - w["start"]) / (0.085 + syl * 0.135)
        if t in STOP:
            s *= 0.18
        if i == last:
            s *= 0.55                  # damp phrase-final lengthening
        if any(c.isdigit() for c in t):
            s *= 2.0
        if len(t) >= 9:
            s *= 1.15
        return s

    if os.environ.get("KIN_NO_EMPHASIS"):
        return set()
    k = 2 if len(group) >= 5 else 1
    picked = set(sorted(group, key=lambda i: -score(i))[:k])
    for unit in bind_names(ws, group):   # «App Store», never «App STORE»
        if len(unit) > 1 and picked & set(unit):
            picked |= set(unit)
    return picked


def layout_for(w, h):
    """Aspect ratio picks the layout and the band the text lives in."""
    ar = w / h
    if ar >= 1.5:                                  # landscape -> letterbox
        vh = round(OUT_W * h / w / 2) * 2
        vy = min(OUT_H - vh - 150, round((OUT_H - vh) * 0.70))  # below centre
        return dict(kind="letterbox", vw=OUT_W, vh=vh, vy=vy,
                    text_top=120, text_bottom=vy - 50)
    if ar <= 0.7:                                  # portrait -> full bleed
        return dict(kind="full", vw=OUT_W, vh=OUT_H, vy=0,
                    text_top=230, text_bottom=820)
    vh = round(OUT_W * h / w / 2) * 2              # square -> 4:5
    vy = (OUT_H - vh) // 2
    return dict(kind="letterbox", vw=OUT_W, vh=vh, vy=vy,
                text_top=120, text_bottom=vy - 40)


def llm_picks(ws, groups):
    """Optional: one batched model call picks the emphasis words instead.

    Returns None on any failure — a missing SDK, no key, a network blip — and
    the deterministic scorer takes over. Enabled with KIN_EMPHASIS=llm.
    """
    if os.environ.get("KIN_EMPHASIS") != "llm":
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import emphasis_llm
    except ImportError:
        return None
    got = emphasis_llm.pick([[ws[i]["text"] for i in g] for g in groups])
    if got is None:
        return None
    return [{g[w] for w in got[gi] if w < len(g)} for gi, g in enumerate(groups)]


def build_ass(ws, groups, lay, dur, path, picks=None):
    """One ASS event per phrase; each word reveals itself with its own \\t().

    Layout, kerning and line breaks are left to libass. Computing them by hand
    produced ragged gaps and words drawn on top of each other.
    """
    avail = lay["text_bottom"] - lay["text_top"]
    max_lines = max(1, int(avail / (BIG * LINE)))
    ev = []
    for gi, g in enumerate(groups):
        emph = picks[gi] if picks else emphasis(ws, g)
        for unit in bind_names(ws, g):
            if len(unit) > 1 and emph & set(unit):
                emph = emph | set(unit)
        t0 = ws[g[0]]["start"]
        end = min(ws[g[-1]]["end"] + PAD, dur)
        if gi + 1 < len(groups):
            end = min(end, ws[groups[gi + 1][0]]["start"] - 0.05)
        if end <= t0:
            continue
        # Aim for the same OPTICAL size a grotesque would have, then check the
        # longest emphasis word still fits using this face's real glyph width.
        longest = max((len(ws[i]["text"]) for i in emph), default=1)
        want = BIG * ACCENT_SCALE
        cap = (OUT_W - SIDE - RIGHT) / (longest * ACC_AVG)
        big = int(max(SMALL + 20, min(want, cap)))
        est_lines = 1 + sum(len(ws[i]["text"]) for i in g) * AVG * SMALL // (OUT_W - SIDE - RIGHT)
        if est_lines > max_lines:
            big = int(big * max(0.55, max_lines / est_lines))

        parts = []
        for i in g:
            t = clean(ws[i]["text"]).replace("{", "(").replace("}", ")")
            if not t:
                continue
            hot = i in emph
            fs = big if hot else SMALL
            a = max(0, int((ws[i]["start"] - t0) * 1000))
            tags = (f"\\fs{fs}\\b{1 if hot else 0}"
                    f"\\c{STYLE['accent'] if hot else STYLE['base']}"
                    f"\\3c{STYLE['outline']}"
                    f"\\bord{STYLE['bord'] if hot else BORD_SMALL}")
            if hot and FONT_ACCENT:
                tags += f"\\fn{FONT_ACCENT}"
            tags += f"\\alpha&HFF&\\t({a},{a + 260},\\alpha&H00&)"
            parts.append(f"{{{tags}}}{t}")
        if not parts:
            continue

        # A hard space inside a proper name stops «App Store» splitting lines.
        glued = set()
        for unit in bind_names(ws, g):
            for _, b_ in zip(unit, unit[1:]):
                glued.add(g.index(b_))
        line = parts[0]
        for k in range(1, len(parts)):
            line += ("\\h" if k in glued else " ") + parts[k]
        ev.append((t0, end, line))

    def t(v):
        return f"{int(v//3600)}:{int(v%3600//60):02d}:{v%60:05.2f}"

    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: K,{FONT},{SMALL},{STYLE["base"]},{STYLE["base"]},{STYLE["outline"]},&HB4000000,0,0,0,0,100,100,-1,0,1,{STYLE["bord"]},{STYLE["shadow"]},1,{SIDE},{RIGHT},{OUT_H - lay["text_bottom"]},204

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(hdr)
        for a, b, tx in ev:
            f.write(f"Dialogue: 0,{t(a)},{t(b)},K,,0,0,0,,{tx}\n")
    return len(ev)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + "_kinetic.mp4"
    for tool in ("ffmpeg", "ffprobe", WHISPER):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found in PATH")
    if "ass" not in run(["ffmpeg", "-hide_banner", "-filters"]).stdout:
        sys.exit("this ffmpeg was built without libass — it cannot draw subtitles")
    if not os.path.exists(MODEL):
        sys.exit(f"whisper model not found: {MODEL}\nset WHISPER_MODEL to its path")

    w, h, dur = probe(src)
    lay = layout_for(w, h)
    print(f"source {w}x{h} ({w/h:.2f}) -> layout: {lay['kind']}, "
          f"frame {lay['vw']}x{lay['vh']} at y={lay['vy']}")

    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "a.wav")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])
        ws = transcribe(wav, tmp)
        print(f"words: {len(ws)}")
        groups = split_phrases(ws)
        print(f"phrases: {len(groups)}")
        picks = llm_picks(ws, groups)
        if picks is not None:
            print("emphasis: chosen by model")
        ass = os.path.join(tmp, "k.ass")
        print(f"ass events: {build_ass(ws, groups, lay, dur, ass, picks)}")

        if lay["kind"] == "letterbox":
            vf = (f"scale={lay['vw']}:{lay['vh']}:flags=lanczos,"
                  f"pad={OUT_W}:{OUT_H}:0:{lay['vy']}:black,")
        else:
            vf = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                  f"crop={OUT_W}:{OUT_H},")
        if GRADE:
            vf += GRADE + ","        # grade before the text, or the type tints
        vf += (f"scale=in_range=full:out_range=limited,format=yuv420p,"
               f"setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
               f"ass={ass.replace(':', r'\:')}")

        run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf", vf,
             "-color_range", "tv", "-colorspace", "bt709",
             "-color_primaries", "bt709", "-color_trc", "bt709",
             "-c:v", "libx264", "-crf", "20", "-preset", "medium",
             "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst])
    print(f"done: {dst}")


if __name__ == "__main__":
    main()
