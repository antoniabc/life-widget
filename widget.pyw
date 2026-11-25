"""Life widget: spokojny stoper zadań (karta z pierścieniem postępu).

Własne zadania (nazwa + planowany czas) -> start / pauza / przedłuż / zalicz / usuń.
Po zaliczeniu wpis trafia do historii (data/history.jsonl) z błędem planowania
(faktyczny czas minus plan pierwotny). Opcjonalnie import dzisiejszych zadań z Google Calendar.

Uruchomienie:  pythonw widget.pyw        (tryb testowy: --demo)
"""
import csv
import ctypes
import datetime as dt
import json
import math
import os
import queue
import re
import socket
import sys
import threading
import uuid
import webbrowser
import tkinter as tk
from functools import lru_cache
from pathlib import Path
from tkinter import font as tkfont
from tkinter import colorchooser, messagebox, simpledialog

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

from cloud_sync import CloudSync
from widget_web import WebError, WidgetWeb

HERE = Path(__file__).resolve().parent
LIFE = HERE.parent.parent
DEMO = "--demo" in sys.argv
DATA = HERE / ("data-demo" if DEMO else "data")
STORE = DATA / "tasks.json"
HISTORY = DATA / "history.jsonl"
CATS = DATA / "categories.json"
CSV_OUT = DATA / "historia.csv"
TIMELOG = LIFE / ("tools/widget/timelog-demo.jsonl" if DEMO else "timelog.jsonl")
DAILYLOG = LIFE / ("tools/widget/log-demo.md" if DEMO else "log.md")
CONFIG = HERE / ("config-demo.json" if DEMO else "config.json")
CREDS = HERE / "credentials.json"
TOKEN = HERE / "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
PREFIXES = ("①", "②", "③", "○")
MARKS = ("✓", "½", "✗")

# paleta ze zdjęcia: chłodny fiolet na jasnej karcie
CARD, TRACK = "#faf8fa", "#ece8f0"
P_TOP, P_BOT = "#bcaed6", "#8c77ab"
B_TOP, B_BOT = "#a99bc4", "#8a75a9"
WARN_TOP, WARN_BOT = "#f2c38f", "#d98a4e"
SEC_BG, SEC_FG = "#efedf0", "#8f89a0"
W, H = 360, 480
MIN_UI, MAX_UI = 0.7, 2.0
MINI_W, MINI_H = 210, 60
MAX_BLOCK = 8 * 3600     # dłuższy ciągły blok traktuję jako zapomniany stoper
WEEKDAYS = ["pn", "wt", "śr", "czw", "pt", "sob", "nd"]
EMPTY_DAY = "#e8bdb8"    # dzień bez pracy na wykresie
# kolory kategorii: spokojne, dalekie od pomarańczu "ponad plan"
PALETTE = [("fiolet", "#8c77ab"), ("morski", "#4f9a94"), ("niebieski", "#5b7fc4"), ("róż", "#c46f9b"),
           ("oliwka", "#7f9a52"), ("piasek", "#c2a053"), ("śliwka", "#7d4f73"), ("grafit", "#6b6577")]
DEFAULT_CATS = [{"id": "arch", "name": "Architektura", "color": "#8c77ab"},
                {"id": "apps", "name": "Aplikacje", "color": "#4f9a94"}]

SKINS = {"lavender": {"name": "Lawenda", "card": "#f1edf6", "ink": "#423c51", "dim": "#685b80",
                      "muted": "#716883", "track": "#e0d9ea", "row": "#ece6f2", "row_sel": "#e2d8ee",
                      "border": "#d9cee7", "font": "Cambria", "warn": "#a55419", "good": "#3f7953"},
         "campfire": {"name": "Ognisko RPG", "card": "#211b32", "ink": "#fff1dc", "dim": "#d2beda",
                      "muted": "#b9a5c7", "track": "#423454", "row": "#2f2642", "row_sel": "#443354",
                      "border": "#5a466c", "font": "Consolas", "warn": "#ffc382", "good": "#acd6a9"}}


# ---------- helpers ----------
def now():
    return dt.datetime.now().astimezone()


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def daily_seconds(running=None, catmap=None):
    """{data: {kategoria: sekundy pracy}} z timelog.jsonl (start -> pause/done/cancel), bloki przez północ
    dzielone na dni. Kategoria bloku: aktualna kategoria zadania z catmap (więc zmiana kategorii działa też
    wstecz), w razie braku ta zapisana w timelogu. running = (start_dt, end_dt, kategoria) trwającego bloku."""
    out = {}
    catmap = catmap or {}

    def add(a, b, cat):
        if (b - a).total_seconds() > MAX_BLOCK:     # zapomniany stoper, nie realna praca
            return
        while a < b:
            nxt = min(b, (a + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
            day = out.setdefault(a.date(), {})
            day[cat] = day.get(cat, 0) + (nxt - a).total_seconds()
            a = nxt
    openb = {}
    if TIMELOG.exists():
        for line in TIMELOG.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                ts = dt.datetime.fromisoformat(e["ts"]).astimezone()
            except Exception:
                continue
            if e.get("action") == "start":
                openb[e["id"]] = (ts, e.get("cat"))
            elif e.get("action") in ("pause", "done", "cancel") and e["id"] in openb:
                a, cat = openb.pop(e["id"])
                add(a, ts, catmap.get(e["id"]) or cat or e.get("cat"))
    if running:
        add(*running)
    return out


def load_categories():
    data = load_json(CATS, None)
    if not isinstance(data, dict) or not data.get("categories"):
        data = {"categories": [dict(c) for c in DEFAULT_CATS]}
        save_json(CATS, data)
    return data


def rewrite_history(fn):
    """fn(wpis) zmienia wpis w miejscu i zwraca True, jeśli coś zmieniło. Zapis atomowy; zwraca liczbę zmian."""
    if not HISTORY.exists():
        return 0
    out, n = [], 0
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if fn(e):
            n += 1
            out.append(json.dumps(e, ensure_ascii=False))
        else:
            out.append(line)
    if n:
        tmp = HISTORY.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        tmp.replace(HISTORY)
    return n


def read_history():
    out = []
    if HISTORY.exists():
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def split_mark(summary):
    s = summary.strip()
    mark = ""
    while s and s[0] in MARKS:
        mark = s[0]
        s = s[1:].lstrip()
    return mark, s


def parse_planned(title):
    """Planowany czas w sekundach z tytułu: '3h', '1,5 h', '10 min'."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*h(?![a-ząćęłńóśźż])", title, re.I)
    if m:
        return float(m.group(1).replace(",", ".")) * 3600
    m = re.search(r"(\d+)\s*min", title, re.I)
    if m:
        return int(m.group(1)) * 60
    return None


def parse_duration(text):
    """'45', '45 min', '1h', '1h30', '1,5h', '1:30', '0:45:10' -> sekundy (liczba bez jednostki = minuty)."""
    s = text.strip().lower().replace(",", ".")
    if not s:
        return None
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", s)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+)\s*(?:min|m)?)?", s)
    if m and (m.group(1) or m.group(2)):
        return int(float(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60)
    return None


def fmt_hms(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_hm(sec):
    sec = int(round(max(0, sec) / 60)) * 60
    return f"{sec // 3600}:{sec % 3600 // 60:02d}"


def fmt_dur(sec):
    sec = int(round(abs(sec)))
    if sec < 60:
        return f"{sec} s"
    m = round(sec / 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def fmt_signed(sec):
    return ("+" if sec >= 0 else "−") + fmt_dur(sec)


def append_daily_log(day, title, seconds, note):
    text = DAILYLOG.read_text(encoding="utf-8") if DAILYLOG.exists() else "# Dziennik dzienny\n"
    if not text.endswith("\n"):
        text += "\n"
    line = f"{day.isoformat()}: ✓ {title} — {fmt_hm(seconds)} h"
    if note:
        line += f" · następny krok: {note}"
    DAILYLOG.write_text(text + line + "\n", encoding="utf-8")


# ---------- grafika (PIL, supersampling) ----------
def hexrgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def vgrad(w, h, top, bot):
    g = Image.linear_gradient("L").resize((w, h))
    return ImageOps.colorize(g, hexrgb(top), hexrgb(bot))


@lru_cache(maxsize=8)
def soft_dial(size, bg):
    n = size * 3
    def ellipse(inset, dy=0, blur=0):
        mask = Image.new("L", (n, n))
        a = n * inset
        ImageDraw.Draw(mask).ellipse((a, a + dy * n, n - a, n - a + dy * n), fill=255)
        return mask.filter(ImageFilter.GaussianBlur(n * blur)) if blur else mask

    base = Image.new("RGB", (n, n), bg)
    base.paste("#c9bfd3", mask=ellipse(.060, .017, .020))
    base.paste(vgrad(n, n, "#fffcff", "#d9d0e3"), mask=ellipse(.062))
    interior = ellipse(.127)
    base.paste(vgrad(n, n, "#b4aabc", "#fcf9ff"), mask=interior)
    field = Image.new("RGB", (n, n), "#eee8f3")
    field.paste(vgrad(n, n, "#e5ddec", "#f9f5fa"), mask=ellipse(.139, .017, .024))
    base.paste(field, mask=interior)
    return base


def ring_image(size, width, p, top, bot, bg=CARD, track_color=TRACK, soft=False):
    ss = 3
    n, w = size * ss, width * ss
    inset = round(n * .094) if soft else 0
    box = [inset, inset, n - inset - 1, n - inset - 1]
    if soft:
        out = soft_dial(size, bg).copy()
    else:
        out = Image.new("RGB", (n, n), bg)
        ImageDraw.Draw(out).ellipse(box, outline=track_color, width=w)
    if p > 0:
        m = Image.new("L", (n, n), 0)
        d = ImageDraw.Draw(m)
        if p >= 0.999:
            d.ellipse(box, outline=255, width=w)
        else:
            end = -90 + 360 * p
            d.arc(box, -90, end, fill=255, width=w)
            r, c = (n - w) / 2 - inset, n / 2
            for a in (-90, end):
                x, y = c + r * math.cos(math.radians(a)), c + r * math.sin(math.radians(a))
                d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=255)
        out.paste(vgrad(n, n, top, bot), mask=m)
    return out.resize((size, size), Image.LANCZOS)


def rr_image(w, h, r, fill, bg=CARD):
    ss = 4
    im = Image.new("RGB", (w * ss, h * ss), hexrgb(bg))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], r * ss, fill=hexrgb(fill))
    return im.resize((w, h), Image.LANCZOS)


def lighten(color, k):
    """Miesza kolor z białym (k=0 bez zmian, k=1 biały)."""
    return "#%02x%02x%02x" % tuple(round(c + (255 - c) * k) for c in hexrgb(color))


def bar_image(w, h, r, segs, bg=CARD, vertical=True):
    """Zaokrąglony słupek złożony z odcinków [(udział, kolor)], od dołu (pionowo) albo od lewej."""
    ss = 4
    bw, bh = max(1, w) * ss, max(1, h) * ss
    im = Image.new("RGB", (bw, bh), hexrgb(bg))
    fill = Image.new("RGB", (bw, bh), hexrgb(bg))
    d = ImageDraw.Draw(fill)
    length = bh if vertical else bw
    pos = 0
    for i, (frac, color) in enumerate(segs):
        seg = length - pos if i == len(segs) - 1 else round(frac * length)
        if vertical:
            d.rectangle([0, bh - pos - seg, bw, bh - pos], fill=hexrgb(color))
            if i:
                d.rectangle([0, bh - pos - ss, bw, bh - pos], fill=hexrgb(bg))
        else:
            d.rectangle([pos, 0, pos + seg, bh], fill=hexrgb(color))
            if i:
                d.rectangle([pos, 0, pos + ss, bh], fill=hexrgb(bg))
        pos += seg
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], max(0, r) * ss, fill=255)
    im.paste(fill, mask=mask)
    return im.resize((max(1, w), max(1, h)), Image.LANCZOS)


def dot_image(d, color, bg=None):
    """Kółko koloru kategorii; bg=None -> przezroczyste tło (do menu)."""
    ss = 4
    n = max(2, d) * ss
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0)) if bg is None else Image.new("RGB", (n, n), hexrgb(bg))
    ImageDraw.Draw(im).ellipse([0, 0, n - 1, n - 1], fill=hexrgb(color) + ((255,) if bg is None else ()))
    return im.resize((max(2, d), max(2, d)), Image.LANCZOS)


def btn_image(kind, w, h, primary, bg=CARD, skin=None):
    """Przycisk-pigułka/koło z ikoną (pause/play/check/plus/stop) lub bez ikony (kind=None)."""
    ss = 4
    pad = min(9, h // 5) if skin == "lavender" or primary else 0
    bw, bh = (w + 2 * pad) * ss, (h + 2 * pad) * ss
    im = Image.new("RGB", (bw, bh), hexrgb(bg))
    box = [pad * ss, pad * ss, (pad + w) * ss - 1, (pad + h) * ss - 1]
    rad = min(w, h) * ss // (3 if skin == "lavender" else 2)
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=rad, fill=255)
    if skin == "lavender":
        sh = Image.new("L", (bw, bh))
        ImageDraw.Draw(sh).rounded_rectangle([box[0], box[1] + 4 * ss, box[2], box[3] + 4 * ss],
                                            radius=rad, fill=180)
        im.paste("#bdb1ca", mask=sh.filter(ImageFilter.GaussianBlur(4 * ss)))
        im.paste(vgrad(bw, bh, "#fffaff", "#e1d9eb"), mask=mask)
        ImageDraw.Draw(im).rounded_rectangle(box, radius=rad, outline="#f8f3fa", width=ss)
    elif skin == "campfire":
        im.paste("#715790" if primary else "#3f3154", mask=mask)
        ImageDraw.Draw(im).rounded_rectangle(box, radius=rad, outline="#9e82b0" if primary else "#624c76",
                                            width=ss)
    elif primary:
        sh = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(sh).rounded_rectangle([box[0], box[1] + 5 * ss, box[2], box[3] + 5 * ss], radius=rad, fill=120)
        sh = sh.filter(ImageFilter.GaussianBlur(5 * ss))
        im.paste(Image.new("RGB", (bw, bh), hexrgb("#a698c0")), mask=sh)
        im.paste(vgrad(bw, bh, B_TOP, B_BOT), mask=mask)
    else:
        im.paste(Image.new("RGB", (bw, bh), hexrgb(SEC_BG)), mask=mask)
    d = ImageDraw.Draw(im)
    cx, cy, u = bw / 2, bh / 2, h * ss
    if skin == "lavender":
        col = "#755b9d" if primary else "#625773"
    elif skin == "campfire":
        col = "#fff1dc"
    else:
        col = "#ffffff" if primary else SEC_FG
    if kind == "pause":
        bwid, bhei, gap = .11 * u, .36 * u, .14 * u
        for sx in (-1, 1):
            x0 = cx + (gap / 2 if sx > 0 else -gap / 2 - bwid)
            d.rounded_rectangle([x0, cy - bhei / 2, x0 + bwid, cy + bhei / 2], radius=bwid / 3, fill=col)
    elif kind == "play":
        d.polygon([(cx - .1 * u, cy - .19 * u), (cx - .1 * u, cy + .19 * u), (cx + .21 * u, cy)], fill=col)
    elif kind == "check":
        pts = [(cx - .17 * u, cy + .01 * u), (cx - .05 * u, cy + .13 * u), (cx + .18 * u, cy - .12 * u)]
        lw = int(.075 * u)
        d.line(pts, fill=col, width=lw, joint="curve")
        for x, y in (pts[0], pts[2]):
            d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=col)
    elif kind == "plus":
        lw = int(.075 * u)
        d.line([(cx - .15 * u, cy), (cx + .15 * u, cy)], fill=col, width=lw)
        d.line([(cx, cy - .15 * u), (cx, cy + .15 * u)], fill=col, width=lw)
    elif kind == "stop":
        d.rounded_rectangle([cx - .13 * u, cy - .13 * u, cx + .13 * u, cy + .13 * u], radius=.04 * u, fill=col)
    elif kind == "list":
        for y in (-.16 * u, 0, .16 * u):
            d.ellipse([cx - .18 * u, cy + y - .03 * u, cx - .12 * u, cy + y + .03 * u], fill=col)
            d.line([(cx - .06 * u, cy + y), (cx + .19 * u, cy + y)], fill=col, width=max(1, int(.04 * u)))
    elif kind == "more":
        radius = .045 * u
        for x in (-.15 * u, 0, .15 * u):
            d.ellipse([cx + x - radius, cy - radius, cx + x + radius, cy + radius], fill=col)
    return im.resize((bw // ss, bh // ss), Image.LANCZOS)


def make_icon(path):
    ss, n = 4, 256
    im = Image.new("RGBA", (n * ss, n * ss), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, n * ss - 1, n * ss - 1], 56 * ss, fill=hexrgb("#faf8fa") + (255,))
    ring = ring_image(176, 22, .78, P_TOP, P_BOT)
    ring = ring.resize((176 * ss, 176 * ss), Image.LANCZOS)
    r = Image.new("L", ring.size, 0)
    ImageDraw.Draw(r).ellipse([0, 0, ring.size[0] - 1, ring.size[1] - 1], fill=255)
    ring_rgba = ring.convert("RGBA")
    im.paste(ring_rgba, (40 * ss, 40 * ss), r)
    d = ImageDraw.Draw(im)
    bw, bh = 16 * ss, 54 * ss
    for x0 in (n * ss // 2 - 22 * ss, n * ss // 2 + 6 * ss):
        d.rounded_rectangle([x0, n * ss // 2 - bh // 2, x0 + bw, n * ss // 2 + bh // 2], radius=6 * ss, fill=hexrgb(B_BOT) + (255,))
    im = im.resize((n, n), Image.LANCZOS)
    im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


# ---------- Google Calendar (opcjonalny import i oznaczanie ✓) ----------
class Calendar:
    def __init__(self):
        self.service = None

    def _connect(self):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
        if creds and not creds.valid and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None
        if not creds or not creds.valid:
            if not CREDS.exists():
                raise FileNotFoundError("Brak credentials.json (instrukcja: tools/widget/README.md)")
            creds = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES).run_local_server(port=0)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    def _call(self, fn):
        if self.service is None:
            self._connect()
        try:
            return fn()
        except Exception as e:
            if "invalid_grant" in str(e) or "401" in str(e):
                self.service = None
                TOKEN.unlink(missing_ok=True)
                self._connect()
                return fn()
            raise

    def tasks(self, day):
        start = dt.datetime.combine(day, dt.time()).astimezone()
        end = start + dt.timedelta(days=1)

        def fn():
            return self.service.events().list(
                calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=100).execute()

        out = []
        for e in self._call(fn).get("items", []):
            s, en = e.get("start", {}).get("date"), e.get("end", {}).get("date")
            if s and en and not (s <= day.isoformat() < en):
                continue
            mark, base = split_mark(e.get("summary", ""))
            if base.startswith(PREFIXES) and not mark:
                out.append({"id": e["id"], "base": base, "planned": parse_planned(base)})
        out.sort(key=lambda t: (PREFIXES.index(t["base"][0]), t["base"]))
        return out

    def set_mark(self, cal, mark):
        summary = f"{mark} {cal['base']}" if mark else cal["base"]
        self._call(lambda: self.service.events().patch(
            calendarId="primary", eventId=cal["id"], body={"summary": summary}).execute())


class DemoCalendar:
    def tasks(self, day):
        return [
            {"id": "demo1", "base": "① Długa 3h: rzut dachu dla klientki", "planned": 10800},
            {"id": "demo2", "base": "② Przykładowe zadanie 1h", "planned": 3600},
        ]

    def set_mark(self, cal, mark):
        pass


# ---------- UI ----------
class App:
    def __init__(self):
        self.cfg = load_json(CONFIG, {})
        self.skin_id = self.cfg.get("skin", "lavender")
        if self.skin_id not in SKINS:
            self.skin_id = "lavender"
        self.skin = SKINS[self.skin_id]
        self.web = None
        self.modal_depth = 0
        self.cloud = None if DEMO else CloudSync(
            HERE, {"store": STORE, "history": HISTORY, "cats": CATS, "timelog": TIMELOG}, CONFIG)
        self.store = load_json(STORE, {"tasks": [], "current": None})
        self.cats = load_categories()
        self.add_cat = None
        self.add_draft = None
        self.dots = {}
        self.menu = None
        self.cal = DemoCalendar() if DEMO else Calendar()
        self.q = queue.Queue()
        self.view = "focus"
        self.prev_view = "focus"
        self.mini = bool(self.cfg.get("mini", False))
        self.w, self.h = (MINI_W, MINI_H) if self.mini else (W, H)
        self.scroll = 0
        self.chart_mode = "week"
        self.chart_off = 0
        self.moved = False
        self.keep = []
        self.bcache = {}
        self.notified = set()
        self.last_entry = None
        self.last_seen_saved = 0
        self.ring_key = None
        self.ring_photo = None
        self.ring_item = self.time_t = self.sub_t = self.plan_t = None

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        r = self.root = tk.Tk()
        r.title("Life")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=self.skin["card"])
        self.dpi = r.winfo_fpixels("1i") / 96
        self.ui = min(MAX_UI, max(MIN_UI, float(self.cfg.get("ui_scale", 1.0))))
        self.cv = tk.Canvas(r, bg=self.skin["card"], highlightthickness=0, bd=0)
        self.cv.pack()
        self._resize()
        x, y = self.cfg.get("x", 100), self.cfg.get("y", 100)
        vx, vy, vw, vh = r.winfo_vrootx(), r.winfo_vrooty(), r.winfo_vrootwidth(), r.winfo_vrootheight()
        x = min(max(x, vx), vx + vw - self.p(self.w))
        y = min(max(y, vy), vy + vh - self.p(self.h))
        r.geometry(f"+{x}+{y}")
        r.update_idletasks()
        self._round_corners()

        cv = self.cv
        cv.bind("<ButtonPress-1>", self._drag_start)
        cv.bind("<B1-Motion>", self._drag_move)
        cv.bind("<ButtonRelease-1>", self._drag_end)
        cv.bind("<MouseWheel>", self._wheel)
        r.bind("<Control-MouseWheel>", self._zoom)

        self._migrate_cats()
        self._recover()
        cur = self.cur()
        self.store["current"] = cur["id"] if cur else None
        if self.mini:
            self.view = "mini"
        self.draw()
        self.tick()

    # --- okno ---
    @property
    def S(self):
        return self.ui * self.dpi

    def p(self, v):
        return int(round(v * self.S))

    def _resize(self):
        self.cv.config(width=self.p(self.w), height=self.p(self.h))
        self.bcache.clear()

    def _round_corners(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
            red, green, blue = hexrgb(self.skin["border"])
            border = red | green << 8 | blue << 16
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ctypes.c_int(border)), 4)
        except Exception:
            pass

    def _drag_start(self, e):
        self.moved = False
        self._d0 = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, e):
        x0, y0, wx, wy = self._d0
        if abs(e.x_root - x0) + abs(e.y_root - y0) > 5:
            self.moved = True
        if self.moved:
            self.root.geometry(f"+{wx + e.x_root - x0}+{wy + e.y_root - y0}")

    def _drag_end(self, e):
        if self.moved:
            self.cfg.update(x=self.root.winfo_x(), y=self.root.winfo_y())
            save_json(CONFIG, self.cfg)

    def _zoom(self, e):
        self.ui = round(min(MAX_UI, max(MIN_UI, self.ui + (0.1 if e.delta > 0 else -0.1))), 2)
        self.cfg["ui_scale"] = self.ui
        save_json(CONFIG, self.cfg)
        self._resize()
        self.draw()

    def _wheel(self, e):
        if self.view in ("list", "history"):
            self.scroll = max(0, self.scroll + (-1 if e.delta > 0 else 1))
            self.draw()

    # --- dane ---
    def save(self):
        save_json(STORE, self.store)
        if self.cloud:
            self.cloud.poke(force=True)

    def cur(self):
        tasks = self.store["tasks"]
        for t in tasks:
            if t["id"] == self.store.get("current"):
                return t
        return tasks[0] if tasks else None

    def running(self):
        return next((t for t in self.store["tasks"] if t.get("run_start")), None)

    def elapsed(self, t):
        s = t["elapsed"]
        if t.get("run_start"):
            s += (now() - dt.datetime.fromisoformat(t["run_start"])).total_seconds()
        return s

    def _tlog(self, t, action, **extra):
        append_jsonl(TIMELOG, {"ts": now().isoformat(), "date": now().date().isoformat(), "id": t["id"],
                               "title": t["title"], "action": action, "cat": t.get("cat"), **extra})

    # --- kategorie ---
    def cat(self, cid):
        cats = self.cats["categories"]
        return next((c for c in cats if c["id"] == cid), cats[0])

    def last_cat(self):
        return self.cat(self.cfg.get("last_cat"))["id"]

    def save_cats(self):
        save_json(CATS, self.cats)

    def _migrate_cats(self):
        """Zadania i wpisy historii bez kategorii trafiają do pierwszej kategorii (Architektura)."""
        first = self.cats["categories"][0]["id"]
        changed = False
        for t in self.store["tasks"]:
            if not t.get("cat"):
                t["cat"] = first
                changed = True
        if changed:
            self.save()
        rewrite_history(lambda e: not e.get("cat") and (e.__setitem__("cat", first) or True))

    def catmap(self):
        m = {h["id"]: h["cat"] for h in read_history() if h.get("id") and h.get("cat")}
        m.update({t["id"]: t.get("cat") for t in self.store["tasks"]})
        return m

    def work_by_day(self, live=True):
        """{data: {id kategorii: sekundy}}; nieznane kategorie liczone do pierwszej."""
        r = self.running() if live else None
        run = (dt.datetime.fromisoformat(r["run_start"]).astimezone(), now(), r.get("cat")) if r else None
        out = {}
        for d, per in daily_seconds(run, self.catmap()).items():
            day = out.setdefault(d, {})
            for cid, s in per.items():
                k = self.cat(cid)["id"]
                day[k] = day.get(k, 0) + s
        return out

    def segs(self, per):
        tot = sum(per.values())
        return [(per[c["id"]] / tot, c["color"]) for c in self.cats["categories"] if per.get(c["id"], 0) > 0] if tot else []

    def ring_colors(self, t):
        c = self.cat(t.get("cat"))["color"]
        return lighten(c, .38), c

    def dot(self, color, d=12):
        """Kółko do menu (trzymane w pamięci, bo Tk gubi obrazki bez referencji)."""
        key = (color, d)
        if key not in self.dots:
            self.dots[key] = ImageTk.PhotoImage(dot_image(self.p(d), color))
        return self.dots[key]

    def popup(self, m, e=None):
        if self.menu is not None:
            try:
                self.menu.destroy()
            except tk.TclError:
                pass
        self.menu = m
        x, y = (e.x_root, e.y_root) if e else (self.root.winfo_pointerx(), self.root.winfo_pointery())
        m.tk_popup(x, y)

    def cat_menu(self, current, pick, e=None, extra=()):
        """Menu wyboru kategorii: lista, nowa kategoria, edycja kategorii i opcjonalne dodatkowe pozycje."""
        m = tk.Menu(self.root, tearoff=0, font=(self.skin["font"], 11))
        for c in self.cats["categories"]:
            m.add_command(label="  " + c["name"], image=self.dot(c["color"]), compound="left",
                          accelerator="✓" if c["id"] == current else "", command=lambda cid=c["id"]: pick(cid))
        m.add_separator()
        m.add_command(label="Nowa kategoria…", command=lambda: self.new_category(pick))
        m.add_cascade(label="Edytuj kategorie", menu=self.manage_menu(m))
        if extra:
            m.add_separator()
            for label, cmd in extra:
                m.add_command(label=label, command=cmd)
        self.popup(m, e)

    def manage_menu(self, parent):
        sub = tk.Menu(parent, tearoff=0, font=(self.skin["font"], 11))
        for c in self.cats["categories"]:
            sub.add_cascade(label="  " + c["name"], image=self.dot(c["color"]), compound="left",
                            menu=self.edit_cat_menu(sub, c))
        return sub

    def edit_cat_menu(self, parent, c):
        cm = tk.Menu(parent, tearoff=0, font=(self.skin["font"], 11))
        cm.add_command(label="Zmień nazwę…", command=lambda: self.rename_category(c))
        col = tk.Menu(cm, tearoff=0, font=(self.skin["font"], 11))
        for name, hexc in PALETTE:
            col.add_command(label="  " + name, image=self.dot(hexc), compound="left",
                            accelerator="✓" if hexc == c["color"] else "",
                            command=lambda h=hexc: self.recolor_category(c, h))
        col.add_separator()
        col.add_command(label="Własny kolor…", command=lambda: self.custom_color(c))
        cm.add_cascade(label="Kolor", menu=col)
        cm.add_separator()
        cm.add_command(label="Usuń kategorię…", command=lambda: self.delete_category(c))
        return cm

    def new_category(self, pick=None):
        name = self.modal(simpledialog.askstring, "Nowa kategoria", "Nazwa kategorii:")
        name = (name or "").strip()
        if not name:
            return
        cats = self.cats["categories"]
        same = next((c for c in cats if c["name"].lower() == name.lower()), None)
        if same:
            c = same
        else:
            used = {x["color"] for x in cats}
            color = next((h for _, h in PALETTE if h not in used), PALETTE[len(cats) % len(PALETTE)][1])
            c = {"id": uuid.uuid4().hex[:8], "name": name, "color": color}
            cats.append(c)
            self.save_cats()
        if pick:
            pick(c["id"])
        else:
            self.draw()
        if not same:
            self.toast(f"Dodano „{name}” · kolor zmienisz w Edytuj kategorie")

    def rename_category(self, c):
        name = self.modal(simpledialog.askstring, "Zmień nazwę kategorii", "Nazwa kategorii:", initialvalue=c["name"])
        name = (name or "").strip()
        if name and name != c["name"]:
            c["name"] = name
            self.save_cats()
            self.redraw_keep()

    def recolor_category(self, c, color):
        c["color"] = color
        self.save_cats()
        self.redraw_keep()

    def custom_color(self, c):
        res = self.modal(colorchooser.askcolor, color=c["color"], title=f"Kolor: {c['name']}")
        if res and res[1]:
            self.recolor_category(c, res[1].lower())

    def delete_category(self, c):
        cats = self.cats["categories"]
        if len(cats) < 2:
            return self.toast("To ostatnia kategoria, nie można jej usunąć", self.skin["warn"])
        target = next(x for x in cats if x is not c)
        n = sum(1 for t in self.store["tasks"] if t.get("cat") == c["id"])
        n += sum(1 for h in read_history() if h.get("cat") == c["id"])
        if not self.modal(messagebox.askyesno, "Usuń kategorię",
                          f"Usunąć kategorię „{c['name']}”?\n\nZadania w niej ({n}) przejdą do „{target['name']}”."):
            return
        for t in self.store["tasks"]:
            if t.get("cat") == c["id"]:
                t["cat"] = target["id"]
        self.save()
        rewrite_history(lambda e: e.get("cat") == c["id"] and (e.__setitem__("cat", target["id"]) or True))
        cats.remove(c)
        self.save_cats()
        if self.add_cat == c["id"]:
            self.add_cat = target["id"]
        self.redraw_keep()

    def redraw_keep(self):
        """Odświeża widok; na ekranie „+ Nowe” zachowuje wpisany tekst."""
        if self.view == "add":
            self.add_draft = (self.e_title.get(), self.e_time.get())
        self.draw()

    def set_task_cat(self, t, cid):
        t["cat"] = cid
        self.save()
        self.draw()

    def task_cat_menu(self, t, e=None):
        self.cat_menu(t.get("cat"), lambda cid: self.set_task_cat(t, cid), e)

    def set_hist(self, hid, **fields):
        rewrite_history(lambda e: e.get("id") == hid and (e.update(fields) or True))
        self.draw()

    def rename_hist(self, h):
        title = self.modal(simpledialog.askstring, "Zmień nazwę", "Nazwa zadania:", initialvalue=h["title"])
        title = (title or "").strip()
        if title and title != h["title"]:
            self.set_hist(h["id"], title=title)

    def hist_menu(self, h, e=None):
        self.cat_menu(h.get("cat"), lambda cid: self.set_hist(h["id"], cat=cid), e,
                      extra=[("Zmień nazwę…", lambda: self.rename_hist(h))])

    def _add_pick(self, cid):
        self.add_cat = cid
        self.redraw_keep()

    def _recover(self):
        """Blok otwarty po zamknięciu/wyłączeniu PC zamknij na ostatnim sygnale życia widgetu."""
        last = self.cfg.get("last_seen")
        for t in self.store["tasks"]:
            if not t.get("run_start"):
                continue
            start = dt.datetime.fromisoformat(t["run_start"])
            end = max(start, min(now(), dt.datetime.fromisoformat(last))) if last else now()
            t["elapsed"] += (end - start).total_seconds()
            t["run_start"] = None
            append_jsonl(TIMELOG, {"ts": end.isoformat(), "date": end.date().isoformat(), "id": t["id"],
                                   "title": t["title"], "action": "pause", "cat": t.get("cat"), "auto": True})
        self.save()

    # --- akcje ---
    def add_task(self, title, planned, start=False, cal=None, cat=None):
        t = {"id": uuid.uuid4().hex[:10], "title": title.strip(), "planned": int(planned), "original": int(planned),
             "ext": [], "elapsed": 0.0, "run_start": None, "created": now().isoformat(), "first_start": None,
             "cal": cal, "cat": self.cat(cat or self.last_cat())["id"]}
        self.store["tasks"].append(t)
        self.store["current"] = t["id"]
        self.save()
        if start:
            self.start(t)
        else:
            self.go("focus")
        return t

    def start(self, t):
        r = self.running()
        if r and r is not t:
            self.pause(r, redraw=False)
        if not t.get("run_start"):
            t["run_start"] = now().isoformat()
            t["first_start"] = t.get("first_start") or t["run_start"]
            self._tlog(t, "start")
        self.store["current"] = t["id"]
        self.save()
        self.go("focus")

    def pause(self, t, redraw=True):
        if not t.get("run_start"):
            return
        t["elapsed"] = self.elapsed(t)
        t["run_start"] = None
        self._tlog(t, "pause")
        self.save()
        if redraw:
            self.draw()

    def toggle(self, t):
        self.pause(t) if t.get("run_start") else self.start(t)

    def extend(self, t, sec):
        t["planned"] += int(sec)
        t["ext"].append({"ts": now().isoformat(), "add_s": int(sec), "at_elapsed_s": round(self.elapsed(t))})
        self.notified.discard(t["id"])
        self.save()
        self.draw()

    def extend_custom(self, t):
        txt = self.modal(simpledialog.askstring, "Przedłuż", "O ile przedłużyć? (np. 20, 1h, 1:30)")
        if txt is None:
            return
        sec = parse_duration(txt)
        if sec:
            self.extend(t, sec)
        else:
            self.toast("Nie rozumiem tego czasu")

    def task_options(self, t, e):
        font = (self.skin["font"], 11)
        menu = tk.Menu(self.root, tearoff=0, font=font)
        extend = tk.Menu(menu, tearoff=0, font=font)
        for label, sec in (("+ 5 min", 300), ("+ 15 min", 900), ("+ 30 min", 1800), ("+ 1 h", 3600)):
            extend.add_command(label=label, command=lambda s=sec: self.extend(t, s))
        extend.add_separator()
        extend.add_command(label="Inna wartość…", command=lambda: self.extend_custom(t))
        menu.add_cascade(label="Przedłuż", menu=extend)
        menu.add_command(label="Usuń zadanie…", command=lambda: self.delete(t))
        self.popup(menu, e)

    def modal(self, fn, *a, **kwargs):
        self.modal_depth += 1
        self.root.attributes("-topmost", False)
        try:
            return fn(*a, parent=self.root, **kwargs)
        finally:
            self.modal_depth -= 1
            self.root.attributes("-topmost", True)

    def rename(self, t):
        title = self.modal(simpledialog.askstring, "Zmień nazwę", "Nazwa zadania:", initialvalue=t["title"])
        if title is None:
            return
        title = title.strip()
        if not title:
            return self.toast("Podaj nazwę zadania", self.skin["warn"])
        try:
            self.set_task_title(t, title)
        except OSError:
            self.toast("Nie udało się zapisać nazwy zadania", self.skin["warn"])

    def set_task_title(self, t, title):
        original = t["title"]
        if title == original:
            return
        t["title"] = title
        try:
            self.save()
        except OSError:
            t["title"] = original
            raise
        self.redraw_keep()

    def complete(self, t):
        note = self.modal(simpledialog.askstring, "Zaliczone", f"{t['title']}\n\nNastępny krok (opcjonalnie):")
        if note is not None:
            self.finish_task(t, note)

    def finish_task(self, t, note):
        if t.get("run_start"):
            self.pause(t, redraw=False)
        el = t["elapsed"]
        orig = t["original"]
        entry = {
            "id": t["id"], "title": t["title"], "status": "done", "date": now().date().isoformat(),
            "created": t["created"], "first_start": t.get("first_start"), "finished": now().isoformat(),
            "planned_original_s": orig, "planned_final_s": t["planned"], "actual_s": round(el),
            "error_s": round(el - orig), "error_pct": round(100 * (el - orig) / orig, 1) if orig else None,
            "extensions": t["ext"], "note": note.strip(), "source": "calendar" if t.get("cal") else "manual",
            "cat": t.get("cat"),
        }
        append_jsonl(HISTORY, entry)
        self._tlog(t, "done", note=note.strip())
        append_daily_log(now().date(), t["title"], el, note.strip())
        if t.get("cal"):
            self._bg(lambda: self.cal.set_mark(t["cal"], "✓"), "Oznaczono w kalendarzu")
        self.store["tasks"].remove(t)
        self.store["current"] = None
        self.save()
        self.last_entry = entry
        self.go("summary")

    def delete(self, t):
        if not self.modal(messagebox.askyesno, "Usuń zadanie", f"Usunąć „{t['title']}”?"):
            return
        self.remove_task(t)

    def remove_task(self, t):
        if t.get("run_start"):
            self.pause(t, redraw=False)
        if t["elapsed"] > 0:
            append_jsonl(HISTORY, {"id": t["id"], "title": t["title"], "status": "deleted",
                                   "date": now().date().isoformat(), "actual_s": round(t["elapsed"]),
                                   "planned_original_s": t["original"], "planned_final_s": t["planned"],
                                   "extensions": t["ext"], "source": "calendar" if t.get("cal") else "manual",
                                   "cat": t.get("cat")})
            self._tlog(t, "cancel")
        self.store["tasks"].remove(t)
        self.store["current"] = None
        self.save()
        self.draw()

    def import_calendar(self):
        self.toast("Pobieram z kalendarza…")

        def run():
            try:
                self.q.put(("cal", self.cal.tasks(now().date())))
            except Exception as e:
                self.q.put(("error", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _bg(self, fn, ok):
        def run():
            try:
                fn()
                self.q.put(("ok", ok))
            except Exception as e:
                self.q.put(("error", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _drain(self):
        while not self.q.empty():
            kind, data = self.q.get()
            if kind == "cal":
                have = {t["cal"]["id"] for t in self.store["tasks"] if t.get("cal")}
                n = 0
                for c in data:
                    if c["id"] in have:
                        continue
                    title = c["base"][1:].strip() if c["base"][0] in PREFIXES else c["base"]
                    t = self.add_task(title, c["planned"] or 1500, cal={"id": c["id"], "base": c["base"]})
                    n += 1
                self.go("list")
                self.toast(f"Dodano {n} z kalendarza" if n else "Brak nowych zadań w kalendarzu")
            elif kind == "ok":
                self.toast(data)
            elif kind == "error":
                self.toast("Błąd: " + data[:60], self.skin["warn"])

    # --- pomocnicze rysowanie ---
    def go(self, view):
        if view == "add":
            self.add_cat, self.add_draft = self.last_cat(), None
        self.view = "mini" if self.mini else view
        self.scroll = 0
        self.draw()

    def draw(self):
        self.cv.delete("all")
        for w in self.cv.winfo_children():
            w.destroy()
        self.cv.config(cursor="")
        self.keep = []
        self.ring_key = None
        self.ring_item = self.time_t = self.sub_t = self.plan_t = None
        getattr(self, "view_" + self.view)()
        self.nav()

    def font(self, size, style=""):
        f = (self.skin["font"], -self.p(size))
        return f + (style,) if style else f

    def text(self, x, y, txt, size, fill=None, anchor="center", style="", cmd=None, tags=()):
        it = self.cv.create_text(self.p(x), self.p(y), text=txt, fill=fill or self.skin["ink"], anchor=anchor,
                                 font=self.font(size, style), tags=tags)
        if cmd:
            self.clickable(it, cmd)
        return it

    def fit(self, txt, size, maxw, style="", minsize=11):
        for s in range(int(size), minsize - 1, -1):
            f = tkfont.Font(family=self.skin["font"], size=-self.p(s), slant="italic" if style == "italic" else "roman",
                            weight="bold" if style == "bold" else "normal")
            if f.measure(txt) <= self.p(maxw):
                return txt, s
        while txt and f.measure(txt + "…") > self.p(maxw):
            txt = txt[:-1]
        return txt + "…", minsize

    def tw(self, txt, size, style=""):
        """Szerokość tekstu w jednostkach układu (przed skalowaniem)."""
        f = tkfont.Font(family=self.skin["font"], size=-self.p(size), slant="italic" if style == "italic" else "roman",
                        weight="bold" if style == "bold" else "normal")
        return f.measure(txt) / self.S

    def clickable(self, item, cmd):
        cv = self.cv
        cv.tag_bind(item, "<ButtonRelease-1>", lambda e: None if self.moved else cmd(e))
        cv.tag_bind(item, "<Enter>", lambda e: cv.config(cursor="hand2"))
        cv.tag_bind(item, "<Leave>", lambda e: cv.config(cursor=""))

    def put(self, pil, cx, cy, cmd=None):
        ph = ImageTk.PhotoImage(pil)
        self.keep.append(ph)
        it = self.cv.create_image(self.p(cx), self.p(cy), image=ph, anchor="center")
        if cmd:
            self.clickable(it, cmd)
        return it

    def button(self, kind, cx, cy, w, h, primary, cmd, bg=None):
        bg = bg or self.skin["card"]
        key = (kind, w, h, primary, bg)
        if key not in self.bcache:
            self.bcache[key] = btn_image(kind, self.p(w), self.p(h), primary, bg, self.skin_id)
        return self.put(self.bcache[key], cx, cy, cmd)

    def pill_text(self, label, cx, cy, w, h, primary, cmd):
        self.button(None, cx, cy, w, h, primary, cmd)
        it = self.text(cx, cy, label, 14, self.skin["ink"])
        self.clickable(it, cmd)

    def rrect(self, cx, cy, w, h, r, color, cmd=None):
        key = ("rr", w, h, r, color)
        if key not in self.bcache:
            self.bcache[key] = rr_image(self.p(w), self.p(h), self.p(r), color, self.skin["card"])
        return self.put(self.bcache[key], cx, cy, cmd)

    def toast(self, msg, color=None):
        self.cv.delete("toast")
        self.text(180, 425, msg, 12, color or self.skin["dim"], style="italic", tags=("toast",))
        self.root.after(3500, lambda: self.cv.delete("toast"))

    def nav(self):
        if self.view == "mini":
            return
        self.cv.create_line(self.p(14), self.p(447), self.p(346), self.p(447), fill=self.skin["border"])
        items = [("focus", "Zegar"), ("list", "Zadania"),
                 ("history", "Historia"), ("month", "Wykres"), ("add", "+ Nowe")]
        for (view, label), x in zip(items, (32, 100, 172, 240, 316)):
            active = self.view == view or (view == "focus" and self.view == "summary")
            cmd = lambda e, v=view: self.go(v)
            hit = self.cv.create_rectangle(self.p(x - 30), self.p(449), self.p(x + 30), self.p(479),
                                           outline="", fill=self.skin["card"])
            self.clickable(hit, cmd)
            self.text(x, 464, label, 12, self.skin["ink"] if active else self.skin["muted"],
                      style="bold" if active else "", cmd=cmd)
        self.text(342, 20, "✕", 14, self.skin["muted"], cmd=lambda e: self.quit())
        self.text(312, 20, "—", 14, self.skin["muted"], cmd=lambda e: self.set_mini(True))
        self.text(26, 20, "☰", 17, self.skin["dim"], cmd=self.settings_menu)

    def settings_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0, font=("Cambria", 11))
        self.skin_choice = tk.StringVar(self.root, value=self.skin_id)
        for skin_id, skin in SKINS.items():
            menu.add_radiobutton(label=skin["name"], value=skin_id, variable=self.skin_choice,
                                 command=lambda name=skin_id: self.set_skin(name))
        menu.add_separator()
        menu.add_command(label="Otwórz w przeglądarce", command=lambda: self.open_web(False))
        menu.add_command(label="Połącz telefon przez Wi-Fi…", command=lambda: self.open_web(True))
        if self.web:
            menu.add_command(label="Wyłącz dostęp z przeglądarki", command=self.close_web)
        if self.cloud:
            menu.add_separator()
            menu.add_command(label=self.cloud.status(), state="disabled")
            if self.cloud.connected:
                menu.add_command(label="Wyślij do chmury teraz", command=lambda: self.cloud.poke(force=True))
                menu.add_command(label="Odłącz od chmury", command=self.cloud_off)
            else:
                menu.add_command(label="Połącz z chmurą (konto apki)…", command=self.cloud_on)
        self.popup(menu, event)

    def cloud_on(self):
        email = self.modal(simpledialog.askstring, "Chmura", "E-mail konta w apce Life:")
        if not email or not email.strip():
            return
        password = self.modal(simpledialog.askstring, "Chmura", "Hasło (nie jest zapisywane):", show="*")
        if not password:
            return
        self._bg(lambda: self.cloud.sign_in(email, password), "Połączono z chmurą")

    def cloud_off(self):
        self.cloud.sign_out()
        self.toast("Odłączono od chmury")

    def set_skin(self, name):
        self.cfg["skin"] = name
        save_json(CONFIG, self.cfg)
        self.skin_id, self.skin = name, SKINS[name]
        self.root.config(bg=self.skin["card"])
        self.cv.config(bg=self.skin["card"])
        self.bcache.clear()
        self._round_corners()
        self.redraw_keep()

    def open_web(self, lan):
        try:
            host = WidgetWeb.local_ip() if lan else "127.0.0.1"
            if self.web and lan and not self.web.lan:
                self.close_web()
            if not self.web:
                self.web = WidgetWeb(self.root, self.web_action, lambda: self.modal_depth > 0, lan=lan)
        except OSError as error:
            return self.modal(messagebox.showerror, "Life w przeglądarce", str(error))
        link = self.web.link(host)
        if lan:
            self.root.clipboard_clear()
            self.root.clipboard_append(link)
            messagebox.showinfo("Life na telefonie",
                                f"Na tej samej zaufanej sieci Wi-Fi otwórz:\nhttp://{host}:{self.web.port}/\n\n"
                                f"Kod dostępu:\n{self.web.token}\n\n"
                                "Pełny link skopiowano do schowka. Widget Windows musi pozostać otwarty. "
                                "Jeśli Zapora Windows zapyta o dostęp, wybierz sieć prywatną.",
                                parent=self.root)
        else:
            webbrowser.open(link)

    def close_web(self):
        if self.web:
            self.web.close()
            self.web = None

    def web_action(self, request):
        if request is not None:
            action = request.get("action")
            title = request.get("title")
            if action == "skin":
                name = request.get("skin")
                if not isinstance(name, str) or name not in SKINS:
                    raise WebError("Nieznany wygląd.")
                self.set_skin(name)
            elif action == "add":
                if not isinstance(title, str) or not title.strip() or len(title) > 200:
                    raise WebError("Podaj nazwę od 1 do 200 znaków.")
                category = self.cat(request.get("cat"))["id"]
                self.add_task(title, self.web_duration(request), request.get("start") is True, cat=category)
            else:
                task = next((t for t in self.store["tasks"] if t["id"] == request.get("id")), None)
                if not task:
                    raise WebError("Zadanie zostało już zmienione lub usunięte.", 404)
                if action == "select":
                    self._select(task)
                elif action == "start":
                    if not task.get("run_start"):
                        self.start(task)
                elif action == "pause":
                    self.pause(task)
                elif action == "rename":
                    if not isinstance(title, str) or not title.strip() or len(title) > 200:
                        raise WebError("Podaj nazwę od 1 do 200 znaków.")
                    self.set_task_title(task, title.strip())
                elif action == "extend":
                    self.extend(task, self.web_duration(request))
                elif action == "category":
                    self.set_task_cat(task, self.cat(request.get("cat"))["id"])
                elif action == "complete":
                    note = request.get("note", "")
                    if not isinstance(note, str) or len(note) > 1000:
                        raise WebError("Notatka może mieć do 1000 znaków.")
                    self.finish_task(task, note)
                elif action == "delete":
                    self.remove_task(task)
                else:
                    raise WebError("Nieznana akcja.")
        current = self.cur()
        today = now().date()
        return {
            "skin": self.skin_id, "current": current["id"] if current else None,
            "tasks": [{"id": t["id"], "title": t["title"], "planned": t["planned"], "original": t["original"],
                       "elapsed": int(self.elapsed(t)), "running": bool(t.get("run_start")),
                       "cat": self.cat(t.get("cat"))["id"]} for t in self.store["tasks"]],
            "categories": self.cats["categories"], "last_category": self.last_cat(),
            "goal": round(float(self.cfg.get("goal_h", 4)) * 3600),
            "days": {d.isoformat(): values for d, values in self.work_by_day().items()},
            "today": today.isoformat(),
            "history": [{k: h.get(k) for k in ("title", "date", "planned_original_s", "actual_s", "error_s", "cat")}
                        for h in reversed(read_history()) if h.get("status") == "done"],
        }

    @staticmethod
    def web_duration(request):
        value = request.get("duration")
        duration = parse_duration(value) if isinstance(value, str) and len(value) <= 24 else None
        if not duration or duration > 999 * 3600:
            raise WebError("Podaj czas, np. 45, 1h30 albo 1:30.")
        return duration

    # --- tryb mini ---
    def set_mini(self, flag):
        self.mini = flag
        self.cfg["mini"] = flag
        save_json(CONFIG, self.cfg)
        if flag:
            self.prev_view = self.view if self.view != "mini" else "focus"
            self.view = "mini"
            self.w, self.h = MINI_W, MINI_H
        else:
            self.view = self.prev_view
            self.w, self.h = W, H
        self._resize()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        vx, vy, vw, vh = (self.root.winfo_vrootx(), self.root.winfo_vrooty(),
                          self.root.winfo_vrootwidth(), self.root.winfo_vrootheight())
        self.root.geometry(f"+{min(max(x, vx), vx + vw - self.p(self.w))}+{min(max(y, vy), vy + vh - self.p(self.h))}")
        self.draw()

    def view_mini(self):
        t = self.running() or self.cur()
        expand = lambda e: self.set_mini(False)
        self.cv.create_rectangle(0, 0, self.p(MINI_W), self.p(MINI_H), fill=self.skin["card"], outline="", tags=("hit",))
        self.clickable("hit", expand)
        if t:
            el, plan = self.elapsed(t), t["planned"]
            over = el >= plan
            top, bot = (WARN_TOP, WARN_BOT) if over else self.ring_colors(t)
            self.put(ring_image(self.p(38), self.p(4), 1 if over else el / plan, top, bot, bg=self.skin["card"], track_color=self.skin["track"]), 30, 30, expand)
            title, size = self.fit(t["title"], 11, 100, "italic", 9)
            self.text(58, 22, fmt_hms(el), 19, self.skin["warn"] if over else self.skin["ink"], "w", cmd=expand)
            self.text(58, 44, title, size, self.skin["dim"], "w", "italic", cmd=expand)
            run = bool(t.get("run_start"))
            self.button("pause" if run else "play", 182, 30, 34, 34, run, lambda e: self.toggle(t))
        else:
            self.text(105, 22, "Brak zadania", 15, self.skin["dim"], cmd=expand)
            self.text(105, 44, "kliknij, aby rozwinąć", 10, self.skin["muted"], style="italic", cmd=expand)

    # --- widok: zegar ---
    def view_focus(self):
        t = self.cur()
        camp = self.skin_id == "campfire"
        self.text(180, 23, "Life", 22, self.skin["dim"])
        if camp:
            path = HERE / "web" / "campfire.png"
            if path.exists():
                if "campfire" not in self.bcache:
                    with Image.open(path) as art:
                        scene = ImageOps.fit(art, (self.p(328), self.p(172)), Image.NEAREST, centering=(.5, .65))
                        fade = Image.linear_gradient("L").resize(scene.size).point(lambda x: max(0, (x - 192) * 4))
                        self.bcache["campfire"] = Image.composite(Image.new("RGB", scene.size, self.skin["card"]),
                                                                scene.convert("RGB"), fade)
                self.put(self.bcache["campfire"], 180, 138)
            title_y, time_y, status_y, plan_y, cat_y = 236, 276, 305, 326, 216
        else:
            title_y, time_y, status_y, plan_y, cat_y = 63, 200, 229, 253, 160
            self.ring_item = self.put(self.focus_ring(0, P_TOP, P_BOT), 180, 200)

        title, size = self.fit(t["title"] if t else "Brak zadania", 21, 308, "italic")
        self.text(180, title_y, title, size, self.skin["dim"], style="italic",
                  cmd=(lambda e: self.rename(t)) if t else None)
        self.time_t = self.text(180, time_y, "00:00:00", 36 if camp else 43)
        self.sub_t = self.text(180, status_y, "", 12, self.skin["dim"], style="italic")
        self.plan_t = self.text(180, plan_y, "", 12, self.skin["dim"])
        if t:
            self.button("more", 270, 20, 30, 26, False, lambda e: self.task_options(t, e))
            c = self.cat(t.get("cat"))
            name, size = self.fit(c["name"], 11, 130, "italic", 10)
            tw = self.tw(name, size, "italic")
            x0 = 180 - (16 + tw) / 2
            pick = lambda e: self.task_cat_menu(t, e)
            self.put(dot_image(self.p(9), c["color"]), x0 + 5, cat_y, pick)
            self.text(x0 + 16, cat_y, name, size, self.skin["dim"], "w", "italic", cmd=pick)
            running = bool(t.get("run_start"))
            self.button("list", 105, 373, 44, 42, False, lambda e: self.go("list"))
            self.button("pause" if running else "play", 180, 373, 64, 54, True, lambda e: self.toggle(t))
            self.button("check", 255, 373, 44, 42, False, lambda e: self.complete(t))
            if camp:
                self.cv.create_line(self.p(82), self.p(314), self.p(278), self.p(314),
                                    width=self.p(3), fill=self.skin["track"], capstyle="round")
                self.task_progress_fill = self.cv.create_line(self.p(82), self.p(314), self.p(82), self.p(314),
                                                             width=self.p(3), capstyle="round")
        else:
            self.pill_text("Nowe zadanie", 180, 373, 170, 40, True, lambda e: self.go("add"))
        self.day_bar()
        self.update_focus()

    def focus_ring(self, progress, top, bot):
        return ring_image(self.p(264), self.p(8), progress, top, bot,
                          bg=self.skin["card"], track_color=self.skin["track"], soft=True)

    def day_bar(self):
        """Pasek łącznego czasu pracy dziś (wszystkie zadania), żywo odświeżany."""
        self.day_base = self.work_by_day(live=False).get(now().date(), {})
        self.day_txt = self.text(30, 422, "", 12, self.skin["dim"], "w", "italic")
        self.day_goal = self.text(330, 422, "", 11, self.skin["muted"], "e", "italic")
        self.cv.create_image(self.p(30), self.p(434), image=self._keep(rr_image(self.p(300), self.p(8), self.p(4), self.skin["track"], self.skin["card"])), anchor="w")
        self.day_item = self.cv.create_image(self.p(30), self.p(434), anchor="w")
        self.day_w = None
        self.update_day()

    def _keep(self, pil):
        ph = ImageTk.PhotoImage(pil)
        self.keep.append(ph)
        return ph

    def update_day(self):
        if self.view != "focus" or getattr(self, "day_item", None) is None:
            return
        r = self.running()
        per = dict(self.day_base)
        if r:
            live = (now() - dt.datetime.fromisoformat(r["run_start"])).total_seconds()
            if live <= MAX_BLOCK:
                k = self.cat(r.get("cat"))["id"]
                per[k] = per.get(k, 0) + live
        tot = sum(per.values())
        goal = float(self.cfg.get("goal_h", 4)) * 3600
        segs = self.segs(per)
        w = self.p(300 * min(1, tot / goal))
        key = (w if tot >= 60 else 0, tuple((round(f * w), c) for f, c in segs), self.S)
        label = "PD dziś" if self.skin_id == "campfire" else "Dziś"
        self.cv.itemconfig(self.day_txt, text=f"{label} {fmt_hm(tot)} h")
        self.cv.itemconfig(self.day_goal, text="cel osiągnięty ✓" if tot >= goal else f"cel {self.cfg.get('goal_h', 4):g} h")
        if key != self.day_w:
            self.day_w = key
            if tot >= 60:
                self.day_ph = ImageTk.PhotoImage(bar_image(max(w, self.p(8)), self.p(8), self.p(4), segs,
                                                           bg=self.skin["track"], vertical=False))
                self.cv.itemconfig(self.day_item, image=self.day_ph)
            else:
                self.cv.itemconfig(self.day_item, image="")

    def update_focus(self):
        t = self.cur()
        if self.view != "focus" or not t or self.time_t is None:
            return
        el, plan = self.elapsed(t), t["planned"]
        over = el >= plan
        p = 1 if over else el / plan
        running = bool(t.get("run_start"))
        top, bot = (WARN_TOP, WARN_BOT) if over else self.ring_colors(t)
        key = (int(p * 720), top, bot, self.S)
        if key != self.ring_key:
            self.ring_key = key
            if self.ring_item is not None:
                self.ring_photo = ImageTk.PhotoImage(self.focus_ring(p, top, bot))
                self.cv.itemconfig(self.ring_item, image=self.ring_photo)
            else:
                self.cv.coords(self.task_progress_fill, self.p(82), self.p(314), self.p(82 + 196 * p), self.p(314))
                self.cv.itemconfig(self.task_progress_fill, fill=top if p > 0 else self.skin["track"])
        if over:
            sub = f"Ponad plan +{fmt_hms(el - plan)[3:] if el - plan < 3600 else fmt_hms(el - plan)}"
            sub = sub if running else "Pauza · " + sub.lower()
            col = self.skin["warn"]
        else:
            sub = "Skupienie" if running else ("Pauza" if el > 0 else "Gotowe do startu")
            col = self.skin["dim"]
        self.cv.itemconfig(self.sub_t, text=sub, fill=col)
        self.cv.itemconfig(self.time_t, text=fmt_hms(el), fill=None)
        ext = plan - t["original"]
        self.cv.itemconfig(self.plan_t, text=f"z {fmt_hm(plan)}" + (f"  (+{fmt_hm(ext)})" if ext > 0 else ""))

    # --- widok: lista ---
    def view_list(self):
        self.text(180, 42, "Zadania", 22)
        tasks = self.store["tasks"]
        if not tasks:
            self.text(180, 210, "Pusto. Dodaj pierwsze zadanie.", 14, self.skin["dim"], style="italic")
            self.pill_text("Nowe zadanie", 180, 280, 170, 40, True, lambda e: self.go("add"))
            return
        per = 5
        self.scroll = min(self.scroll, max(0, len(tasks) - per))
        cur = self.cur()
        for i, t in enumerate(tasks[self.scroll:self.scroll + per]):
            cy = 108 + i * 68
            sel = cur is t
            self.rrect(180, cy, 320, 60, 14, self.skin["row_sel"] if sel else self.skin["row"], lambda e, t=t: self._select(t))
            self.put(dot_image(self.p(10), self.cat(t.get("cat"))["color"], self.skin["row_sel"] if sel else self.skin["row"]), 39, cy - 11,
                     lambda e, t=t: self.task_cat_menu(t, e))
            title, size = self.fit(t["title"], 15, 200)
            self.text(50, cy - 11, title, size, anchor="w", cmd=lambda e, t=t: self._select(t))
            el = self.elapsed(t)
            run = bool(t.get("run_start"))
            line = f"{fmt_hm(el)} z {fmt_hm(t['planned'])}" + ("  · trwa" if run else "")
            self.text(50, cy + 12, line, 12, self.skin["warn"] if el >= t["planned"] else self.skin["dim"], anchor="w", style="italic")
            self.text(231, cy + 12, "zmień nazwę", 11, self.skin["dim"], style="italic", cmd=lambda e, t=t: self.rename(t))
            self.button("pause" if run else "play", 292, cy, 34, 34, run, lambda e, t=t: self.toggle(t),
                        bg=self.skin["row_sel"] if sel else self.skin["row"])
            self.text(326, cy, "✕", 11, self.skin["muted"], cmd=lambda e, t=t: self.delete(t))
        if len(tasks) > per:
            self.text(180, 438, f"{self.scroll + 1}–{min(len(tasks), self.scroll + per)} z {len(tasks)} · przewiń kółkiem", 11, self.skin["muted"], style="italic")

    def _select(self, t):
        self.store["current"] = t["id"]
        self.save()
        self.go("focus")

    # --- widok: nowe zadanie ---
    def view_add(self):
        self.text(180, 42, "Nowe zadanie", 22)
        kw = dict(font=(self.skin["font"], -self.p(15)), bg=self.skin["row"], fg=self.skin["ink"], relief="flat", insertbackground=self.skin["ink"],
                  highlightthickness=1, highlightbackground=self.skin["border"], highlightcolor=P_BOT)
        self.text(30, 80, "Nazwa", 12, self.skin["dim"], "w", "italic")
        self.e_title = tk.Entry(self.cv, **kw)
        self.cv.create_window(self.p(30), self.p(92), window=self.e_title, anchor="nw", width=self.p(300), height=self.p(34))
        self.text(30, 142, "Planowany czas", 12, self.skin["dim"], "w", "italic")
        self.e_time = tk.Entry(self.cv, **kw)
        self.cv.create_window(self.p(30), self.p(154), window=self.e_time, anchor="nw", width=self.p(110), height=self.p(34))
        self.text(152, 171, "np. 45 · 1h30 · 1:30", 11, self.skin["muted"], "w", "italic")
        for i, (label, mins) in enumerate((("15m", 15), ("25m", 25), ("45m", 45), ("1h", 60), ("1,5h", 90), ("2h", 120))):
            cx = 45 + i * 54

            def setv(e, m=mins):
                self.e_time.delete(0, "end")
                self.e_time.insert(0, str(m))
            self.rrect(cx, 216, 46, 28, 14, self.skin["row"], setv)
            self.text(cx, 216, label, 12, self.skin["dim"], cmd=setv)
        self.text(30, 250, "Kategoria", 12, self.skin["dim"], "w", "italic")
        self.text(330, 250, "prawy klik: edycja", 10, self.skin["muted"], "e", "italic")
        self.cat_chips(276)
        self.pill_text("Dodaj", 105, 358, 100, 40, False, lambda e: self.submit(False))
        self.pill_text("Dodaj i start", 235, 358, 150, 40, True, lambda e: self.submit(True))
        self.text(180, 400, "Dodaj dzisiejsze z kalendarza", 12, self.skin["dim"], style="italic", cmd=lambda e: self.import_calendar())
        for en in (self.e_title, self.e_time):
            en.bind("<Return>", lambda e: self.submit(True))
        if self.add_draft:
            self.e_title.insert(0, self.add_draft[0])
            self.e_time.insert(0, self.add_draft[1])
            self.add_draft = None
        self.e_title.focus_force()
        self.e_title.icursor("end")

    def cat_chips(self, y0):
        """Kategorie jako pigułki (do 2 rzędów); nadmiar w menu „…”, na końcu „+” dodaje nową."""
        cats = self.cats["categories"]
        sel = self.cat(self.add_cat)["id"]

        def place(items):
            out, x, row = [], 30, 0
            for label, c in items:
                w = self.tw(label, 12) + (30 if c else 22)
                if x + w > 330 and x > 30:
                    row, x = row + 1, 30
                if row > 1:
                    return None
                out.append((label, c, x, row, w))
                x += w + 8
            return out
        chips = [(c["name"], c) for c in cats]
        n = len(chips)
        res = place(chips + [("+", None)])
        while res is None and n > 1:
            n -= 1
            res = place(chips[:n] + [("…", None)])
        for label, c, x, row, w in res or []:
            cy = y0 + row * 34
            if c is None:
                cmd = ((lambda e: self.new_category(self._add_pick)) if label == "+"
                       else (lambda e: self.cat_menu(sel, self._add_pick, e)))
                self.rrect(x + w / 2, cy, w, 28, 14, self.skin["row"], cmd)
                self.text(x + w / 2, cy, label, 13, self.skin["dim"], cmd=cmd)
                continue
            on = c["id"] == sel
            bg = self.skin["row_sel"] if on else self.skin["row"]
            cmd = lambda e, cid=c["id"]: self._add_pick(cid)
            items = [self.rrect(x + w / 2, cy, w, 28, 14, bg, cmd),
                     self.put(dot_image(self.p(10), c["color"], bg), x + 14, cy, cmd),
                     self.text(x + 24, cy, label, 12, self.skin["ink"] if on else self.skin["dim"], "w", "italic" if on else "", cmd=cmd)]
            for it in items:
                self.cv.tag_bind(it, "<ButtonRelease-3>", lambda e, c=c: self.popup(self.edit_cat_menu(self.root, c), e))

    def submit(self, start):
        title, sec = self.e_title.get().strip(), parse_duration(self.e_time.get())
        if not title:
            return self.toast("Podaj nazwę zadania", self.skin["warn"])
        if not sec:
            return self.toast("Podaj czas, np. 45 albo 1h30", self.skin["warn"])
        self.cfg["last_cat"] = self.cat(self.add_cat)["id"]
        save_json(CONFIG, self.cfg)
        self.add_task(title, sec, start=start, cat=self.add_cat)

    # --- widok: podsumowanie po zaliczeniu ---
    def view_summary(self):
        e = self.last_entry
        if not e:
            return self.go("focus")
        title, size = self.fit(e["title"], 20, 300)
        self.text(180, 60, "Zaliczone ✓", 24)
        self.text(180, 96, title, size, self.skin["dim"], style="italic")
        err = e["error_s"]
        col = self.skin["warn"] if err > 0 else self.skin["good"]
        rows = [("Plan", fmt_dur(e["planned_original_s"]), self.skin["ink"]), ("Faktycznie", fmt_dur(e["actual_s"]), self.skin["ink"])]
        if e["extensions"]:
            add = sum(x["add_s"] for x in e["extensions"])
            rows.append((f"Przedłużono {len(e['extensions'])}×", "+" + fmt_dur(add), self.skin["dim"]))
        pct = f" ({e['error_pct']:+.0f}%)" if e["error_pct"] is not None else ""
        rows.append(("Błąd planowania", fmt_signed(err) + pct, col))
        for i, (a, b, c) in enumerate(rows):
            y = 160 + i * 44
            self.rrect(180, y, 300, 38, 12, self.skin["row"])
            self.text(40, y, a, 14, self.skin["dim"], "w", "italic")
            self.text(320, y, b, 15, c, "e")
        self.pill_text("Dalej", 180, 375, 140, 40, True, lambda ev: self.go("focus"))

    # --- widok: wykres (tydzień / 4 tygodnie) ---
    def _bar(self, cx, base, w, h, segs, r=8):
        """Słupek skumulowany: odcinki kategorii od dołu."""
        h = max(3, h)
        self.put(bar_image(self.p(w), self.p(h), self.p(min(r, h / 2)), segs, self.skin["card"]), cx, base - h / 2)

    def legend(self, y, days, data, today):
        """Suma okresu w podziale na kategorie: ● Architektura 12:30 h  ● Aplikacje 1:10 h."""
        sums = {}
        for d in days:
            if d <= today:
                for k, s in data.get(d, {}).items():
                    sums[k] = sums.get(k, 0) + s
        items = sorted(((self.cat(k), s) for k, s in sums.items() if s >= 60), key=lambda x: -x[1])
        while items:
            for size in (11, 10, 9):
                labels = [f"{c['name']} {fmt_hm(s)} h" for c, s in items]
                widths = [13 + self.tw(lb, size) for lb in labels]
                total = sum(widths) + 14 * (len(widths) - 1)
                if total <= 320:
                    break
            if total <= 320:
                break
            items.pop()
        if not items:
            return
        x = 180 - total / 2
        for (c, s), lb, w in zip(items, labels, widths):
            self.put(dot_image(self.p(9), c["color"], self.skin["card"]), x + 4.5, y)
            self.text(x + 13, y, lb, size, self.skin["dim"], "w")
            x += w + 14

    def view_month(self):
        today = now().date()
        week = self.chart_mode == "week"
        mon0 = today - dt.timedelta(days=today.weekday())
        data = self.work_by_day()
        goal = float(self.cfg.get("goal_h", 4)) * 3600
        if week:
            start = mon0 + dt.timedelta(days=7 * self.chart_off)
            weeks = [start]
            end = start + dt.timedelta(days=6)
            title = f"{start.day}.{start.month} – {end.day}.{end.month}"
        else:
            last = mon0 + dt.timedelta(days=28 * self.chart_off)
            weeks = [last - dt.timedelta(days=7 * (3 - i)) for i in range(4)]
            end = weeks[-1] + dt.timedelta(days=6)
            title = f"{weeks[0].day}.{weeks[0].month} – {end.day}.{end.month}"
        self.text(180, 42, title, 22)
        self.text(34, 44, "‹", 24, self.skin["dim"], cmd=lambda e: self._month(-1))
        self.text(300, 44, "›", 24, self.skin["dim"], cmd=lambda e: self._month(1))
        for label, mode, cx in (("Tydzień", "week", 130), ("4 tygodnie", "month", 230)):
            on = self.chart_mode == mode
            self.rrect(cx, 82, 92, 28, 14, "#8c77ab" if on else self.skin["row"], lambda e, m=mode: self._mode(m))
            self.text(cx, 82, label, 12, "#ffffff" if on else self.skin["dim"], cmd=lambda e, m=mode: self._mode(m))

        def val(d):
            return sum(data.get(d, {}).values()) if d <= today else None

        def segs(d, v):
            return self.segs(data.get(d, {})) if v >= 60 else [(1, EMPTY_DAY)]
        days = [w + dt.timedelta(days=i) for w in weeks for i in range(7)]
        top = max([goal] + [val(d) or 0 for d in days])
        tot = [val(d) or 0 for d in days]
        names = ("Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd")
        if week:
            base, hmax = 330, 190
            gy = base - hmax * goal / top
            self.cv.create_line(self.p(26), self.p(gy), self.p(334), self.p(gy), dash=(3, 4), fill=self.skin["border"])
            self.text(334, gy - 8, f"cel {self.cfg.get('goal_h', 4):g} h", 10, self.skin["muted"], "e", "italic")
            for i, d in enumerate(days):
                cx, v = 50 + i * 44, val(d)
                if v is not None:
                    h = hmax * v / top if v >= 60 else 0
                    self._bar(cx, base, 30, h, segs(d, v))
                    if v >= 60:
                        self.text(cx, base - h - 10, fmt_hm(v), 11, self.skin["ink"])
                self.text(cx, base + 18, names[i], 12, self.skin["ink"] if d == today else self.skin["muted"], style="bold" if d == today else "")
                self.text(cx, base + 34, str(d.day), 10, self.skin["muted"])
        else:
            for i, n in enumerate(names):
                self.text(100 + i * 26, 112, n[0], 10, self.skin["muted"])
            for r_, w in enumerate(weeks):
                y0 = 124 + r_ * 66
                base, hmax = y0 + 48, 44
                wt = 0
                for i in range(7):
                    d, cx = w + dt.timedelta(days=i), 100 + i * 26
                    v = val(d)
                    if v is None:
                        continue
                    wt += v
                    self._bar(cx, base, 18, hmax * v / top if v >= 60 else 0, segs(d, v), 6)
                cur = w == mon0
                self.text(22, y0 + 20, f"{w.day}.{w.month}", 12, self.skin["ink"] if cur else self.skin["dim"], "w", "bold" if cur else "")
                self.text(22, y0 + 36, "ten tydzień" if cur else "", 10, self.skin["muted"], "w", "italic")
                self.text(336, y0 + 28, fmt_hm(wt) + " h", 13, self.skin["ink"], "e")
        self.legend(386, days, data, today)
        shown = [x for x, d in zip(tot, days) if d <= today]
        worked = [x for x in shown if x >= 60]
        n = len(days)
        self.text(180, 408, f"Razem {fmt_hm(sum(shown))} h · aktywne dni {len(worked)}/{n}", 13, self.skin["ink"])
        self.text(180, 430, (f"średnio {fmt_hm(sum(worked) / len(worked))} h w dzień pracy · najlepszy {fmt_hm(max(worked))} h"
                             if worked else "brak zapisanej pracy w tym okresie"), 12, self.skin["dim"], style="italic")

    def _month(self, step):
        self.chart_off += step
        self.draw()

    def _mode(self, mode):
        self.chart_mode, self.chart_off = mode, 0
        self.draw()

    # --- widok: historia ---
    def view_history(self):
        self.text(180, 42, "Historia", 22)
        done = [h for h in read_history() if h.get("status") == "done"]
        if not done:
            self.text(180, 210, "Brak zaliczonych zadań.", 14, self.skin["dim"], style="italic")
            return
        plan = sum(h["planned_original_s"] for h in done)
        act = sum(h["actual_s"] for h in done)
        self.text(180, 80, f"{len(done)} zadań · plan {fmt_hm(plan)} h · faktycznie {fmt_hm(act)} h", 12, self.skin["dim"])
        if len(done) >= 3 and plan:
            self.text(180, 102, f"zwykle trwa ×{act / plan:.2f} planu", 13, self.skin["warn"] if act > plan else self.skin["good"], style="italic")
        else:
            self.text(180, 102, "średni błąd pokaże się od 3 zadań", 12, self.skin["muted"], style="italic")
        done.reverse()
        per = 5
        self.scroll = min(self.scroll, max(0, len(done) - per))
        for i, h in enumerate(done[self.scroll:self.scroll + per]):
            cy = 148 + i * 54
            menu = lambda e, h=h: self.hist_menu(h, e)
            self.rrect(180, cy, 320, 48, 12, self.skin["row"], menu)
            self.put(dot_image(self.p(10), self.cat(h.get("cat"))["color"], self.skin["row"]), 36, cy - 9, menu)
            title, size = self.fit(h["title"], 14, 178)
            self.text(47, cy - 9, title, size, anchor="w", cmd=menu)
            d = dt.date.fromisoformat(h["date"])
            self.text(47, cy + 11, f"{d.day}.{d.month:02d} · {fmt_hm(h['planned_original_s'])} → {fmt_hm(h['actual_s'])}"
                      + (f" · przedł. {len(h['extensions'])}×" if h.get("extensions") else ""), 11, self.skin["dim"], "w", "italic")
            err = h["error_s"]
            self.text(328, cy, fmt_signed(err), 14, self.skin["warn"] if err > 0 else self.skin["good"], "e")
        self.text(180, 424, "eksport CSV", 11, self.skin["muted"], style="italic", cmd=lambda e: self.export_csv())

    def export_csv(self):
        rows = [dict(h, category=self.cat(h.get("cat"))["name"]) for h in read_history()]
        cols = ["date", "title", "category", "status", "planned_original_s", "planned_final_s", "actual_s", "error_s",
                "error_pct", "source", "first_start", "finished", "note"]
        CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
        with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore", delimiter=";")
            w.writeheader()
            w.writerows(rows)
        try:
            os.startfile(CSV_OUT)
        except Exception:
            self.toast("Zapisano data/historia.csv")

    # --- pętla ---
    def tick(self):
        self._drain()
        r = self.running()
        if r:
            if self.elapsed(r) >= r["planned"] and r["id"] not in self.notified:
                self.notified.add(r["id"])
                try:
                    import winsound
                    winsound.MessageBeep()
                except Exception:
                    pass
            if self.view == "focus":
                self.update_day()
                if r is self.cur():
                    self.update_focus()
            elif self.view == "mini":
                self.draw()
            elif self.view == "list" or (self.view == "month" and int(now().timestamp()) % 30 == 0):
                self.draw()
        if self.cloud:
            self.cloud.poke()
        if now().timestamp() - self.last_seen_saved >= 30:
            self.last_seen_saved = now().timestamp()
            self.cfg["last_seen"] = now().isoformat()
            save_json(CONFIG, self.cfg)
        self.root.after(1000, self.tick)

    def quit(self):
        self.close_web()
        r = self.running()
        if r:
            self.pause(r, redraw=False)
        self.cfg["last_seen"] = now().isoformat()
        save_json(CONFIG, self.cfg)
        self.root.destroy()


if __name__ == "__main__":
    if "--make-icon" in sys.argv:
        make_icon(HERE / "life-widget.ico")
        sys.exit()
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47614 if DEMO else 47613))
    except OSError:
        sys.exit()          # widget już działa
    App().root.mainloop()
