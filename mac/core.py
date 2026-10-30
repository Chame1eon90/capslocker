# -*- coding: utf-8 -*-
"""
WoW-переводчик чата: русский <-> английский.

Режимы: Авто (онлайн, без интернета -> офлайн), Только онлайн, Только офлайн.
• Горячая клавиша (по умолчанию F9): берёт фразу из чата игры, переводит
  RU -> EN и вставляет обратно.
• Входящие: скопируй английский текст (Ctrl+C) — перевод на русский
  появится в окне программы.
• В окне программы можно писать на любом из двух языков — направление
  определяется само.
"""
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

APP_NAME = "Капслокер"
_BASE = os.path.expanduser("~/Library/Application Support")
# сертификаты для HTTPS (в собранной программе для macOS их нет «из коробки»)
try:
    import ssl as _ssl
    import certifi as _certifi
    _CTX = _ssl.create_default_context(cafile=_certifi.where())
    _ssl._create_default_https_context = lambda *a, **k: _CTX
except Exception:
    pass
DATA_DIR = os.path.join(_BASE, "Capslocker")
OLD_DATA_DIR = os.path.join(_BASE, "WowTranslator")
OLD_CAPS_DIR = os.path.join(_BASE, "CapsFix")
# переезд со старого «WoW Переводчика»: модели и настройки не нужно качать заново
if not os.path.isdir(DATA_DIR) and os.path.isdir(OLD_DATA_DIR):
    try:
        os.rename(OLD_DATA_DIR, DATA_DIR)
    except Exception:
        pass
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "log.txt")

MODEL_INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
MODEL_FALLBACK_URLS = {
    ("ru", "en"): "https://argos-net.com/v1/translate-ru_en-1_9.argosmodel",
    ("en", "ru"): "https://argos-net.com/v1/translate-en_ru-1_9.argosmodel",
}

WOW_CHAT_LIMIT = 255

DEFAULT_SETTINGS = {
    "mode": "auto",          # auto | online | offline
    "hotkey": "f9",
    "auto_send": False,      # нажимать Enter после вставки
    "watch_clipboard": True, # переводить скопированный английский текст
    "keep_on_top": True,
    "offline_engine": "fast",
    "unload_minutes": 10,
    "spell_punct": False,
    "punct_only": "",
    "online_service": "google",
    "online_fallback": True,
}

os.makedirs(DATA_DIR, exist_ok=True)

CYR = re.compile(r"[А-Яа-яЁё]")
LAT = re.compile(r"[A-Za-z]")


LOG_MAX_BYTES = 512 * 1024   # журнал не больше ~0,5 МБ: старая часть уходит в log.old.txt


def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            old = os.path.join(DATA_DIR, "log.old.txt")
            if os.path.exists(old):
                os.remove(old)
            os.replace(LOG_FILE, old)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        log("save_settings failed: " + traceback.format_exc())


def detect_direction(text):
    """Есть кириллица -> RU->EN, иначе есть латиница -> EN->RU."""
    if CYR.search(text):
        return ("ru", "en")
    if LAT.search(text):
        return ("en", "ru")
    return None


# ---------------------------------------------------------------- переводчики

UA = "Mozilla/5.0"
UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def _http(url, data=None, headers=None, timeout=4, method=None, opener=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    op = opener.open if opener else urllib.request.urlopen
    with op(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


class MultiMethodService:
    """Сервис с несколькими способами доступа: пробуем по очереди, удачный запоминаем."""
    name = "?"

    def methods(self):
        return []

    def __init__(self):
        self._good = None
        self.last_report = []

    def translate(self, text, src, tgt, timeout=4):
        ms = self.methods()
        if self._good is not None:
            ms = [m for m in ms if m.__name__ == self._good] + [m for m in ms if m.__name__ != self._good]
        errors = []
        for m in ms:
            try:
                out = m(text, src, tgt, timeout)
                if out and out.strip():
                    if self._good != m.__name__:
                        log(f"{self.name}: работает способ {m.__name__}")
                    self._good = m.__name__
                    self.last_report = errors + [f"{m.__name__}: ok"]
                    return out
                errors.append(f"{m.__name__}: пустой ответ")
            except Exception as e:
                errors.append(f"{m.__name__}: {e}")
        self._good = None
        self.last_report = errors
        log(f"{self.name} все способы: {errors}")
        raise RuntimeError(errors[-1] if errors else "нет способов")


class GoogleService(MultiMethodService):
    name = "Google"

    def methods(self):
        return [self.gtx_api, self.dict_chrome, self.gtx_site]

    @staticmethod
    def _gtx(host, text, src, tgt, timeout):
        q = urllib.parse.urlencode({"client": "gtx", "sl": src, "tl": tgt, "dt": "t", "q": text})
        data = json.loads(_http(f"https://{host}/translate_a/single?" + q, timeout=timeout))
        return "".join(part[0] for part in data[0] if part and part[0])

    def gtx_api(self, text, src, tgt, timeout):
        return self._gtx("translate.googleapis.com", text, src, tgt, timeout)

    def gtx_site(self, text, src, tgt, timeout):
        return self._gtx("translate.google.com", text, src, tgt, timeout)

    def dict_chrome(self, text, src, tgt, timeout):
        q = urllib.parse.urlencode({"client": "dict-chrome-ex", "sl": src, "tl": tgt, "q": text})
        data = json.loads(_http("https://clients5.google.com/translate_a/t?" + q, timeout=timeout))
        first = data[0] if isinstance(data, list) else data
        if isinstance(first, list):
            first = first[0]
        if isinstance(data, dict):
            return "".join(s.get("trans", "") for s in data.get("sentences", []))
        return first


class MicrosoftService(MultiMethodService):
    """Переводчик Microsoft / Bing."""
    name = "Microsoft"

    def __init__(self):
        super().__init__()
        self._token = None
        self._token_time = 0
        self._bing = None      # (opener, ig, iid, key, token, time)

    def methods(self):
        return [self.bing_web, self.edge_api]

    def edge_api(self, text, src, tgt, timeout):
        if not self._token or time.time() - self._token_time > 480:
            self._token = _http("https://edge.microsoft.com/translate/auth", timeout=timeout,
                                headers={"User-Agent": UA_BROWSER + " Edg/130.0"}).strip()
            self._token_time = time.time()
        url = ("https://api-edge.cognitive.microsofttranslator.com/translate?"
               + urllib.parse.urlencode({"from": src, "to": tgt, "api-version": "3.0"}))
        try:
            raw = _http(url, data=json.dumps([{"Text": text}]).encode("utf-8"), timeout=timeout,
                        headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._token = None
            raise
        return json.loads(raw)[0]["translations"][0]["text"]

    def _bing_session(self, timeout, force=False):
        if self._bing and not force and time.time() - self._bing[5] < 1800:
            return self._bing
        import http.cookiejar
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        html = _http("https://www.bing.com/translator", timeout=timeout + 3, opener=opener,
                     headers={"User-Agent": UA_BROWSER, "Accept-Language": "ru,en;q=0.8"})
        ig = re.search(r'IG:"([^"]+)"', html)
        iid = re.search(r'data-iid="([^"]+)"', html)
        helper = re.search(r"params_AbusePreventionHelper\s*=\s*\[([^\]]+)\]", html)
        if not (ig and helper):
            raise RuntimeError("Bing: не нашёл параметры страницы")
        parts = [p.strip().strip('"') for p in helper.group(1).split(",")]
        self._bing = (opener, ig.group(1), iid.group(1) if iid else "translator.5023", parts[0], parts[1], time.time())
        return self._bing

    def bing_web(self, text, src, tgt, timeout):
        for attempt in (0, 1):
            opener, ig, iid, key, token, _ = self._bing_session(timeout, force=attempt == 1)
            url = f"https://www.bing.com/ttranslatev3?isVertical=1&&IG={ig}&IID={iid}"
            body = urllib.parse.urlencode({"fromLang": src, "to": tgt, "text": text, "token": token,
                                           "key": key, "tryFetchingGenderDebiasedTranslations": "true"}).encode()
            raw = _http(url, data=body, timeout=timeout, opener=opener,
                        headers={"User-Agent": UA_BROWSER, "Content-Type": "application/x-www-form-urlencoded",
                                 "Referer": "https://www.bing.com/translator"})
            data = json.loads(raw) if raw.strip() else None
            if isinstance(data, list) and data and data[0].get("translations"):
                return data[0]["translations"][0]["text"]
        raise RuntimeError(f"Bing: неожиданный ответ {str(data)[:80]}")


class YandexService(MultiMethodService):
    """Яндекс Переводчик (неофициально, может перестать работать)."""
    name = "Яндекс"

    def __init__(self):
        super().__init__()
        import uuid
        self._ucid = uuid.uuid4().hex
        self._sid = None
        self._sid_time = 0

    def methods(self):
        return [self.android_api, self.widget_api]

    def android_api(self, text, src, tgt, timeout):
        url = ("https://translate.yandex.net/api/v1/tr.json/translate?"
               + urllib.parse.urlencode({"ucid": self._ucid, "srv": "android", "format": "text"}))
        raw = _http(url, data=urllib.parse.urlencode({"text": text, "lang": f"{src}-{tgt}"}).encode("utf-8"),
                    timeout=timeout, headers={"User-Agent": "ru.yandex.translate/24.10.1",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        data = json.loads(raw)
        if data.get("code") == 200 and data.get("text"):
            return "".join(data["text"])
        raise RuntimeError(f"код {data.get('code')}")

    def widget_api(self, text, src, tgt, timeout):
        if not self._sid or time.time() - self._sid_time > 3000:
            js = _http("https://translate.yandex.net/website-widget/v1/widget.js"
                       "?widgetId=ytWidget&pageLang=ru&widgetTheme=light&autoMode=false", timeout=timeout)
            m = re.search(r"sid\s*:\s*'([0-9a-f.]+)'", js)
            if not m:
                raise RuntimeError("Яндекс не выдал ключ сессии")
            self._sid, self._sid_time = m.group(1), time.time()
        last = None
        for variant in (self._sid, ".".join(p[::-1] for p in self._sid.split("."))):
            url = ("https://translate.yandex.net/api/v1/tr.json/translate?"
                   + urllib.parse.urlencode({"id": variant + "-0-0", "srv": "tr-text",
                                             "lang": f"{src}-{tgt}", "reason": "auto", "format": "text"}))
            try:
                raw = _http(url, data=urllib.parse.urlencode({"text": text}).encode("utf-8"), timeout=timeout,
                            headers={"Content-Type": "application/x-www-form-urlencoded",
                                     "Referer": "https://translate.yandex.ru/"})
                data = json.loads(raw)
                if data.get("code") == 200 and data.get("text"):
                    return "".join(data["text"])
                last = RuntimeError(f"код {data.get('code')}")
            except Exception as e:
                last = e
        self._sid = None
        raise last


class MyMemoryService:
    name = "MyMemory"

    def translate(self, text, src, tgt, timeout=4):
        q = urllib.parse.urlencode({"q": text, "langpair": f"{src}|{tgt}"})
        data = json.loads(_http("https://api.mymemory.translated.net/get?" + q, timeout=timeout))
        out = data.get("responseData", {}).get("translatedText")
        if not out or data.get("responseStatus") not in (200, "200"):
            raise RuntimeError(f"MyMemory: {data.get('responseDetails') or data.get('responseStatus')}")
        return out


class OnlineTranslator:
    ORDER = ["google", "microsoft", "yandex", "mymemory"]

    def __init__(self):
        self.services = {
            "google": GoogleService(),
            "microsoft": MicrosoftService(),
            "yandex": YandexService(),
            "mymemory": MyMemoryService(),
        }
        self.preferred = "google"
        self.fallback = True   # если выбранный не ответил — пробовать остальные

    def _chain(self):
        chain = [self.preferred] + ([k for k in self.ORDER if k != self.preferred] if self.fallback else [])
        return [k for k in chain if k in self.services]

    def translate(self, text, src, tgt, timeout=4):
        """Возвращает (перевод, имя сервиса)."""
        errors = []
        for key in self._chain():
            svc = self.services[key]
            try:
                out = svc.translate(text, src, tgt, timeout=timeout)
                if out and out.strip():
                    return out.strip(), svc.name
                errors.append(f"{svc.name}: пустой ответ")
            except Exception as e:
                log(f"online {svc.name} failed: {e!r}")
                errors.append(f"{svc.name}: {e}")
        raise RuntimeError("Онлайн-сервисы не ответили (" + "; ".join(errors)[:200] + ")")

    def test_all(self, timeout=6):
        """Проверка всех сервисов: {key: (ok, текст/ошибка, секунды)}."""
        res = {}
        for key in self.ORDER:
            svc = self.services[key]
            t = time.time()
            try:
                out = svc.translate("Привет, нужен танк в подземелье", "ru", "en", timeout=timeout)
                res[key] = (True, out, time.time() - t)
            except Exception as e:
                rep = getattr(svc, "last_report", None)
                res[key] = (False, ("; ".join(rep) if rep else str(e))[:200], time.time() - t)
            log(f"test {svc.name}: {res[key]}")
        return res


class OfflineModel:
    """Одна офлайн-модель для одного направления (например, ru->en)."""

    def __init__(self, src, tgt):
        self.src, self.tgt = src, tgt
        self.dir = os.path.join(DATA_DIR, f"model_{src}_{tgt}")
        self._translator = None
        self._sp = None
        self._lock = threading.Lock()

    @staticmethod
    def _find(root, name, want_dir=False):
        for dirpath, dirnames, filenames in os.walk(root):
            if want_dir and name in dirnames:
                return os.path.join(dirpath, name)
            if not want_dir and name in filenames:
                return os.path.join(dirpath, name)
        return None

    def is_installed(self):
        return bool(os.path.isdir(self.dir)
                    and self._find(self.dir, "model", want_dir=True)
                    and self._find(self.dir, "sentencepiece.model"))

    def candidate_urls(self):
        name = f"translate-{self.src}_{self.tgt}-1_9.argosmodel"
        urls = []
        try:
            with urllib.request.urlopen(MODEL_INDEX_URL, timeout=15) as r:
                for p in json.loads(r.read().decode("utf-8")):
                    if p.get("from_code") == self.src and p.get("to_code") == self.tgt:
                        urls += p.get("links") or []
                        name = os.path.basename(urls[0]) if urls else name
                        break
        except Exception:
            log("index fetch failed")
        urls += [
            MODEL_FALLBACK_URLS[(self.src, self.tgt)],
            f"http://argos-net.com/v1/{name}",
            f"https://data.argosopentech.com/argospm/v1/{name}",
        ]
        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _fetch(self, url, dest, progress, label):
        """Качает с докачкой: обрывы и зависания не страшны."""
        total = 0
        stalls = 0
        while True:
            done = os.path.getsize(dest) if os.path.exists(dest) else 0
            if total and done >= total:
                return
            headers = {"User-Agent": "Mozilla/5.0"}
            if done:
                headers["Range"] = f"bytes={done}-"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=25) as r:
                    if done and r.status != 206:
                        done = 0
                        open(dest, "wb").close()
                    length = int(r.headers.get("Content-Length") or 0)
                    total = done + length if length else total
                    with open(dest, "ab") as f:
                        got_any = False
                        while True:
                            chunk = r.read(1024 * 256)
                            if not chunk:
                                break
                            got_any = True
                            f.write(chunk)
                            done += len(chunk)
                            mb = done // 1048576
                            if total:
                                progress(int(done * 100 / total), f"{label}: {mb} из {total // 1048576} МБ")
                            else:
                                progress(-1, f"{label}: {mb} МБ")
                    if got_any:
                        stalls = 0
                    if not total or done >= total:
                        return
            except urllib.error.HTTPError as e:
                if e.code == 416 and done:
                    return
                raise
            except Exception as e:
                stalls += 1
                log(f"download stall {stalls} at {done} bytes from {url}: {e!r}")
                if done == 0 and stalls >= 2:
                    raise RuntimeError("сервер не отвечает")
                if stalls >= 6:
                    raise
                progress(-1, f"{label}: связь оборвалась, продолжаю… ({done // 1048576} МБ)")
                time.sleep(2)

    def install_from_file(self, path):
        if os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)
        with zipfile.ZipFile(path) as z:
            z.extractall(self.dir)
        if not self.is_installed():
            shutil.rmtree(self.dir, ignore_errors=True)
            raise RuntimeError("Это не файл офлайн-модели.")
        self._translator = None

    def download(self, progress=lambda pct, msg: None, label=""):
        tmp_zip = os.path.join(DATA_DIR, f"download_{self.src}_{self.tgt}.zip")
        errors = []
        for url in self.candidate_urls():
            progress(-1, f"{label}: подключаюсь к {urllib.parse.urlparse(url).netloc}…")
            fetched = False
            try:
                self._fetch(url, tmp_zip, progress, label)
                fetched = True
                progress(-1, f"{label}: распаковываю…")
                self.install_from_file(tmp_zip)
                os.remove(tmp_zip)
                return
            except Exception as e:
                log(f"download from {url} failed: {e!r}")
                errors.append(f"{urllib.parse.urlparse(url).netloc}: {e}")
                # битый или пустой файл — удаляем, чтобы не докачивать мусор
                if os.path.exists(tmp_zip) and (fetched or os.path.getsize(tmp_zip) == 0):
                    os.remove(tmp_zip)
        raise RuntimeError("не получилось ни с одного сервера:\n" + "\n".join(errors))

    def _load(self):
        if self._translator is not None:
            return
        import ctranslate2
        import sentencepiece
        model_path = self._find(self.dir, "model", want_dir=True)
        sp_path = self._find(self.dir, "sentencepiece.model")
        threads = max(1, min(4, (os.cpu_count() or 2)))
        self._translator = ctranslate2.Translator(model_path, device="cpu", inter_threads=1, intra_threads=threads)
        self._sp = sentencepiece.SentencePieceProcessor(model_file=sp_path)

    def warm_up(self):
        if self.is_installed():
            with self._lock:
                self._load()

    def loaded(self):
        return self._translator is not None

    def unload(self):
        with self._lock:
            self._translator = None
            self._sp = None

    def translate(self, text):
        if not self.is_installed():
            raise RuntimeError("Офлайн-модель не скачана. Нажми «Скачать офлайн-модели».")
        with self._lock:
            self._load()
            sentences = [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s]
            tokens = [self._sp.encode(s, out_type=str) for s in sentences]
            results = self._translator.translate_batch(tokens, beam_size=4, max_decoding_length=256)
            out = [self._sp.decode(r.hypotheses[0]) for r in results]
        return " ".join(o.replace("▁", " ").strip() for o in out)


# ---------------------------------------------------------------- логика

SLASH_CMD = re.compile(r"^(/\S+\s+)(.*)$", re.S)
WHISPER_CMD = re.compile(r"^(/(?:w|whisper|t|tell|ш)\s+\S+\s+)(.*)$", re.S | re.I)


# ---------------------------------------------------------------- Tencent HY-MT

HYMT_FILE = "HY-MT1.5-1.8B-Q4_K_M.gguf"
HYMT_URLS = [
    f"https://huggingface.co/tencent/HY-MT1.5-1.8B-GGUF/resolve/main/{HYMT_FILE}",
    f"https://hf-mirror.com/tencent/HY-MT1.5-1.8B-GGUF/resolve/main/{HYMT_FILE}",
    f"https://modelscope.cn/models/Tencent-Hunyuan/HY-MT1.5-1.8B-GGUF/resolve/master/{HYMT_FILE}",
]
LANG_NAMES = {"en": "English", "ru": "Russian"}


def _runtime_dir():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "llama")


def _kill_on_close_job():
    """Windows Job Object: если программа закроется или упадёт, модель тоже выгрузится."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("a", ctypes.c_longlong), ("b", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                        ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO_COUNTERS), ("p", ctypes.c_size_t),
                        ("j", ctypes.c_size_t), ("pp", ctypes.c_size_t), ("pj", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        return job
    except Exception:
        return None


class HyMTModel:
    """Качественная офлайн-модель Tencent HY-MT1.5 (1.8B), переводит в обе стороны."""

    def __init__(self):
        self.dir = os.path.join(DATA_DIR, "hymt")
        self.path = os.path.join(self.dir, HYMT_FILE)
        self.proc = None
        self.port = None
        self._lock = threading.Lock()
        self._job = None

    def is_installed(self):
        return os.path.isfile(self.path) and os.path.getsize(self.path) > 500 * 1048576

    def install_from_file(self, path, progress=lambda pct, msg: None):
        if not path.lower().endswith(".gguf"):
            raise RuntimeError("Нужен файл .gguf")
        if os.path.getsize(path) < 500 * 1048576:
            raise RuntimeError("Файл слишком маленький — похоже, скачался не до конца.")
        self.stop()
        os.makedirs(self.dir, exist_ok=True)
        if os.path.abspath(path) != os.path.abspath(self.path):
            total = os.path.getsize(path)
            done = 0
            tmp = self.path + ".part"
            with open(path, "rb") as src, open(tmp, "wb") as dst:
                while True:
                    buf = src.read(8 * 1048576)
                    if not buf:
                        break
                    dst.write(buf)
                    done += len(buf)
                    progress(int(done * 100 / total), f"Копирую модель: {done // 1048576} из {total // 1048576} МБ")
            os.replace(tmp, self.path)

    def download(self, progress=lambda pct, msg: None, label="Модель HY-MT"):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".part"
        errors = []
        for url in HYMT_URLS:
            progress(-1, f"{label}: подключаюсь к {urllib.parse.urlparse(url).netloc}…")
            try:
                OfflineModel._fetch(None, url, tmp, progress, label)
                if os.path.getsize(tmp) < 500 * 1048576:
                    raise RuntimeError("скачался не тот файл")
                with open(tmp, "rb") as f:
                    if f.read(4) != b"GGUF":
                        raise RuntimeError("скачался не тот файл")
                self.stop()
                os.replace(tmp, self.path)
                return
            except Exception as e:
                log(f"hymt download from {url} failed: {e!r}")
                errors.append(f"{urllib.parse.urlparse(url).netloc}: {e}")
                try:
                    if os.path.exists(tmp):
                        with open(tmp, "rb") as f:
                            head = f.read(4)
                        if head != b"GGUF":
                            os.remove(tmp)
                except Exception:
                    pass
        raise RuntimeError("не получилось ни с одного сервера:\n" + "\n".join(errors))

    # ---- локальный сервер llama.cpp
    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _health(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                return b"ok" in r.read()
        except Exception:
            return False

    def start(self):
        with self._lock:
            if self._alive() and self._health():
                return
            if not self.is_installed():
                raise RuntimeError("Качественная модель не скачана.")
            self.stop()
            import socket
            import subprocess
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
            s.close()
            exe = os.path.join(_runtime_dir(), "llama-server.exe")
            threads = max(2, (os.cpu_count() or 4) // 2)
            args = [exe, "-m", self.path, "--host", "127.0.0.1", "--port", str(self.port),
                    "-c", "2048", "-t", str(threads), "-ngl", "0", "--parallel", "1"]
            flags = 0x08000000 | 0x00004000  # без окна, пониженный приоритет — игра не тормозит
            logf = open(os.path.join(DATA_DIR, "hymt_server.log"), "w", encoding="utf-8", errors="replace")
            self.proc = subprocess.Popen(args, cwd=_runtime_dir(), stdout=logf, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, creationflags=flags)
            try:
                if self._job is None:
                    self._job = _kill_on_close_job()
                if self._job:
                    import ctypes
                    h = ctypes.windll.kernel32.OpenProcess(0x1F0FFF, False, self.proc.pid)
                    ctypes.windll.kernel32.AssignProcessToJobObject(self._job, h)
            except Exception:
                pass
            deadline = time.time() + 120
            while time.time() < deadline:
                if not self._alive():
                    raise RuntimeError("Модель не запустилась (подробности в hymt_server.log).")
                if self._health():
                    return
                time.sleep(0.5)
            self.stop()
            raise RuntimeError("Модель слишком долго загружается.")

    def stop(self):
        p = self.proc
        self.proc = None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def warm_up(self):
        if self.is_installed():
            self.start()

    def loaded(self):
        return self._alive()

    def unload(self):
        self.stop()

    def translate(self, text, src, tgt):
        self.start()
        prompt = (f"Translate the following segment into {LANG_NAMES[tgt]}, "
                  f"without additional explanation.\n\n{text}")
        body = json.dumps({
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7, "top_k": 20, "top_p": 0.6, "repeat_penalty": 1.05,
            "max_tokens": 300, "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = data["choices"][0]["message"]["content"].strip()
        return out.strip('"').strip()


# ---------------------------------------------------------------- проверка ошибок

class SpellChecker:
    """Исправление опечаток: Яндекс.Спеллер (основной), LanguageTool (запасной). Нужен интернет."""

    def _yandex(self, text, timeout):
        body = urllib.parse.urlencode({"text": text, "lang": "ru,en", "options": 2 | 4 | 512}).encode("utf-8")
        raw = _http("https://speller.yandex.net/services/spellservice.json/checkText", data=body,
                    timeout=timeout, headers={"Content-Type": "application/x-www-form-urlencoded"})
        fixes = [(e["pos"], e["len"], e["s"][0]) for e in json.loads(raw) if e.get("s")]
        return fixes

    PUNCT_CATEGORIES = {"PUNCTUATION", "TYPOGRAPHY"}

    def _languagetool(self, text, timeout, spelling=True, punctuation=False):
        lang = "ru-RU" if CYR.search(text) else "en-US"
        body = urllib.parse.urlencode({"text": text, "language": lang, "level": "default"}).encode("utf-8")
        raw = _http("https://api.languagetool.org/v2/check", data=body, timeout=timeout + 2,
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        fixes = []
        for m in json.loads(raw).get("matches", []):
            rule = m.get("rule", {})
            kind = (rule.get("issueType") or "").lower()
            cat = (rule.get("category", {}).get("id") or "").upper()
            reps = m.get("replacements") or []
            if not reps:
                continue
            # только знаки препинания и пробелы; заглавные буквы и стиль не трогаем
            is_punct = cat in self.PUNCT_CATEGORIES or "COMMA" in (rule.get("id") or "")
            if cat == "CASING":
                continue
            if (punctuation and is_punct) or (spelling and not is_punct and kind in ("misspelling", "grammar")):
                fixes.append((m["offset"], m["length"], reps[0]["value"]))
        return fixes

    @staticmethod
    def _apply(text, fixes):
        out, n, last = text, 0, None
        for pos, length, rep in sorted(fixes, key=lambda f: f[0], reverse=True):
            if last is not None and pos + length > last:
                continue  # пересекающиеся правки пропускаем
            if out[pos:pos + length] != rep:
                out = out[:pos] + rep + out[pos + length:]
                n += 1
            last = pos
        return out, n

    def check(self, text, timeout=4, punctuation=False):
        """Возвращает (исправленный текст, сколько исправлений, сервис).
        punctuation=True — после орфографии ещё и знаки препинания (LanguageTool)."""
        errors = []
        fixed, n, name = None, 0, ""
        try:
            fixed, n = self._apply(text, self._yandex(text, timeout))
            name = "Яндекс.Спеллер"
        except Exception as e:
            log(f"spell Яндекс.Спеллер failed: {e!r}")
            errors.append(f"Яндекс.Спеллер: {e}")
        if fixed is None or punctuation:
            src = text if fixed is None else fixed
            try:
                fx = self._languagetool(src, timeout, spelling=fixed is None, punctuation=punctuation)
                fixed, n2 = self._apply(src, fx)
                n += n2
                name = "LanguageTool" if not name else name + " + LanguageTool"
            except Exception as e:
                log(f"spell LanguageTool failed: {e!r}")
                errors.append(f"LanguageTool: {e}")
                if fixed is None:
                    fixed = None
        if fixed is None:
            raise RuntimeError("Проверка ошибок недоступна (" + "; ".join(errors)[:160] + ")")
        return fixed, n, name


# ---------------------------------------------------------------- офлайн-проверка ошибок

SAGE_URL = "https://github.com/Chame1eon90/capslocker/releases/download/models/sage-ru-ct2-int8.zip"
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:[-'][A-Za-zА-Яа-яЁё]+)*")


def _match_case(src, word):
    if src.isupper() and len(src) > 1:
        return word.upper()
    if src[:1].isupper():
        return word[:1].upper() + word[1:]
    return word.lower() if src.islower() else word


class OfflineSpeller:
    """Без интернета: русский — модель SAGE (Сбер, по желанию скачивается),
    без неё и для английского — словари pyspellchecker (встроены)."""

    def __init__(self):
        self.dir = os.path.join(DATA_DIR, "sage_ru")
        self._lock = threading.Lock()
        self._tr = None
        self._tok = None
        self._prefix = ""
        self._dicts = {}

    # ---- модель SAGE
    def sage_installed(self):
        return all(os.path.exists(os.path.join(self.dir, f)) for f in ("model.bin", "tokenizer.json"))

    def install_from_file(self, path):
        tmp = self.dir + "_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        src = tmp
        for root, dirs, files in os.walk(tmp):
            if "model.bin" in files:
                src = root
                break
        if not os.path.exists(os.path.join(src, "model.bin")):
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError("Это не файл модели проверки ошибок.")
        self.unload()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.move(src, self.dir)
        shutil.rmtree(tmp, ignore_errors=True)

    def download(self, progress=lambda pct, msg: None, label="Модель SAGE"):
        tmp = os.path.join(DATA_DIR, "download_sage.zip")
        progress(-1, f"{label}: подключаюсь к github.com…")
        OfflineModel._fetch(None, SAGE_URL, tmp, progress, label)
        progress(-1, f"{label}: распаковываю…")
        try:
            self.install_from_file(tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def loaded(self):
        return self._tr is not None or bool(self._dicts)

    def unload(self):
        with self._lock:
            self._tr = None
            self._tok = None
            self._dicts = {}

    def _load_sage(self):
        if self._tr is not None:
            return
        import ctranslate2
        import tokenizers
        try:
            cfg = json.load(open(os.path.join(self.dir, "sage_config.json"), encoding="utf-8"))
            self._prefix = cfg.get("prefix", "")
        except Exception:
            self._prefix = ""
        threads = max(1, min(4, (os.cpu_count() or 2)))
        self._tr = ctranslate2.Translator(self.dir, device="cpu", compute_type="int8", intra_threads=threads)
        self._tok = tokenizers.Tokenizer.from_file(os.path.join(self.dir, "tokenizer.json"))

    def _sage(self, text):
        toks = self._tok.encode(self._prefix + text).tokens
        res = self._tr.translate_batch([toks], beam_size=1, max_decoding_length=int(len(toks) * 1.5) + 8)
        ids = [self._tok.token_to_id(t) for t in res[0].hypotheses[0]]
        return self._tok.decode([i for i in ids if i is not None], skip_special_tokens=True).strip()

    # ---- словари
    def _dict(self, lang):
        if lang not in self._dicts:
            from spellchecker import SpellChecker as _SC
            self._dicts[lang] = _SC(language=lang, distance=2)
        return self._dicts[lang]

    def _dict_fix(self, text):
        out, n, pos = [], 0, 0
        for m in WORD_RE.finditer(text):
            w = m.group(0)
            lang = "ru" if CYR.search(w) else "en"
            sc = self._dict(lang)
            fixed = w
            if len(w) > 2 and not sc.known([w.lower()]):
                cand = sc.correction(w.lower())
                if cand and cand != w.lower():
                    fixed = _match_case(w, cand)
            out.append(text[pos:m.start()])
            out.append(fixed)
            n += fixed != w
            pos = m.end()
        out.append(text[pos:])
        return "".join(out), n

    @staticmethod
    def _merge(orig, model_out, punctuation):
        """Берём из ответа модели исправленные слова (и знаки — если нужна пунктуация),
        но регистр букв оставляем как у пользователя."""
        import difflib
        a = re.findall(r"\w+|[^\w\s]+|\s+", orig)
        b = re.findall(r"\w+|[^\w\s]+|\s+", model_out)
        norm = lambda t: t.lower() if t.strip() else " "
        sm = difflib.SequenceMatcher(a=[norm(t) for t in a], b=[norm(t) for t in b], autojunk=False)
        res, n = [], 0
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                res.extend(a[i1:i2])
                continue
            seg_a, seg_b = a[i1:i2], b[j1:j2]
            words_a = [t for t in seg_a if re.match(r"\w", t)]
            words_b = [t for t in seg_b if re.match(r"\w", t)]
            if punctuation:
                fixed = []
                wi = 0
                for t in seg_b:
                    if re.match(r"\w", t):
                        src = words_a[wi] if wi < len(words_a) else t
                        fixed.append(_match_case(src, t) if len(words_a) == len(words_b) else t)
                        wi += 1
                    else:
                        fixed.append(t)
                res.extend(fixed)
                n += 1
            elif len(words_a) == len(words_b) and words_a:
                # только слова: знаки и пробелы оставляем как были
                wi = 0
                for t in seg_a:
                    if re.match(r"\w", t):
                        res.append(_match_case(t, words_b[wi]))
                        wi += 1
                    else:
                        res.append(t)
                n += sum(1 for x, y in zip(words_a, words_b) if x.lower() != y.lower())
            else:
                res.extend(seg_a)
        return "".join(res), n

    def check(self, text, punctuation=False):
        with self._lock:
            if CYR.search(text) and self.sage_installed():
                self._load_sage()
                out = self._sage(text)
                fixed, n = self._merge(text, out, punctuation)
                # точку в конце фразы не добавляем — в чатах её обычно не ставят
                if fixed.endswith(".") and not text.rstrip().endswith((".", "!", "?", "…")):
                    fixed = fixed[:-1]
                # английские слова внутри русской фразы — словарём
                if re.search(r"[A-Za-z]", fixed):
                    fixed, n2 = self._dict_fix(fixed)
                    n += n2
                return fixed, n, "офлайн SAGE"
            fixed, n = self._dict_fix(text)
            return fixed, n, "офлайн словарь"


class Engine:
    def __init__(self):
        self.online = OnlineTranslator()
        self.speller = SpellChecker()
        self.offline_speller = OfflineSpeller()
        self.offline = {
            ("ru", "en"): OfflineModel("ru", "en"),
            ("en", "ru"): OfflineModel("en", "ru"),
        }
        self.hymt = HyMTModel()
        self.mode = "auto"
        self.offline_engine = "fast"   # fast = Argos, quality = Tencent HY-MT
        self.last_offline_use = time.time()

    def fast_ready(self):
        return all(m.is_installed() for m in self.offline.values())

    def offline_ready(self):
        return self.hymt.is_installed() if self.offline_engine == "quality" else self.fast_ready()

    def models_loaded(self):
        return (self.hymt.loaded() or any(m.loaded() for m in self.offline.values())
                or self.offline_speller.loaded())

    def unload_models(self):
        """Выгрузить все офлайн-модели из памяти. Возвращает True, если что-то было загружено."""
        was = self.models_loaded()
        for m in self.offline.values():
            m.unload()
        self.hymt.unload()
        self.offline_speller.unload()
        if was:
            import gc
            gc.collect()
        return was

    def _offline(self, text, src, tgt):
        self.last_offline_use = time.time()
        if self.offline_engine == "quality":
            if not self.hymt.is_installed():
                raise RuntimeError("Качественная модель не скачана.")
            out = self.hymt.translate(text, src, tgt)
            self.last_offline_use = time.time()
            return out, "офлайн HY-MT"
        model = self.offline[(src, tgt)]
        if not model.is_installed():
            raise RuntimeError("Быстрые офлайн-модели не скачаны.")
        out = model.translate(text)
        self.last_offline_use = time.time()
        return out, "офлайн"

    def _translate_core(self, text, src, tgt):
        if self.mode == "online":
            return self.online.translate(text, src, tgt)
        if self.mode == "offline":
            return self._offline(text, src, tgt)
        try:
            return self.online.translate(text, src, tgt, timeout=3)
        except Exception as e:
            log("online failed, fallback offline: %r" % e)
            if not self.offline_ready():
                raise RuntimeError("Нет интернета, а офлайн-модель не скачана.")
            result, how = self._offline(text, src, tgt)
            return result, how + " (нет сети)"

    def spell(self, text, punctuation=False):
        """Исправить опечатки (и, если нужно, пунктуацию). Команды чата «/g », «/w Ник » не трогаем."""
        text = text.strip()
        if not text:
            return text, 0, ""
        prefix, body = "", text
        m = WHISPER_CMD.match(text) or SLASH_CMD.match(text)
        if m:
            prefix, body = m.group(1), m.group(2)
        if self.mode == "offline":
            fixed, n, how = self.offline_speller.check(body, punctuation)
        else:
            # русскую пунктуацию лучше всего правит SAGE (если скачана): LanguageTool
            # почти не знает русских правил про запятые (обращения, вводные слова)
            sage_punct = punctuation and bool(CYR.search(body)) and self.offline_speller.sage_installed()
            try:
                fixed, n, how = self.speller.check(body, punctuation=punctuation and not sage_punct, timeout=3)
                if sage_punct:
                    try:
                        fixed, n2, _ = self.offline_speller.check(fixed, True)
                        n += n2
                        how += " + SAGE"
                        self.last_offline_use = time.time()
                    except Exception as e2:
                        log(f"sage punct failed: {e2!r}")
            except Exception as e:
                if self.mode == "online":
                    raise
                log(f"spell online failed, offline: {e!r}")
                fixed, n, how = self.offline_speller.check(body, punctuation)
                how += " (нет сети)"
        self.last_offline_use = time.time() if how.startswith("офлайн") else self.last_offline_use
        return prefix + fixed, n, how

    def translate(self, text, direction=None):
        """direction: ('ru','en'), ('en','ru') или None = определить само."""
        text = text.strip()
        if not text:
            return "", "пусто"
        prefix, body = "", text
        m = WHISPER_CMD.match(text) or SLASH_CMD.match(text)
        if m:
            prefix, body = m.group(1), m.group(2)
        auto = detect_direction(body)
        if direction is None:
            direction = auto
        if direction is None or auto != direction:
            return text, "без перевода"
        result, how = self._translate_core(body, *direction)
        return prefix + result, how


