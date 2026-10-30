# -*- coding: utf-8 -*-
"""
Обновление по кнопке: смотрим сборки на GitHub, качаем новую и заменяем программу.

Сборки берутся из Releases публичного репозитория. Номер текущей сборки записан
в build_info.py (его заполняет GitHub при сборке; при ручной сборке там 0).
"""
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
    # GitHub сортирует релизы по названию как по тексту («build-9» раньше «build-10»),
    # поэтому берём весь список (до 3 страниц) и сравниваем номера как числа
    releases = []
    for page in (1, 2, 3):
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}",
                                     headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            chunk = json.loads(r.read().decode("utf-8"))
        releases += chunk
        if len(chunk) < 100:
            break
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


def windows_script(pid, exe, new_exe, work, ulog):
    def q(p):
        return "'" + p.replace("'", "''") + "'"
    return f"""$ErrorActionPreference = 'Continue'
$log = {q(ulog)}
function L($m) {{ Add-Content -LiteralPath $log -Value ((Get-Date -Format 'HH:mm:ss') + ' ' + $m) -Encoding UTF8 }}
$exe = {q(exe)}; $new = {q(new_exe)}
L "Скрипт замены начал работу, жду закрытия программы (pid {pid})"
$ok = $false
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {{
  $busy = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -eq $exe }})
  if ($busy.Count -eq 0) {{
    try {{ Move-Item -LiteralPath $new -Destination $exe -Force -ErrorAction Stop; L 'Файл заменён'; $ok = $true; break }}
    catch {{ L ('Файл ещё занят: ' + $_.Exception.Message) }}
  }}
  Start-Sleep -Milliseconds 500
}}
if (-not $ok) {{ L 'Не удалось заменить файл за 90 секунд' }}
Start-Sleep -Milliseconds 300
Start-Process -FilePath $exe
L 'Программа запущена'
Remove-Item -LiteralPath {q(work)} -Recurse -Force -ErrorAction SilentlyContinue
"""


def clean_env():
    """Окружение без служебных переменных PyInstaller: иначе новая версия решит, что она
    «дочерний процесс» старой, и будет искать свои файлы в уже удалённой временной папке."""
    env = {k: v for k, v in os.environ.items()
           if not (k.upper().startswith("_PYI") or k.upper().startswith("_MEI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and "PATH" in env:
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                      if not p.lower().startswith(meipass.lower()))
    for k in ("TCL_LIBRARY", "TK_LIBRARY", "SSL_CERT_FILE"):
        if meipass and env.get(k, "").lower().startswith(meipass.lower()):
            env.pop(k, None)
    return env


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
        # Ждём, пока файл программы освободится (у собранного exe два процесса — внешний
        # освобождает файл чуть позже внутреннего), потом заменяем и запускаем новую версию.
        script = os.path.join(work, "replace.ps1")
        with open(script, "w", encoding="utf-8-sig") as f:
            f.write(windows_script(pid, exe, new_exe, work, ulog))
        with open(ulog, "w", encoding="utf-8") as f:
            f.write(f"Запускаю замену: {script}\n")
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", script]
        # независимый процесс: своя группа, без окна; пробуем выйти из «задания» программы
        base = 0x00000200 | 0x08000000          # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        for flags in (base | 0x01000000, base):  # | CREATE_BREAKAWAY_FROM_JOB
            try:
                p = subprocess.Popen(args, creationflags=flags, close_fds=True, env=clean_env(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with open(ulog, "a", encoding="utf-8") as f:
                    f.write(f"powershell запущен, pid {p.pid}, флаги {hex(flags)}\n")
                break
            except OSError as e:
                with open(ulog, "a", encoding="utf-8") as f:
                    f.write(f"не запустился с флагами {hex(flags)}: {e}\n")
        else:
            raise RuntimeError("Не удалось запустить замену файла")
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
        subprocess.Popen(["/bin/bash", "-c", script], start_new_session=True, close_fds=True, env=clean_env())
