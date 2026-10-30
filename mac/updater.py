# -*- coding: utf-8 -*-
"""
Обновление по кнопке: смотрим сборки на GitHub, качаем новую и заменяем программу.

Сборки берутся из Releases публичного репозитория. Номер текущей сборки записан
в build_info.py (его заполняет GitHub при сборке; при ручной сборке там 0).
"""
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

try:
    from build_info import BUILD
except Exception:
    BUILD = 0

REPO = "Chame1eon90/capslocker"
UA = {"User-Agent": "Capslocker-updater", "Accept": "application/vnd.github+json"}


def asset_name():
    if sys.platform == "win32":
        return "Capslocker-Windows.zip"
    arch = platform.machine().lower()
    return "Capslocker-macOS-Apple-M.zip" if arch in ("arm64", "aarch64") else "Capslocker-macOS-Intel.zip"


def _num(tag):
    m = re.search(r"(\d+)\s*$", tag or "")
    return int(m.group(1)) if m else 0


def latest(timeout=10):
    """Последняя сборка для этой системы: (номер, ссылка на архив, размер) или None."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases?per_page=15", headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        releases = json.loads(r.read().decode("utf-8"))
    want = asset_name()
    best = None
    for rel in releases:
        if rel.get("draft"):
            continue
        for a in rel.get("assets", []):
            if a.get("name") == want:
                n = _num(rel.get("tag_name"))
                if best is None or n > best[0]:
                    best = (n, a["browser_download_url"], a.get("size", 0))
    return best


def download(url, dest, progress=lambda pct, msg: None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Capslocker-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                progress(int(done * 100 / total), f"Скачиваю обновление: {done // 1048576} из {total // 1048576} МБ")


def can_self_update():
    """Обновлять себя умеет только собранная программа (exe или .app), не запуск из исходников."""
    return bool(getattr(sys, "frozen", False))


def install_and_restart(zip_path):
    """Распаковать скачанное, запустить замену после выхода программы. Вызывающий должен сразу выйти."""
    work = tempfile.mkdtemp(prefix="capslocker_update_")
    pid = os.getpid()
    if sys.platform == "win32":
        import zipfile
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if n.lower().endswith("capslocker.exe")]
            if not names:
                raise RuntimeError("В архиве нет Capslocker.exe")
            new_exe = os.path.join(work, "Capslocker.new.exe")
            with z.open(names[0]) as src, open(new_exe, "wb") as dst:
                shutil.copyfileobj(src, dst)
        exe = sys.executable

        def q(p):
            return "'" + p.replace("'", "''") + "'"
        ps = (f"$p={pid}; while (Get-Process -Id $p -ErrorAction SilentlyContinue) {{ Start-Sleep -Milliseconds 300 }}; "
              f"Start-Sleep -Milliseconds 800; "
              f"for ($i=0; $i -lt 20; $i++) {{ try {{ Move-Item -LiteralPath {q(new_exe)} -Destination {q(exe)} -Force -ErrorAction Stop; break }} "
              f"catch {{ Start-Sleep -Milliseconds 500 }} }}; "
              f"Start-Process -FilePath {q(exe)}; Remove-Item -LiteralPath {q(work)} -Recurse -Force -ErrorAction SilentlyContinue")
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-EncodedCommand", enc], creationflags=0x08000000 | 0x00000008, close_fds=True)
    else:
        subprocess.run(["ditto", "-x", "-k", zip_path, work], check=True)
        new_app = os.path.join(work, "Capslocker.app")
        if not os.path.isdir(new_app):
            raise RuntimeError("В архиве нет Capslocker.app")
        # sys.executable = …/Capslocker.app/Contents/MacOS/Capslocker
        app = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
        if not app.endswith(".app"):
            raise RuntimeError("Не нашёл, где лежит программа")
        script = (f'while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; sleep 0.8; '
                  f'rm -rf "{app}" && mv "{new_app}" "{app}" && xattr -cr "{app}" ; open "{app}"; rm -rf "{work}"')
        subprocess.Popen(["/bin/bash", "-c", script], start_new_session=True, close_fds=True)
