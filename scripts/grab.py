#!/usr/bin/env python3
"""grab — торент → Jellyfin в одну команду для стека media-stack.

Виконується ВСЕРЕДИНІ LXC `media-server` (потрібні 127.0.0.1-порти сервісів
і /opt/media-stack/config). З ноутбука запускається обгорткою ./grab у корені репо.

    grab                     що качається / що чекає імпорту / що ще недоступне
    grab "аріетті"           пошук по toloka+rutracker через Prowlarr
    grab 0                   взяти реліз №0 з останнього пошуку
    grab 0 --to movie:25     те саме, але з явною прив'язкою до фільму Radarr
    grab finish              доімпортувати все, що докачалось (cron робить це сам)
    grab subs <фільм>        українські субтитри: взяти англійські й перекласти локально
    grab import <part> --to movie:25   ручний імпорт торента, що вже лежить у qB

Навіщо: Radarr шукає за англійською назвою з TMDB, а на toloka релізи названі
українською — тому автопошук повертає 0 результатів. grab шукає довільним
запитом, а прив'язку до конкретного запиту (movieId/seriesId) робить явно,
тому імпорт не залежить від того, чи розпарсить Radarr українську назву.
"""

import argparse
import fcntl
import http.cookiejar
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

CONFIG_ROOT = "/opt/media-stack/config"
STATE_PATH = "/opt/media-stack/grab-state.json"
LOCK_PATH = "/opt/media-stack/grab.lock"
TRANSLATOR = "/opt/media-stack/subs-translate.py"
# Те саме роздвоєння шляхів, що і в torrent_content: /data/... бачать лише
# контейнери, а grab виконується на LXC.
HOST_DATA = "/opt/media-stack/data"
TORRENT_DIR = "/data/torrents"

BAZARR = "http://127.0.0.1:6767"
RADARR = "http://127.0.0.1:7878"
SONARR = "http://127.0.0.1:8989"
PROWLARR = "http://127.0.0.1:9696"
QBIT = "http://127.0.0.1:8085"
SEERR = "http://127.0.0.1:5055"
JELLYFIN = "http://127.0.0.1:8096"

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov")

# Кирилиця, що виглядає як латиниця: "1080р" на toloka — з кириличною 'р'.
HOMOGLYPHS = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ѕ": "s",
})


# --------------------------------------------------------------------------
# credentials — читаються з конфігів у рантаймі, нічого не зашито в код
# --------------------------------------------------------------------------

def arr_key(service):
    path = os.path.join(CONFIG_ROOT, service, "config.xml")
    with open(path, encoding="utf-8") as fh:
        return re.search(r"<ApiKey>([^<]+)", fh.read()).group(1)


def seerr_settings():
    with open(os.path.join(CONFIG_ROOT, "jellyseerr", "settings.json"), encoding="utf-8") as fh:
        return json.load(fh)


def bazarr_key():
    path = os.path.join(CONFIG_ROOT, "bazarr", "config", "config.yaml")
    with open(path, encoding="utf-8") as fh:
        return re.search(r"apikey:\s*(\S+)", fh.read()).group(1)


def on_lxc(container_path):
    """Шлях у namespace контейнера → шлях, видимий grab'у на LXC."""
    return container_path.replace("/data/", HOST_DATA + "/", 1)


def qb_creds():
    """Логін/пароль qBittorrent лежать у налаштуваннях download client'а Radarr."""
    uri = "file:%s?mode=ro" % os.path.join(CONFIG_ROOT, "radarr", "radarr.db")
    con = sqlite3.connect(uri, uri=True)
    try:
        row = con.execute(
            "SELECT Settings FROM DownloadClients WHERE Implementation='QBittorrent'"
        ).fetchone()
    finally:
        con.close()
    cfg = json.loads(row[0])
    return cfg["username"], cfg["password"]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def api(url, key, method="GET", data=None, timeout=300, header="X-Api-Key"):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={header: key, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_release(url):
    """('magnet', uri) або ('torrent', bytes) за посиланням Prowlarr.

    Тягне саме grab, а не qBittorrent: посилання Prowlarr вказує на
    127.0.0.1:9696, а всередині контейнера qB це вже сам qB. Індексери на кшталт
    Nyaa віддають лише магнет — Prowlarr тоді відповідає 301 на `magnet:`, який
    urllib за редіректом пройти не вміє, тому Location читаємо вручну.
    """
    if url.startswith("magnet:"):
        return "magnet", url
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=120) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        location = (exc.headers.get("Location") or "") if exc.headers else ""
        if 300 <= exc.code < 400 and location.startswith("magnet:"):
            return "magnet", location
        raise
    if blob[:1] != b"d":
        raise SystemExit("Prowlarr віддав не .torrent (%r…) — перевір індексер." % blob[:40])
    return "torrent", blob


class QBit:
    def __init__(self):
        user, password = qb_creds()
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        payload = urllib.parse.urlencode({"username": user, "password": password}).encode()
        answer = self.opener.open(QBIT + "/api/v2/auth/login", payload, timeout=30).read()
        # 5.1 відповідало тілом "Ok." / "Fails."; 5.2.3 віддає 204 з порожнім тілом
        # і лише ставить кукі. Тому ознака успіху — саме кукі сесії, не текст.
        if b"Fails" in answer or not any(c.name.startswith("QBT_SID") for c in jar):
            raise SystemExit("qBittorrent: логін не вдався (%r)" % answer[:60])

    def torrents(self):
        raw = self.opener.open(QBIT + "/api/v2/torrents/info", timeout=60).read()
        return json.loads(raw)

    def files(self, torrent_hash):
        raw = self.opener.open(
            QBIT + "/api/v2/torrents/files?hash=%s" % torrent_hash, timeout=60).read()
        return json.loads(raw)

    def add_file(self, blob, filename, savepath=TORRENT_DIR):
        """Кладемо .torrent як multipart. Категорію НЕ ставимо навмисно: торент з
        категорією `radarr` Radarr спробує імпортувати сам, не зможе розпарсити
        українську назву і повісить у черзі рядок 'Unknown Movie' (див. runbook §7)."""
        boundary = "----grab-%d" % int(time.time())
        chunks = []

        def field(name, value):
            chunks.append(
                ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                 % (boundary, name, value)).encode())

        field("savepath", savepath)
        field("autoTMM", "false")
        chunks.append(
            ('--%s\r\nContent-Disposition: form-data; name="torrents"; filename="%s"\r\n'
             'Content-Type: application/x-bittorrent\r\n\r\n' % (boundary, filename)).encode())
        chunks.append(blob + b"\r\n")
        chunks.append(("--%s--\r\n" % boundary).encode())
        body = b"".join(chunks)
        req = urllib.request.Request(
            QBIT + "/api/v2/torrents/add", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        return self.opener.open(req, timeout=120).read()

    def add_url(self, url, savepath=TORRENT_DIR):
        payload = urllib.parse.urlencode({"urls": url, "savepath": savepath,
                                          "autoTMM": "false"}).encode()
        return self.opener.open(QBIT + "/api/v2/torrents/add", payload, timeout=120).read()


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def take_lock(quiet):
    """Імпорт має виконуватись в один потік: cron (*/5) і ручний запуск легко
    перетинаються, а два ManualImport одного файлу дають дубль у бібліотеці."""
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not quiet:
            print("Інший grab уже імпортує — пропускаю.")
        return None
    return handle  # тримаємо відкритим до кінця процесу


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_search": None, "tracked": []}
    with open(STATE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------
# зіставлення релізу із запитом (movieId / seriesId)
# --------------------------------------------------------------------------

def normalize(text):
    text = unicodedata.normalize("NFKD", (text or "").lower()).translate(HOMOGLYPHS)
    return re.sub(r"[^a-z0-9а-яїієґ ]+", " ", text)


STOPWORDS = {"the", "and", "for", "you", "der", "die", "das", "los", "las",
             "collection", "trilogy", "part", "уперед", "збірка", "колекція"}


def tokens(text):
    return {t for t in normalize(text).split() if len(t) > 2 and t not in STOPWORDS}


def candidates():
    """Усі фільми Radarr та серіали Sonarr як можливі цілі імпорту."""
    out = []
    for movie in api(RADARR + "/api/v3/movie", arr_key("radarr")):
        titles = [movie["title"], movie.get("originalTitle")]
        titles += [alt["title"] for alt in movie.get("alternateTitles", [])]
        out.append({
            "kind": "movie", "id": movie["id"], "title": movie["title"],
            "year": movie.get("year"), "titles": [t for t in titles if t],
            "have": movie.get("hasFile", False), "path": movie.get("path"),
        })
    for series in api(SONARR + "/api/v3/series", arr_key("sonarr")):
        titles = [series["title"]]
        titles += [alt["title"] for alt in series.get("alternateTitles", [])]
        stats = series.get("statistics") or {}
        out.append({
            "kind": "series", "id": series["id"], "title": series["title"],
            "year": series.get("year"), "titles": [t for t in titles if t],
            "have": stats.get("episodeFileCount", 0) >= stats.get("episodeCount", 1),
            "path": series.get("path"),
        })
    return out


def match_target(release_title, pool):
    """Оцінка «наскільки цей реліз схожий на цей запит».

    Релізи toloka майже завжди містять і оригінальну/англійську назву
    ("… / Tonari no Yamada-kun (1999) BDRip …"), тому збіг за alternateTitles
    з TMDB спрацьовує навіть коли основна назва — українська.
    """
    haystack = normalize(release_title)
    hay_tokens = tokens(release_title)
    best, best_score = None, 0.0
    for cand in pool:
        score = 0.0
        for title in cand["titles"]:
            norm = normalize(title).strip()
            if len(norm) > 3 and norm in haystack:
                score = max(score, 4.0 + len(norm) / 100.0)
            sig = tokens(title)
            if not sig:
                continue
            overlap = sig & hay_tokens
            # Одного спільного слова замало: "Панда Кунг-фу: Колекція" ділить
            # рівно "панда" з "Panda! Go Panda!" — і без цього обмеження 23 ГБ
            # чужого релізу причепились би до дитячого мультика 1972 року.
            if len(overlap) >= 2:
                score = max(score, 3.0 * len(overlap) / len(sig))
            elif overlap:
                score = max(score, 2.0 if len(sig) == 1 else 1.5 / len(sig))
        if cand["year"] and str(cand["year"]) in release_title:
            score += 1.5
        if not cand["have"]:
            score += 0.4  # за інших рівних — те, чого ще немає
        if score > best_score:
            best, best_score = cand, score
    return (best, round(best_score, 2)) if best_score >= 3.0 else (None, round(best_score, 2))


# --------------------------------------------------------------------------
# якість і мови
# --------------------------------------------------------------------------

QUALITY_RULES = [
    (r"remux.*2160|2160.*remux", "Remux-2160p"), (r"remux", "Remux-1080p"),
    (r"(bdrip|bluray|blu-ray|bdremux|brrip).*2160|2160.*(bdrip|bluray)", "Bluray-2160p"),
    (r"(bdrip|bluray|blu-ray|bdremux|brrip).*1080|1080.*(bdrip|bluray|bdremux)", "Bluray-1080p"),
    (r"(bdrip|bluray|blu-ray|brrip).*720|720.*(bdrip|bluray)", "Bluray-720p"),
    (r"web-?dl.*2160|2160.*web-?dl", "WEBDL-2160p"),
    (r"web-?dl.*1080|1080.*web-?dl|web-?rip.*1080", "WEBDL-1080p"),
    (r"web-?dl.*720|720.*web-?dl|web-?rip.*720", "WEBDL-720p"),
    (r"hdtv.*1080|1080.*hdtv", "HDTV-1080p"),
    (r"hdtv.*720|720.*hdtv", "HDTV-720p"),
    (r"\b1080[pi]?\b", "WEBDL-1080p"), (r"\b720[pi]?\b", "WEBDL-720p"),
]

LANGUAGE_RULES = [(r"ukr|укр|1xukr|2xukr|3xukr", (32, "Ukrainian")),
                  (r"\beng?\b|англ", (1, "English")),
                  (r"jap|jpn|япон", (8, "Japanese"))]


def quality_for(name, table):
    flat = normalize(name)
    for pattern, quality_name in QUALITY_RULES:
        if re.search(pattern, flat):
            hit = table.get(quality_name.lower())
            if hit:
                return {"quality": hit, "revision": {"version": 1, "real": 0, "isRepack": False}}
    return None


def languages_for(name):
    flat = normalize(name)
    found = [{"id": lid, "name": lname}
             for pattern, (lid, lname) in LANGUAGE_RULES if re.search(pattern, flat)]
    return found or [{"id": 1, "name": "English"}]


def quality_table(base, key):
    table = {}
    for row in api(base + "/api/v3/qualitydefinition", key):
        quality = row["quality"]
        table[quality["name"].lower()] = quality
    return table


# --------------------------------------------------------------------------
# команди
# --------------------------------------------------------------------------

def cmd_search(query, state):
    key = arr_key("prowlarr")
    url = PROWLARR + "/api/v1/search?" + urllib.parse.urlencode({"query": query, "type": "search"})
    results = api(url, key, timeout=300) or []
    results.sort(key=lambda r: (r.get("seeders") or 0), reverse=True)
    pool = candidates()

    # Той самий реліз часто лежить на кількох трекерах (Mazepa здебільшого дублює
    # Toloka, але з мертвим роєм). Згортаємо за точним розміром у байтах —
    # результати вже відсортовані за сідами, тож лишається найживіша копія.
    rows, seen = [], {}
    for item in results:
        size = item.get("size") or 0
        if size and size in seen:
            other = item.get("indexer")
            if other and other not in seen[size]["also"]:
                seen[size]["also"].append(other)
            continue
        target, score = match_target(item.get("title") or "", pool)
        row = {
            "title": item.get("title"), "size": size,
            "seeders": item.get("seeders"), "indexer": item.get("indexer"),
            "downloadUrl": item.get("downloadUrl"), "magnetUrl": item.get("magnetUrl"),
            "guid": item.get("guid"), "also": [],
            "categories": [c.get("name") for c in (item.get("categories") or []) if c.get("name")],
            "target": target, "score": score,
        }
        if size:
            seen[size] = row
        rows.append(row)

    state["last_search"] = {"query": query, "results": rows}
    save_state(state)

    if not rows:
        print("Нічого не знайдено за запитом %r." % query)
        print("Спробуй іншу форму назви — українську, оригінальну або транслітерацію.")
        return 0

    print("Знайдено %d релізів за запитом %r:\n" % (len(rows), query))
    for idx, row in enumerate(rows):
        target = row["target"]
        aim = ("→ %s (%s)" % (target["title"], target["year"])) if target else "→ ціль не вгадав, вкажи --to"
        flag = "" if not target or not target["have"] else "  [вже є файл]"
        also = (" (також: %s)" % ", ".join(row["also"])) if row.get("also") else ""
        print("[%d] %s" % (idx, row["title"]))
        print("    %.2f ГБ · сідів %s · %s%s · %s" % (
            row["size"] / 1e9, row["seeders"], row["indexer"], also,
            ", ".join(row["categories"][:2])))
        print("    %s%s" % (aim, flag))
    print("\nВзяти:  grab <номер>            (напр. grab 0)")
    print("Явно:   grab <номер> --to movie:<id>|series:<id>")
    return 0


def resolve_target(spec, pool):
    kind, _, raw_id = spec.partition(":")
    kind = {"movie": "movie", "film": "movie", "series": "series", "tv": "series"}.get(kind)
    if not kind or not raw_id.isdigit():
        raise SystemExit("--to очікує movie:<id> або series:<id>, отримав %r" % spec)
    for cand in pool:
        if cand["kind"] == kind and cand["id"] == int(raw_id):
            return cand
    raise SystemExit("У %s немає запису з id=%s" % (kind, raw_id))


def cmd_get(index, to_spec, state):
    search = state.get("last_search")
    if not search or not search["results"]:
        raise SystemExit("Немає збереженого пошуку — спершу `grab \"<запит>\"`.")
    if index >= len(search["results"]):
        raise SystemExit("Немає релізу №%d (у пошуку %d)." % (index, len(search["results"])))

    row = search["results"][index]
    target = resolve_target(to_spec, candidates()) if to_spec else row.get("target")
    if not target:
        raise SystemExit("Не вгадав ціль для цього релізу — додай --to movie:<id> "
                         "(id видно у `grab pending`).")

    qb = QBit()
    before = {t["hash"] for t in qb.torrents()}

    # Prowlarr проксує завантаження зі своїми кукі до трекера, тому окремий
    # логін на toloka не потрібен.
    link = row.get("downloadUrl") or row.get("magnetUrl")
    if not link:
        raise SystemExit("У релізу немає ні downloadUrl, ні magnet.")
    kind, payload = fetch_release(link)
    if kind == "magnet":
        qb.add_url(payload)
    else:
        qb.add_file(payload, "grab-%d.torrent" % index)

    new_hash, name = None, row["title"]
    for _ in range(20):
        time.sleep(1.5)
        current = {t["hash"]: t for t in qb.torrents()}
        fresh = set(current) - before
        if fresh:
            new_hash = fresh.pop()
            name = current[new_hash]["name"]
            break
    if not new_hash:
        raise SystemExit("qBittorrent не показав новий торент — перевір його вручну.")

    state["tracked"].append({
        "hash": new_hash, "name": name, "release": row["title"],
        "size": row["size"], "target": {k: target[k] for k in ("kind", "id", "title", "year")},
        "added": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "downloading",
    })
    save_state(state)

    print("✓ Додано в qBittorrent: %s" % name)
    print("  %.2f ГБ → %s (%s)" % (row["size"] / 1e9, target["title"], target["kind"]))
    print("  Далі нічого робити не треба: cron доімпортує сам (або `grab finish`).")
    return 0


def torrent_content(qb, torrent):
    """Тека роздачі + абсолютні шляхи докачаних відеофайлів (у namespace контейнерів).

    os.path.isdir() тут використати не можна: шляхи виду /data/... існують лише
    всередині контейнерів, а grab працює на LXC — перевірка завжди дала б False
    і для роздачі-теки в *arr пішов би батьківський /data/torrents, після чого
    сканується весь каталог, а файли роздачі не знаходяться. Структуру тому
    беремо з самого qBittorrent: імена файлів у ньому відносні до save_path.
    Невибрані (priority 0) і недокачані файли відкидаємо — у сезонних паках
    часто завантажена лише частина.
    """
    files = qb.files(torrent["hash"])
    save = torrent["save_path"].rstrip("/")
    nested = any("/" in f["name"] for f in files)
    root = os.path.join(save, files[0]["name"].split("/")[0]) if nested else save
    paths = [os.path.join(save, f["name"]) for f in files
             if f.get("priority", 1) != 0 and f.get("progress", 0) >= 1
             and f["name"].lower().endswith(VIDEO_EXT)]
    return root, paths


def import_movie(entry, torrent, table, qb):
    key = arr_key("radarr")
    folder, paths = torrent_content(qb, torrent)
    if not paths:
        return False, "у роздачі немає докачаних відеофайлів"
    url = RADARR + "/api/v3/manualimport?" + urllib.parse.urlencode(
        {"folder": folder, "filterExistingFiles": "false"})
    rows = api(url, key, timeout=600) or []

    rows = [r for r in rows if r.get("path") in set(paths)]
    if not rows:
        return False, "Radarr не побачив відеофайлів роздачі у %s" % folder
    # Роздача може містити кілька фільмів (напр. "Panda! Go Panda!" 1972 разом із
    # продовженням 1973-го). Найбільший файл тоді — не той: спершу шукаємо той,
    # що збігається роком, і лише як запасний варіант беремо найбільший.
    year = str(entry["target"].get("year") or "")
    by_year = [r for r in rows if year and year in os.path.basename(r["path"])]
    row = max(by_year or rows, key=lambda r: r.get("size") or 0)

    # Назва релізу на трекері авторитетніша за ім'я файлу: "My.Neighbors.the.
    # Yamadas.1999(Artymko).mkv" не містить джерела, і Radarr здогадується по
    # роздільній здатності → WEBDL-1080p, хоча реліз — BDRip. Занижена якість
    # нижче cutoff'а профілю робить фільм кандидатом на «апгрейд» і перекачку.
    quality = quality_for(entry["release"], table) or row.get("quality")
    if not quality or quality.get("quality", {}).get("id", 0) == 0:
        quality = quality_for(row["path"], table)
    if not quality:
        return False, "не вдалось визначити якість для %s" % os.path.basename(row["path"])

    languages = [l for l in (row.get("languages") or []) if l.get("id", 0) > 0]
    payload = {"name": "ManualImport", "importMode": "copy", "files": [{
        "path": row["path"], "movieId": entry["target"]["id"],
        "quality": quality, "languages": languages or languages_for(entry["release"]),
        "releaseGroup": row.get("releaseGroup") or "",
    }]}
    command = api(RADARR + "/api/v3/command", key, method="POST", data=payload)
    if not wait_command(RADARR, key, command["id"]):
        return False, "команда ManualImport завершилась помилкою"

    movie = api(RADARR + "/api/v3/movie/%d" % entry["target"]["id"], key)
    if not movie.get("hasFile"):
        return False, "Radarr прийняв команду, але файл не з'явився у фільмі"
    return True, movie["movieFile"]["relativePath"]


def import_series(entry, torrent, table, qb):
    key = arr_key("sonarr")
    folder, paths = torrent_content(qb, torrent)
    if not paths:
        return False, "у роздачі немає докачаних відеофайлів"
    url = SONARR + "/api/v3/manualimport?" + urllib.parse.urlencode(
        {"folder": folder, "seriesId": entry["target"]["id"], "filterExistingFiles": "false"})
    rows = api(url, key, timeout=600) or []

    rows = [r for r in rows if r.get("path") in set(paths)]
    if not rows:
        return False, "Sonarr не побачив відеофайлів роздачі у %s" % folder

    files, skipped = [], []
    for row in rows:
        episodes = [e["id"] for e in (row.get("episodes") or [])]
        if not episodes:
            skipped.append(os.path.basename(row["path"]))
            continue
        quality = quality_for(entry["release"], table) or row.get("quality")
        if not quality or quality.get("quality", {}).get("id", 0) == 0:
            quality = quality_for(row["path"], table)
        if not quality:
            skipped.append(os.path.basename(row["path"]))
            continue
        languages = [l for l in (row.get("languages") or []) if l.get("id", 0) > 0]
        files.append({"path": row["path"], "seriesId": entry["target"]["id"],
                      "episodeIds": episodes, "quality": quality,
                      "languages": languages or languages_for(entry["release"]),
                      "releaseGroup": row.get("releaseGroup") or ""})
    if not files:
        return False, ("Sonarr не зміг розпізнати номери серій — імпортуй вручну: %s"
                       % ", ".join(skipped[:3]))

    command = api(SONARR + "/api/v3/command", key, method="POST",
                  data={"name": "ManualImport", "importMode": "copy", "files": files})
    if not wait_command(SONARR, key, command["id"]):
        return False, "команда ManualImport завершилась помилкою"
    note = "%d серій" % len(files)
    if skipped:
        note += " (не розпізнано: %s)" % ", ".join(skipped[:3])
    return True, note


def wait_command(base, key, command_id, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = api(base + "/api/v3/command/%d" % command_id, key)
        if status["status"] in ("completed", "failed", "aborted"):
            return status["status"] == "completed"
        time.sleep(2)
    return False


def notify_jellyfin_and_seerr():
    settings = seerr_settings()
    try:
        api(JELLYFIN + "/Library/Refresh", settings["jellyfin"]["apiKey"],
            method="POST", data={}, header="X-Emby-Token", timeout=60)
    except urllib.error.HTTPError as exc:
        print("  ! Jellyfin refresh: %s" % exc)
    time.sleep(20)  # даємо сканеру побачити новий файл
    for job in ("jellyfin-recently-added-scan", "availability-sync"):
        try:
            api(SEERR + "/api/v1/settings/jobs/%s/run" % job, settings["main"]["apiKey"],
                method="POST", data={}, timeout=120)
        except urllib.error.HTTPError as exc:
            print("  ! Seerr job %s: %s" % (job, exc))


def cmd_finish(state, quiet=False):
    pending = [e for e in state["tracked"] if e["status"] == "downloading"]
    if not pending:
        if not quiet:
            print("Нічого не чекає імпорту.")
        return 0

    qb = QBit()
    torrents = {t["hash"]: t for t in qb.torrents()}
    tables = {"movie": None, "series": None}
    imported = []

    for entry in pending:
        torrent = torrents.get(entry["hash"])
        if not torrent:
            entry["status"] = "lost"
            entry["note"] = "торента вже немає в qBittorrent"
            continue
        if torrent["progress"] < 1:
            if not quiet:
                print("… %s — %.1f%% (%s)" % (entry["name"][:60], torrent["progress"] * 100,
                                              torrent["state"]))
            continue

        kind = entry["target"]["kind"]
        if tables[kind] is None:
            base, key = (RADARR, arr_key("radarr")) if kind == "movie" else (SONARR, arr_key("sonarr"))
            tables[kind] = quality_table(base, key)
        worker = import_movie if kind == "movie" else import_series
        try:
            ok, note = worker(entry, torrent, tables[kind], qb)
        except Exception as exc:  # мережа/API — лишаємо в черзі на наступний прогін
            print("! %s — імпорт впав: %s" % (entry["name"][:60], exc))
            continue

        entry["status"] = "imported" if ok else "failed"
        entry["note"] = note
        print(("✓ Імпортовано: %s → %s" if ok else "! Не вийшло: %s — %s")
              % (entry["target"]["title"], note))
        if ok:
            imported.append(entry)

    save_state(state)
    if imported:
        notify_jellyfin_and_seerr()
        print("Jellyfin просканував, Seerr оновив доступність — %d поз." % len(imported))
    return 0


def cmd_import(part, to_spec, state):
    """Ручний гачок: підхопити торент, що вже лежить у qB (докачаний раніше)."""
    qb = QBit()
    hits = [t for t in qb.torrents() if part.lower() in t["name"].lower()]
    if not hits:
        raise SystemExit("У qBittorrent немає торента з %r у назві." % part)
    if len(hits) > 1:
        print("Підходить кілька — уточни:")
        for t in hits:
            print("  -", t["name"])
        return 1
    torrent = hits[0]
    target = resolve_target(to_spec, candidates())
    entry = {"hash": torrent["hash"], "name": torrent["name"], "release": torrent["name"],
             "size": torrent.get("size", 0),
             "target": {k: target[k] for k in ("kind", "id", "title", "year")},
             "added": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "downloading"}
    state["tracked"].append(entry)
    save_state(state)
    return cmd_finish(state)


def bazarr_form(path, pairs, method="POST"):
    body = urllib.parse.urlencode(list(pairs), doseq=True).encode()
    req = urllib.request.Request(BAZARR + path, data=body, method=method,
                                 headers={"X-API-KEY": bazarr_key(),
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=900) as resp:
        return resp.status


def fetch_english_srt(movie_id, folder):
    """Попросити Bazarr завантажити найкращі англійські субтитри."""
    key = bazarr_key()
    found = api(BAZARR + "/api/providers/movies?radarrid=%d" % movie_id, key,
                timeout=600, header="X-API-KEY") or {}
    data = found.get("data", found if isinstance(found, list) else [])
    # Bazarr віддає ці прапорці рядками "True"/"False", не булевими.
    english = [d for d in data
               if (d.get("language") or "").lower().startswith("en")
               and str(d.get("hearing_impaired")).lower() == "false"]
    english.sort(key=lambda d: -(d.get("score") or 0))
    if not english:
        return None
    best = english[0]
    print("   англійські субтитри: score %s від %s" % (best.get("score"), best.get("provider")))
    # OpenSubtitles регулярно рве першу спробу (RemoteDisconnected), а Bazarr
    # усе одно відповідає 204 — тому дивимось на диск, а не на код відповіді,
    # і пробуємо двічі.
    for attempt in range(2):
        bazarr_form("/api/providers/movies", [
            ("radarrid", movie_id), ("hi", "False"), ("forced", "False"),
            ("original_format", "False"), ("provider", best.get("provider")),
            ("subtitle", best.get("subtitle")),
        ])
        for _ in range(20):
            time.sleep(3)
            hit = [f for f in os.listdir(folder) if f.lower().endswith(".en.srt")]
            if hit:
                return os.path.join(folder, hit[0])
        if attempt == 0:
            print("   провайдер не віддав файл — друга спроба")
    return None


def cmd_subs(query, state):
    """Знайти фільм, дістати англійські субтитри й перекласти їх українською."""
    movies = [c for c in candidates() if c["kind"] == "movie" and c["have"]]
    needle = normalize(query)
    # Шукаємо по всіх назвах із TMDB, а не лише по основній: у Radarr вона
    # англійська, а користувач природно пише українською ("аріетті").
    hits = [m for m in movies if any(needle in normalize(t) for t in m["titles"])]
    if not hits:
        hits = [m for m in movies
                if any(tokens(query) & tokens(t) for t in m["titles"] if tokens(t))]
    if not hits:
        raise SystemExit("Не знайшов фільму з файлом за запитом %r." % query)
    if len(hits) > 1:
        print("Підходить кілька — уточни запит:")
        for m in hits[:10]:
            print("   %s (%s)" % (m["title"], m["year"]))
        return 1

    movie = hits[0]
    folder = on_lxc(movie["path"])
    if not os.path.isdir(folder):
        raise SystemExit("Тека фільму не знайдена: %s" % folder)
    print("фільм: %s (%s)" % (movie["title"], movie["year"]))

    if [f for f in os.listdir(folder) if f.lower().endswith(".uk.srt")]:
        print("   українські субтитри вже є — нічого не роблю")
        return 0

    english = [f for f in os.listdir(folder) if f.lower().endswith(".en.srt")]
    path = os.path.join(folder, english[0]) if english else fetch_english_srt(movie["id"], folder)
    if not path:
        raise SystemExit("Англійських субтитрів немає ні на диску, ні в провайдерів — "
                         "перекладати нема з чого.")

    print("   перекладаю %s" % os.path.basename(path))
    code = subprocess.call(["python3", TRANSLATOR, path])
    if code != 0:
        raise SystemExit("переклад не вдався (код %d)" % code)

    # хай Bazarr зарахує мову, а Jellyfin покаже доріжку
    bazarr_form("/api/movies", [("radarrid", movie["id"]), ("action", "scan-disk")],
                method="PATCH")
    settings = seerr_settings()
    try:
        api(JELLYFIN + "/Library/Refresh", settings["jellyfin"]["apiKey"],
            method="POST", data={}, header="X-Emby-Token", timeout=60)
    except urllib.error.HTTPError as exc:
        print("   ! Jellyfin refresh: %s" % exc)
    print("   готово — Bazarr і Jellyfin оновлені")
    return 0


def cmd_status(state):
    settings = seerr_settings()
    qb = QBit()
    torrents = {t["hash"]: t for t in qb.torrents()}

    active = [e for e in state["tracked"] if e["status"] == "downloading"]
    print("== qBittorrent: під наглядом grab ==")
    if not active:
        print("  (порожньо)")
    for entry in active:
        torrent = torrents.get(entry["hash"])
        if not torrent:
            print("  ? %s — торента немає в qB" % entry["name"][:60])
            continue
        speed = (torrent.get("dlspeed") or 0) / 1e6
        eta = torrent.get("eta")
        eta_text = "—" if not eta or eta > 8639999 else "%d хв" % (eta // 60)
        print("  %5.1f%%  %s" % (torrent["progress"] * 100, entry["name"][:62]))
        print("         %.1f МБ/с · залишилось %s · → %s"
              % (speed, eta_text, entry["target"]["title"]))

    print("\n== Seerr: запити без файлу ==")
    requests = api(SEERR + "/api/v1/request?take=40&sort=added&sortDirection=desc",
                   settings["main"]["apiKey"])
    radarr_key, sonarr_key = arr_key("radarr"), arr_key("sonarr")
    shown = 0
    for req in requests.get("results", []):
        media = req.get("media", {})
        if media.get("status") == 5:
            continue
        kind = "movie" if req["type"] == "movie" else "series"
        service_id = media.get("externalServiceId")
        title, have = None, False
        try:
            if kind == "movie" and service_id:
                movie = api(RADARR + "/api/v3/movie/%d" % service_id, radarr_key)
                title, have = "%s (%s)" % (movie["title"], movie["year"]), movie["hasFile"]
            elif service_id:
                series = api(SONARR + "/api/v3/series/%d" % service_id, sonarr_key)
                stats = series.get("statistics") or {}
                title = "%s (%s)" % (series["title"], series.get("year"))
                have = stats.get("episodeFileCount", 0) > 0
        except urllib.error.HTTPError:
            pass
        shown += 1
        if not title:
            print("  %-52s немає в Radarr/Sonarr (tmdb %s)"
                  % ("запит #%s" % req["id"], media.get("tmdbId")))
            continue
        print("  %-52s %s  → grab <№> --to %s:%s"
              % (title[:52], "частково" if have else "немає файлу", kind, service_id))
    if not shown:
        print("  (усе доступне)")
    return 0


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("args", nargs="*")
    parser.add_argument("--to", default=None)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    opts = parser.parse_args()

    if opts.help:
        print(__doc__)
        return 0

    argv = opts.args
    if argv and argv[0] in ("finish", "f", "import"):
        if take_lock(opts.quiet) is None:
            return 0

    state = load_state()

    if not argv:
        return cmd_status(state)
    head = argv[0]
    if head in ("finish", "f"):
        return cmd_finish(state, quiet=opts.quiet)
    if head in ("status", "pending", "s"):
        return cmd_status(state)
    if head == "subs":
        if len(argv) < 2:
            raise SystemExit("Використання: grab subs <назва фільму>")
        return cmd_subs(" ".join(argv[1:]), state)
    if head == "import":
        if len(argv) < 2 or not opts.to:
            raise SystemExit("Використання: grab import <частина назви> --to movie:<id>")
        return cmd_import(argv[1], opts.to, state)
    if head.isdigit():
        return cmd_get(int(head), opts.to, state)
    return cmd_search(" ".join(argv), state)


if __name__ == "__main__":
    sys.exit(main())
