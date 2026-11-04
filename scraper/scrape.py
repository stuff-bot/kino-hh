#!/usr/bin/env python3
"""Sammelt das Kinoprogramm der Hamburger Lieblingskinos und schreibt site/data.json.

Nur Standardbibliothek, damit es überall (Mac, GitHub Actions) ohne Installation läuft.
Jedes Kino wird einzeln abgefragt; fällt eins aus, bleiben die anderen erhalten und die
App zeigt einen Hinweis.
"""
import html
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
UA = "Mozilla/5.0 (compatible; KinoHH/1.0; private Programmübersicht)"
OUT = Path(__file__).resolve().parent.parent / "site" / "data.json"
DAYS_AHEAD = 10
# python.org-Python auf dem Mac bringt keine CA-Zertifikate mit → System-Bundle nutzen
SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem") if os.path.exists("/etc/ssl/cert.pem") else None

CINEMAS = [
    {"id": "cxw", "web": "https://www.cinemaxx.de/kinoprogramm/hamburg-wandsbek/jetzt-im-kino", "name": "CinemaxX Wandsbek", "short": "CX Wandsbek", "color": "#e4003a"},
    {"id": "cxd", "web": "https://www.cinemaxx.de/kinoprogramm/hamburg-dammtor/jetzt-im-kino", "name": "CinemaxX Dammtor", "short": "CX Dammtor", "color": "#ff6a3d"},
    {"id": "kp", "web": "https://www.kinopolis.de/hh/programm", "name": "Kinopolis HafenCity", "short": "Kinopolis", "color": "#2f80ed"},
    {"id": "astor", "web": "https://hamburg.premiumkino.de/", "name": "Astor Film Lounge", "short": "Astor", "color": "#c9a45c"},
    {"id": "uci", "web": "https://www.uci-kinowelt.de/kinoprogramm/hh-wandsbek-smart-city/54", "name": "UCI Wandsbek", "short": "UCI", "color": "#9b51e0"},
]


def fetch(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de-DE,de"})
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def show(cinema, title, start, *, tags=(), screen=None, url=None, poster=None, minutes=None, fsk=None, genre=None,
         desc=None, page=None):
    return {
        "cinema": cinema,
        "title": title.strip(),
        "start": start,  # "YYYY-MM-DDTHH:MM", Ortszeit Hamburg
        "tags": sorted(set(t for t in tags if t)),
        "screen": screen,
        "url": url,
        "poster": poster,
        "minutes": minutes,
        "fsk": fsk,
        "genre": genre,
        "desc": clean(desc) or None,
        "page": page,  # Filmseite beim Kino (für Beschreibung / Details)
    }


def tags_from_text(text):
    t = text.lower()
    out = []
    if re.search(r"\bomu\b|untertitel|omeu|\bomdu\b", t):
        out.append("OmU")
    elif re.search(r"\bov\b|original|englisch|english", t):
        out.append("OV")
    if "3d" in t:
        out.append("3D")
    if "imax" in t:
        out.append("IMAX")
    if "dolbycinema" in t or "dolby cinema" in t:
        out.append("Dolby Cinema")
    elif "atmos" in t:
        out.append("Atmos")
    if "dbox" in t or "d-box" in t:
        out.append("D-Box")
    if "laser" in t:
        out.append("Laser")
    if "4dx" in t:
        out.append("4DX")
    if "screenx" in t:
        out.append("ScreenX")
    return out


# --- CinemaxX (JSON-Schnittstelle der Website) -------------------------------------------

def cinemaxx(cid, api_id):
    base = "https://www.cinemaxx.de/api/microservice/showings"
    dates = json.loads(fetch(f"{base}/showingDates?cinemaId={api_id}&minEmbargoLevel=2&forNextWeek=true"))["result"]
    days = [d["showingDate"][:10] for d in dates if d.get("hasShowings")][:DAYS_AHEAD]
    shows, missing = [], []
    for day in days:
        try:
            data = json.loads(fetch(
                f"{base}/cinemas/{api_id}/films?showingDate={day}T00:00:00"
                "&minEmbargoLevel=3&includesSession=true&includeSessionAttributes=true", retries=1))
        except Exception:  # noqa: BLE001 – einzelne Tage verweigert die Seite gelegentlich
            missing.append(day)
            continue
        for f in data["result"]:
            for g in f.get("showingGroups", []):
                for s in g.get("sessions", []):
                    attrs = " ".join(f"{a.get('value','')} {a.get('name','')}" for a in s.get("attributes", []))
                    shows.append(show(
                        cid, f["filmTitle"], s["startTime"][:16],
                        tags=tags_from_text(attrs + " " + s.get("screenName", "")),
                        screen=s.get("screenName"),
                        url="https://www.cinemaxx.de" + s["bookingUrl"] if s.get("bookingUrl") else f.get("filmUrl"),
                        poster=f.get("posterImageSrc"),
                        minutes=f.get("runningTime") or None,
                        fsk=(f.get("certificate") or {}).get("name"),
                        genre=", ".join(f.get("genres") or []) or None,
                        desc=f.get("synopsisShort"),
                        page=f.get("filmUrl"),
                    ))
    return shows, missing


# --- Kinopolis HafenCity (Programmseite) --------------------------------------------------

def parse_kp_day(label, today):
    label = label.strip()
    if label.startswith("Heute"):
        return today
    if label.startswith("Morgen"):
        return today + timedelta(days=1)
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.", label)
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    y = today.year + (1 if mo < today.month - 6 else 0)
    return date(y, mo, d)


def kinopolis():
    today = datetime.now(BERLIN).date()
    shows, seen = [], set()
    pages = [("https://www.kinopolis.de/hh/programm/tagesprogramm-" + today.isoformat(), today),
             ("https://www.kinopolis.de/hh/programm", None),
             ("https://www.kinopolis.de/hh/programm/woche-1", None)]
    for url, default_day in pages:
        try:
            page = fetch(url)
        except Exception:  # noqa: BLE001 – Folgewoche ist oft noch nicht online
            continue
        shows += _kinopolis_page(page, today, default_day, seen)
    return shows, []


def _kinopolis_page(page, today, default_day, seen):
    shows = []
    blocks = re.split(r'<h2 class="hl--1 hidden-max-xs">', page)[1:]
    for b in blocks:
        tm = re.search(r'<a href="([^"]+)" class="hl-link">([^<]+)</a>', b)
        if not tm:
            continue
        title = clean(tm.group(2))
        film_page = "https://www.kinopolis.de" + tm.group(1)
        poster = re.search(r'<img src="(https://trailer\.kinopolis\.de/[^"]+)"', page[: page.find(b)][-3000:] + b)
        poster = poster.group(1) if poster else None
        minutes = re.search(r"Dauer:</span>\s*(\d+)", b)
        fsk = re.search(r"FSK:</span>\s*ab (\d+)", b)
        genre = re.search(r"Genre:</span>\s*([^<]+)<", b)
        # Tag je Vorstellung aus der Tagesleiste
        day_of = {}
        for lab_ids, lab in re.findall(r'data-performance-ids=\[([^\]]*)\]><div[^>]*><div class="prog-nav__day">([^<]*)<', b):
            d = parse_kp_day(lab, today)
            for pid in lab_ids.split(","):
                if pid.strip() and d:
                    day_of[pid.strip()] = d
        for pm in re.finditer(r'<div class="prog2__cont[^"]*" data-performance-id="([^"]+)">(.*?)(?=<div class="prog2__cont|</div>\s*</div>\s*</div>\s*<div class="prog-day__wrapper|$)', b, re.S):
            pid, body = pm.group(1), pm.group(2)
            d = day_of.get(pid, default_day)
            if pid in seen:
                continue
            seen.add(pid)
            t = re.search(r">\s*(\d{1,2}:\d{2})\s*</a>", body)
            if not d or not t:
                continue
            hall = re.search(r'prog2__hall-num[^>]*>\s*<div>([^<]+)</div>', body)
            tech = " ".join(re.findall(r'class="tech__icon (tech__[a-z0-9_]+)', body))
            tech += " " + clean(" ".join(re.findall(r'class="tech__icon[^"]*"[^>]*>(.*?)</div>', body)))
            hh, mm = t.group(1).split(":")
            shows.append(show(
                "kp", title, f"{d.isoformat()}T{int(hh):02d}:{mm}",
                tags=tags_from_text(tech + " " + (hall.group(1) if hall else "")),
                screen=clean(hall.group(1)) if hall else None,
                url=f"https://www.kinopolis.de/hh/programm/vorstellung/{pid}",
                poster=poster,
                minutes=int(minutes.group(1)) if minutes else None,
                fsk=f"FSK {fsk.group(1)}" if fsk else None,
                genre=clean(genre.group(1)) if genre else None,
                page=film_page,
            ))
    return shows


def kinopolis_desc(url):
    page = fetch(url)
    # erst nach den Filmdaten suchen – davor steht ein gleichnamiger Block mit der Kinoauswahl
    start = max(page.find("movie__specs"), 0)
    m = re.search(r'<div class="text">\s*(<p>.*?)</div>', page[start:], re.S)
    return clean(m.group(1)) if m else None


# --- Astor Film Lounge HafenCity (premiumkino-Backend) ------------------------------------

def astor():
    d = json.loads(fetch("https://backend.premiumkino.de/v1/de/hamburg/program"))
    try:
        cfg = json.loads(fetch("https://backend.premiumkino.de/v1/de/hamburg/config"))
        halls = {a["id"]: a["name"] for a in (cfg.get("cinema") or cfg).get("auditoriums", [])}
    except Exception:  # noqa: BLE001
        halls = {}
    movies = {m["id"]: m for m in d["movies"]}
    labels = {}
    for g in d.get("movieFilterGroups", []):
        for it in g.get("items", []):
            labels[it["value"]] = it["label"]
    genre_names = labels
    shows = []
    horizon = (datetime.now(BERLIN).date() + timedelta(days=60)).isoformat()
    for p in d["performances"]:
        if p["begin"][:10] > horizon:
            continue
        m = movies.get(p["movieId"], {})
        raw = m.get("name") or p["title"]
        title = re.sub(r"^(ATMOS|OV|OmU|OMU|3D|PREVIEW)\s*:\s*", "", raw, flags=re.I)
        extra = " ".join(labels.get(f, "") for f in p.get("filterIds", [])) + " " + p.get("language", "") + " " + p["title"]
        poster = (m.get("poster") or {}).get("src")
        begin = p["begin"][:16]
        shows.append(show(
            "astor", title, begin,
            tags=tags_from_text(extra.replace("Sprache: Deutsch", "")),
            screen=halls.get(p.get("auditoriumId")),
            url=f"https://hamburg.premiumkino.de/vorstellung/{p['slug']}/{begin[:10].replace('-', '')}/{begin[11:16].replace(':', '')}/{p['id']}",
            poster=f"https://cdn.premiumkino.de{poster}.jpg" if poster else None,
            minutes=m.get("minutes"),
            fsk=f"FSK {m['rating']}" if m.get("rating") is not None else None,
            genre=", ".join(genre_names.get(g, "") for g in m.get("genreIds", []) if genre_names.get(g)) or None,
            desc=next((t.get("descShort") or t.get("descLong") for t in m.get("translations", [])
                       if t.get("descShort") or t.get("descLong")), None),
            page=f"https://hamburg.premiumkino.de/film/{m['slug']}" if m.get("slug") else None,
        ))
    return shows, []


# --- UCI Wandsbek (Programmseite) ---------------------------------------------------------

def uci():
    page = fetch("https://www.uci-kinowelt.de/kinoprogramm/hh-wandsbek-smart-city/54")
    shows = []
    parts = re.split(r'(?=<h2 class="film-container__description__text__eventtitle">)', page)
    for i, b in enumerate(parts[1:], 1):
        tm = re.search(r'eventtitle">\s*<a href="([^"]+)">([^<]+)</a>', b)
        if not tm:
            continue
        title = clean(tm.group(2))
        prev = parts[i - 1][-6000:]
        poster = re.findall(r'srcset="(/Poster/[^" ]+)', prev)
        fsk = re.search(r'alt="FSK (\d+)"', b)
        desc = re.search(r'<dt>\s*Beschreibung\s*</dt>\s*<dd>(.*?)</dd>', b, re.S)
        genre = re.search(r'<dt>\s*Genre\s*</dt>\s*<dd>(.*?)</dd>', b, re.S)
        mins = re.search(r'(\d{2,3})\s*Min', b)
        for a in re.finditer(r'<a href="(https://buchung\.uci-kinowelt\.de/[^"]+)"\s+class="badge badge-performance([^"]*)"(.*?)>', b, re.S):
            attrs = a.group(3)
            dt = re.search(r'data-date="(\d{8})"', attrs)
            t = re.search(r'data-time="(\d{1,2}:\d{2})"', attrs)
            if not dt or not t:
                continue
            ver = re.search(r'data-version="([^"]*)"', attrs)
            aud = re.search(r'data-tracking-auditorium="([^"]*)"', attrs)
            v = (ver.group(1) if ver else "").replace("|", " ")
            v = re.sub(r"\bomeu\b", "omu", v)
            hh, mm = t.group(1).split(":")
            ds = dt.group(1)
            shows.append(show(
                "uci", title, f"{ds[:4]}-{ds[4:6]}-{ds[6:]}T{int(hh):02d}:{mm}",
                tags=tags_from_text(v),
                screen=aud.group(1) if aud else None,
                url=html.unescape(a.group(1)),
                poster="https://www.uci-kinowelt.de" + poster[-1] if poster else None,
                fsk=f"FSK {fsk.group(1)}" if fsk else None,
                desc=re.sub(r"\s*\(Quelle: [^)]*\)\s*$", "", clean(desc.group(1))) if desc else None,
                genre=clean(genre.group(1)) if genre else None,
                minutes=int(mins.group(1)) if mins else None,
                page="https://www.uci-kinowelt.de" + tm.group(1),
            ))
    return shows, []


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001
        return None


# --- Zusammenführen -----------------------------------------------------------------------

def norm_title(t):
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\((?:deutsche fassung|ov|omu|omeu|3d|2d|imax|dolby[^)]*|atmos|original[^)]*)\)", " ", t)
    t = re.sub(r"\b(ov|omu|omeu|3d|2d|imax|atmos)\b\s*$", " ", t)
    t = t.replace("&", " und ").replace("ß", "ss")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def build():
    sources = {
        "cxw": lambda: cinemaxx("cxw", 1208),
        "cxd": lambda: cinemaxx("cxd", 1206),
        "kp": kinopolis,
        "astor": astor,
        "uci": uci,
    }
    shows, status = [], {}
    with ThreadPoolExecutor(5) as ex:
        futs = {k: ex.submit(f) for k, f in sources.items()}
        for k, fu in futs.items():
            try:
                s, missing = fu.result()
                shows += s
                status[k] = {"ok": True, "count": len(s), "missingDays": missing}
            except Exception as e:  # noqa: BLE001
                status[k] = {"ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"[:200]}
            print(f"{k:6} {status[k]}", file=sys.stderr)

    now = datetime.now(BERLIN)
    cutoff = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")
    shows = [s for s in shows if s["start"] >= cutoff]

    films = {}
    for s in shows:
        key = norm_title(s["title"])
        f = films.setdefault(key, {"id": key.replace(" ", "-")[:60] or "film", "title": s["title"],
                                   "poster": None, "minutes": None, "fsk": None, "genre": None, "desc": None,
                                   "_n": {}, "_pages": []})
        f["_n"][s["title"]] = f["_n"].get(s["title"], 0) + 1
        for k in ("poster", "minutes", "fsk", "genre"):
            # CinemaxX/Kinopolis-Daten sind meist am vollständigsten, erster Treffer gewinnt
            if not f[k] and s.get(k):
                f[k] = s[k]
        # längste Beschreibung gewinnt (CinemaxX kürzt mit „...“)
        if s.get("desc") and len(s["desc"]) > len(f["desc"] or ""):
            f["desc"] = s["desc"]
        if s.get("page") and s["page"] not in f["_pages"]:
            f["_pages"].append(s["page"])
        s["film"] = f["id"]
    # Filme ohne brauchbare Beschreibung: bei Kinopolis auf der Filmseite nachsehen
    todo = [f for f in films.values() if not f["desc"] or len(f["desc"]) < 60]
    kp_pages = {id(f): next((p for p in f["_pages"] if "kinopolis.de" in p), None) for f in todo}
    with ThreadPoolExecutor(4) as ex:
        found = dict(zip(kp_pages, ex.map(lambda u: _safe(kinopolis_desc, u) if u else None, kp_pages.values())))
    for f in todo:
        if found.get(id(f)):
            f["desc"] = found[id(f)]
    for f in films.values():
        f["title"] = max(f.pop("_n").items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
        f.pop("_pages")
        if f["desc"] and len(f["desc"]) > 700:
            f["desc"] = f["desc"][:700].rsplit(" ", 1)[0] + " …"

    out_shows = [{k: s[k] for k in ("film", "cinema", "start", "tags", "screen", "url") if s.get(k)} for s in
                 sorted(shows, key=lambda s: (s["start"], s["cinema"]))]
    return {
        "generated": now.isoformat(timespec="minutes"),
        "cinemas": [dict(c, **{"status": status.get(c["id"])}) for c in CINEMAS],
        "films": sorted(films.values(), key=lambda f: f["title"].lower()),
        "shows": out_shows,
    }


if __name__ == "__main__":
    data = build()
    if not data["shows"]:
        sys.exit("Keine Vorstellungen gefunden – data.json bleibt unverändert.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(data['films'])} Filme, {len(data['shows'])} Vorstellungen → {OUT}", file=sys.stderr)
