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
        logdir = os.path.join(os.environ.get("LOCALAPPDATA", work), "Capslocker")
        os.makedirs(logdir, exist_ok=True)
        ulog = os.path.join(logdir, "update.log")

        def q(p):
            return "'" + p.replace("'", "''") + "'"
        # Ждём, пока файл программы освободится (у собранного exe два процесса — внешний
        # освобождает файл чуть позже внутреннего), потом заменяем и запускаем новую версию.
        ps = f"""
$ErrorActionPreference = 'Continue'
Start-Transcript -Path {q(ulog)} -Force | Out-Null
$exe = {q(exe)}; $new = {q(new_exe)}
Write-Output "Жду закрытия программы (pid {pid})"
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {{
  $busy = Get-Process -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -eq $exe }}
  if (-not $busy) {{
    try {{ Move-Item -LiteralPath $new -Destination $exe -Force -ErrorAction Stop; Write-Output "Файл заменён"; $ok = $true; break }}
    catch {{ Write-Output ("Файл ещё занят: " + $_.Exception.Message) }}
  }}
  Start-Sleep -Milliseconds 500
}}
if (-not $ok) {{ Write-Output "Не удалось заменить файл за 90 секунд" }}
Start-Sleep -Milliseconds 300
Start-Process -FilePath $exe
Write-Output "Программа запущена"
Remove-Item -LiteralPath {q(work)} -Recurse -Force -ErrorAction SilentlyContinue
Stop-Transcript | Out-Null
"""
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        # отдельный независимый процесс: вне группы и задания программы, без окна
        flags = 0x00000008 | 0x00000200 | 0x01000000 | 0x08000000
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                "-EncodedCommand", enc]
        try:
            subprocess.Popen(args, creationflags=flags, close_fds=True)
        except OSError:
            # если выход из «задания» запрещён — запускаем без этого флага
            subprocess.Popen(args, creationflags=flags & ~0x01000000, close_fds=True)
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
