"""Wysyłanie danych widgetu do chmury (Firestore apki Life) w tle.

Widget zapisuje dane w plikach (data/tasks.json, data/history.jsonl, data/categories.json, timelog.jsonl).
Ten moduł co kilka sekund porównuje je z tym, co już wysłano, i wysyła tylko różnice, więc trwający stoper
(pole runStart), przerwy, zaliczenia, kategorie i zmiany nazw trafiają do apki bez dodatkowych kroków.
Kierunek: widget -> chmura (apka -> widget nie jest obsługiwany).

Logowanie: przez Google wprost z widgetu (przeglądarka + lokalny adres zwrotny), albo kodem z apki
(Ustawienia -> Widżet na PC). Zapisuje się tylko token odświeżania (cloud.json, poza gitem).
Tylko biblioteka standardowa.
"""
import base64
import datetime as dt
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
import urllib.error
import urllib.request
from pathlib import Path

# publiczna konfiguracja aplikacji webowej Firebase (nie sekret: dostęp chronią reguły Firestore)
API_KEY = "AIzaSyDVxwDtuVRrGNF-HCjdahPcNOxiB61bCK4"
PROJECT = "life-timer-786a"
REFERER = "https://life-timer-786a.web.app/"
DOCS = f"projects/{PROJECT}/databases/(default)/documents"
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
MIN_GAP = 4          # s między próbami wysyłki
RETRY_GAP = 60       # s po błędzie


def to_ms(iso):
    """ISO -> milisekundy epoch (obcięte tak samo jak Date.parse w apce)."""
    if not isinstance(iso, str):
        return None
    try:
        d = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return (d.astimezone(dt.timezone.utc) - EPOCH) // dt.timedelta(milliseconds=1)


def safe(i):
    return str(i).replace("/", "_")


def fv(v):
    """Wartość -> format REST Firestore."""
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [fv(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: fv(x) for k, x in v.items()}}}
    return {"stringValue": str(v)}


def exts(raw):
    out = []
    for x in raw or []:
        out.append({"ts": to_ms(x.get("ts")) or 0, "add_s": int(x.get("add_s") or 0),
                    "at_elapsed_s": int(x.get("at_elapsed_s") or 0)})
    return out


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def read_jsonl(path):
    out = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def build_docs(store, cats, history, timelog, last_cat, since_ms):
    """Pliki widgetu -> {ścieżka dokumentu względem users/{uid}: pola} oraz ustawienia."""
    docs = {}
    cat_list = cats.get("categories", []) if isinstance(cats, dict) else []
    for i, c in enumerate(cat_list):
        docs[f"categories/{c['id']}"] = {"id": str(c["id"]), "name": str(c["name"]),
                                         "color": str(c["color"]), "order": i}
    ids = {c["id"] for c in cat_list}
    first = cat_list[0]["id"] if cat_list else "arch"
    cid = lambda c: c if c in ids else first

    open_ids = set()
    for t in store.get("tasks", []):
        if not t.get("id"):
            continue
        i = safe(t["id"])
        open_ids.add(i)
        docs[f"tasks/{i}"] = {
            "id": i, "title": str(t.get("title", "")), "cat": cid(t.get("cat")),
            "planned": int(t.get("planned") or 0),
            "original": int(t["original"]) if t.get("original") is not None else None,
            "ext": exts(t.get("ext")), "elapsed": float(t.get("elapsed") or 0),
            "runStart": to_ms(t.get("run_start")), "created": to_ms(t.get("created")) or int(time.time() * 1000),
            "firstStart": to_ms(t.get("first_start")), "status": "open", "finished": None, "note": "",
            "source": "calendar" if t.get("cal") else "manual",
        }

    for h in history:
        if not h.get("id") or safe(h["id"]) in open_ids:
            continue
        finished = to_ms(h.get("finished")) or to_ms(f"{h.get('date')}T12:00:00") or int(time.time() * 1000)
        if finished < since_ms:
            continue
        i = safe(h["id"])
        docs[f"tasks/{i}"] = {
            "id": i, "title": str(h.get("title", "")), "cat": cid(h.get("cat")),
            "planned": int(h.get("planned_final_s") or h.get("planned_original_s") or 0),
            "original": int(h["planned_original_s"]) if h.get("planned_original_s") is not None else None,
            "ext": exts(h.get("extensions")), "elapsed": float(h.get("actual_s") or 0), "runStart": None,
            "created": to_ms(h.get("created")) or to_ms(h.get("first_start")) or finished,
            "firstStart": to_ms(h.get("first_start")),
            "status": "deleted" if h.get("status") == "deleted" else "done", "finished": finished,
            "note": str(h.get("note") or ""), "source": str(h.get("source") or "manual"),
        }

    # timelog: start -> pause/done/cancel = zamknięty blok; id bloku jak przy imporcie w apce
    starts = {}
    for e in timelog:
        ts, i = to_ms(e.get("ts")), e.get("id")
        if ts is None or not i:
            continue
        if e.get("action") == "start":
            starts[i] = ts
        elif e.get("action") in ("pause", "done", "cancel") and i in starts:
            a = starts.pop(i)
            if ts > a and a >= since_ms:
                bid = f"{i}_{a}".replace("/", "_").replace(".", "_")
                docs[f"blocks/{bid}"] = {"id": bid, "task": safe(i), "start": a, "end": ts}

    cur = store.get("current")
    settings = {"current": safe(cur) if cur else None, "lastCat": cid(last_cat) if last_cat else None}
    return docs, settings, open_ids


class CloudError(Exception):
    pass


class CloudSync:
    def __init__(self, folder, paths, config_path):
        """paths: dict z kluczami store, history, cats, timelog (ścieżki plików widgetu)."""
        self.dir = Path(folder)
        self.paths = paths
        self.config_path = Path(config_path)
        self.auth_path = self.dir / "cloud.json"          # e-mail, uid, token odświeżania (poza gitem)
        self.state_path = self.dir / "cloud_state.json"   # skróty już wysłanych dokumentów
        self.auth = read_json(self.auth_path, {})
        self.state = read_json(self.state_path, {"hashes": {}, "open": []})
        self.lock = threading.Lock()
        self.busy = False
        self.dirty = False
        self.next_try = 0.0
        self.error = None
        self.last_ok = None
        self.id_token = None
        self.id_exp = 0.0

    # --- stan ---
    @property
    def connected(self):
        return bool(self.auth.get("refresh_token") and self.auth.get("uid"))

    @property
    def email(self):
        return self.auth.get("email", "")

    def status(self):
        if not self.connected:
            return "Chmura: nie połączono"
        if self.error:
            return f"Chmura: błąd, ponawiam ({self.error[:40]})"
        return "Chmura: wysyłanie włączone" + (f" ({self.last_ok:%H:%M:%S})" if self.last_ok else "")

    # --- HTTP ---
    def _post(self, url, body, token=None, form=False):
        data = body.encode() if form else json.dumps(body).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
                   "Referer": REFERER}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode())["error"]["message"]
            except Exception:
                msg = f"HTTP {e.code}"
            raise CloudError(msg) from None
        except (urllib.error.URLError, OSError) as e:
            raise CloudError("brak połączenia") from e

    def google_sign_in(self, creds_path, timeout=180):
        """Logowanie Google bez hasła: przeglądarka -> kod na 127.0.0.1 -> token Google -> logowanie Firebase.
        Używa klienta OAuth typu „aplikacja na komputer” z credentials.json. Rzuca CloudError."""
        cfg = read_json(creds_path, {})
        c = cfg.get("installed") or cfg.get("web") or {}
        if not c.get("client_id"):
            raise CloudError("brak credentials.json (klient OAuth typu Desktop)")
        got = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" in q or "error" in q:
                    got.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<meta charset=utf-8><body style='font-family:Georgia;text-align:center;margin-top:20vh'>"
                                 "<h2>Gotowe</h2><p>Możesz zamknąć tę kartę i wrócić do widgetu.</p>".encode())

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        srv.timeout = 1
        redirect = f"http://127.0.0.1:{srv.server_port}"
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = c.get("auth_uri", "https://accounts.google.com/o/oauth2/auth") + "?" + urllib.parse.urlencode({
            "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "state": state,
            "scope": "openid email profile", "code_challenge": challenge, "code_challenge_method": "S256",
            "prompt": "select_account"})
        try:
            webbrowser.open(url)
            end = time.time() + timeout
            while not got and time.time() < end:
                srv.handle_request()
        finally:
            srv.server_close()
        if not got:
            raise CloudError("nie dokończono logowania w przeglądarce")
        if got.get("error") or got.get("state") != state:
            raise CloudError("Google odrzucił logowanie: " + got.get("error", "zły stan"))
        tok = self._post(c.get("token_uri", "https://oauth2.googleapis.com/token"), urllib.parse.urlencode({
            "code": got["code"], "client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
            "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}), form=True)
        try:
            r = self._post(f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key={API_KEY}", {
                "postBody": "id_token=" + tok["id_token"] + "&providerId=google.com",
                "requestUri": "http://localhost", "returnSecureToken": True, "returnIdpCredential": True})
        except CloudError as e:
            raise CloudError(f"{e}. Dodaj klienta {c['client_id'][:12]}… do dozwolonych w Firebase: "
                             "Authentication > Sign-in method > Google") from None
        midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        self.auth = {"email": r.get("email", ""), "uid": r["localId"], "refresh_token": r["refreshToken"],
                     "since": midnight.isoformat()}
        self.id_token, self.id_exp = r["idToken"], time.time() + int(r.get("expiresIn", 3600)) - 60
        self.state = {"hashes": {}, "open": []}
        self.error = None
        self._save()
        self.poke(force=True)

    def connect(self, code):
        """Łączy konto kodem z apki (Ustawienia -> Widżet na PC). Rzuca CloudError."""
        try:
            info = json.loads(base64.b64decode(code.strip()).decode())
            uid, refresh = info["uid"], info["refreshToken"]
        except Exception:
            raise CloudError("zły kod, skopiuj go ponownie z apki") from None
        midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        self.auth = {"email": info.get("email", ""), "uid": uid, "refresh_token": refresh,
                     "since": midnight.isoformat()}
        self.id_token = None
        try:
            self._token()                      # sprawdza, że token działa
        except CloudError:
            self.auth = {}
            raise
        self.state = {"hashes": {}, "open": []}
        self.error = None
        self._save()
        self.poke(force=True)

    def sign_out(self):
        self.auth, self.state, self.id_token, self.error = {}, {"hashes": {}, "open": []}, None, None
        for p in (self.auth_path, self.state_path):
            p.unlink(missing_ok=True)

    def _token(self):
        if self.id_token and time.time() < self.id_exp:
            return self.id_token
        r = self._post(f"https://securetoken.googleapis.com/v1/token?key={API_KEY}",
                       f"grant_type=refresh_token&refresh_token={self.auth['refresh_token']}", form=True)
        self.id_token, self.id_exp = r["id_token"], time.time() + int(r.get("expires_in", 3600)) - 60
        if r.get("refresh_token") and r["refresh_token"] != self.auth["refresh_token"]:
            self.auth["refresh_token"] = r["refresh_token"]
            self._save()
        return self.id_token

    def _save(self):
        self.auth_path.write_text(json.dumps(self.auth), encoding="utf-8")

    # --- wysyłka ---
    def poke(self, force=False):
        """Wywoływane z pętli widgetu; samo ogranicza częstotliwość i działa w osobnym wątku."""
        if not self.connected:
            return
        now = time.time()
        with self.lock:
            if self.busy or now < self.next_try:
                self.dirty = self.dirty or force
                return
            if not force and now - getattr(self, "_last", 0) < MIN_GAP:
                return
            self.busy = True
            self._last = now
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self._sync()
            self.error = None
            self.last_ok = dt.datetime.now()
        except Exception as e:      # sieć, token, plik w trakcie zapisu: spróbuję ponownie
            self.error = str(e) or type(e).__name__
            self.next_try = time.time() + RETRY_GAP
        finally:
            with self.lock:
                self.busy = False

    def _sync(self):
        p = self.paths
        store = read_json(p["store"], None)
        cats = read_json(p["cats"], None)
        if store is None or cats is None:
            raise CloudError("plik w trakcie zapisu")
        cfg = read_json(self.config_path, {})
        since = to_ms(self.auth.get("since")) or 0
        docs, settings, open_ids = build_docs(store, cats, read_jsonl(p["history"]), read_jsonl(p["timelog"]),
                                              cfg.get("last_cat"), since)
        hashes = self.state["hashes"]
        wanted = {}
        for path, fields in docs.items():
            h = hashlib.sha1(json.dumps(fields, sort_keys=True).encode()).hexdigest()
            if hashes.get(path) != h:
                wanted[path] = (fields, h)
        sh = hashlib.sha1(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        send_settings = hashes.get("settings") != sh
        gone = [f"tasks/{i}" for i in self.state.get("open", []) if i not in open_ids and f"tasks/{i}" not in docs]
        if not wanted and not send_settings and not gone:
            return
        uid = self.auth["uid"]
        base = f"{DOCS}/users/{uid}"
        writes, done = [], {}
        for path, (fields, h) in wanted.items():
            writes.append({"update": {"name": f"{base}/{path}", "fields": {k: fv(v) for k, v in fields.items()}}})
            done[path] = h
        if send_settings:
            writes.append({"update": {"name": base, "fields": {k: fv(v) for k, v in settings.items()}},
                           "updateMask": {"fieldPaths": list(settings)}})
            done["settings"] = sh
        for path in gone:
            writes.append({"delete": f"{base}/{path}"})
        token = self._token()
        url = f"https://firestore.googleapis.com/v1/{DOCS}:commit"
        for i in range(0, len(writes), 400):
            self._post(url, {"writes": writes[i:i + 400]}, token=token)
        hashes.update(done)
        for path in gone:
            hashes.pop(path, None)
        self.state["open"] = sorted(open_ids)
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
