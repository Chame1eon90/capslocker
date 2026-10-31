# -*- coding: utf-8 -*-
"""
CapsFix — ядро: перехват клавиатуры и исправление раскладки по CapsLock.

• Короткое нажатие CapsLock: последнее слово перенабирается в другой раскладке
  и язык переключается. Если слова нет — просто переключает язык.
• Ещё одно нажатие сразу после исправления — отмена.
• Удержание CapsLock — обычный Caps Lock.
• Shift+CapsLock — переделать выделенный текст.
"""
import ctypes
import os
import queue
import threading
import time
import traceback
from ctypes import wintypes

from layouts import KEYS, SPACE_SCAN, render, convert_text, key_char

from core import DATA_DIR, log  # общий журнал Капслокера


# ------------------------------------------------------------------ Win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_INPUTLANGCHANGEREQUEST = 0x0050
LLKHF_INJECTED = 0x10
MARKER = 0x0CAF5F1C  # метка наших собственных нажатий

VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU = 0x08, 0x09, 0x0D, 0x10, 0x11, 0x12
VK_CAPITAL, VK_SPACE, VK_LWIN, VK_RWIN = 0x14, 0x20, 0x5B, 0x5C
MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C, VK_CAPITAL}

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x2, 0x4, 0x8


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


WH_MOUSE_LL = 14
MOUSE_CLICKS = {0x201, 0x204, 0x207, 0x20B}  # кнопки мыши: левая, правая, средняя, боковые
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = ctypes.c_void_p
user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyState.restype = ctypes.c_short
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]

LANG_PRIMARY = {0x19: "ru", 0x09: "en"}


def _down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _send(inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}  # PgUp..стрелки, Ins, Del


def _key_input(vk, up=False, scan=0):
    i = INPUT(type=INPUT_KEYBOARD)
    flags = (KEYEVENTF_KEYUP if up else 0) | (1 if vk in EXTENDED_VKS else 0)
    i.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=MARKER)
    return i


def _uni_input(ch, up=False):
    i = INPUT(type=INPUT_KEYBOARD)
    i.u.ki = KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                        time=0, dwExtraInfo=MARKER)
    return i


KEY_DELAY = 0.004   # небольшие паузы: некоторые программы и игры теряют слишком быстрые нажатия


def tap(vk, times=1):
    time.sleep(0.015)
    for _ in range(times):
        _send([_key_input(vk), _key_input(vk, True)])
        time.sleep(KEY_DELAY)


def type_text(text):
    time.sleep(0.02)
    for ch in text:
        if ch == "\n":
            _send([_key_input(VK_RETURN), _key_input(VK_RETURN, True)])
        else:
            _send([_uni_input(ch), _uni_input(ch, True)])
        time.sleep(KEY_DELAY)


def combo(mod_vk, vk):
    _send([_key_input(mod_vk), _key_input(vk), _key_input(vk, True), _key_input(mod_vk, True)])


def foreground():
    return user32.GetForegroundWindow()


def window_lang(hwnd):
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    hkl = user32.GetKeyboardLayout(tid) or 0
    return LANG_PRIMARY.get(int(hkl) & 0x3FF)


def layouts_list():
    n = user32.GetKeyboardLayoutList(0, None)
    arr = (ctypes.c_void_p * max(n, 1))()
    n = user32.GetKeyboardLayoutList(n, arr)
    return [int(arr[i] or 0) for i in range(n)]


def switch_layout(hwnd, lang=None):
    """lang=None — следующая раскладка по кругу."""
    if os.environ.get("CAPSFIX_NOSWITCH"):
        log(f"layouts={[hex(h) for h in layouts_list()]} want={lang}")
        return
    target = 1  # HKL_NEXT
    if lang:
        for hkl in layouts_list():
            if LANG_PRIMARY.get(hkl & 0x3FF) == lang:
                target = hkl
                break
    targets = [hwnd]
    try:
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus and info.hwndFocus != hwnd:
            targets.insert(0, info.hwndFocus)
    except Exception:
        pass
    if target == 1:
        targets = targets[:1]   # «следующую» шлём один раз, иначе перескочит через одну
    for h in targets:
        user32.PostMessageW(h, WM_INPUTLANGCHANGEREQUEST, 0, target)


def process_name(hwnd):
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        return os.path.basename(buf.value).lower() if ok else ""
    except Exception:
        return ""


def get_clipboard():
    import pyperclip
    try:
        return pyperclip.paste()
    except Exception:
        return None


def set_clipboard(text):
    import pyperclip
    try:
        pyperclip.copy(text)
    except Exception:
        pass


# Пока раскладка пользуется буфером обмена, переводчик его не трогает
CLIP_QUIET_UNTIL = 0.0


def clip_quiet(sec=1.5):
    global CLIP_QUIET_UNTIL
    CLIP_QUIET_UNTIL = time.time() + sec


# ------------------------------------------------------------------ движок
class Engine:
    def __init__(self, settings, on_event=lambda kind, text: None):
        self.settings = settings
        self.on_event = on_event          # уведомления для интерфейса
        self.q = queue.Queue()
        self._hook = None
        self._proc = HOOKPROC(self._hook_proc)
        self._mproc = HOOKPROC(self._mouse_proc)
        self._mhook = None
        self.thread_id = None

        self.buf = []           # всё, что набрано в этом поле: [(scan, shift, caps)], пробел = SPACE_SCAN
        self.buf_hwnd = None
        self.last_type = 0.0     # когда последний раз что-то набрали
        self.last_fix = None    # для отмены
        self.auto = None        # core.AutoLayout — автоматическое исправление (если включено)
        self.on_auto_skip = None  # функция(слово): автоисправление отменили — больше это слово не трогать
        # пока идёт автоисправление, нажатия придерживаем и потом отправляем сами — иначе буквы,
        # набранные во время замены, попадут в середину слова
        self.hold = False
        self.hold_since = 0.0
        self.pending = []
        self.hold_lock = threading.Lock()
        self.lang_hint = None    # (hwnd, язык, время): раскладка, на которую только что переключили
        self.caps_down = False
        self.caps_press_id = 0
        self.caps_toggled = False
        self.caps_shift = False
        self.caps_on = bool(user32.GetKeyState(VK_CAPITAL) & 1)

    # ---------- запуск
    def start(self):
        threading.Thread(target=self._hook_thread, daemon=True, name="hook").start()
        threading.Thread(target=self._worker, daemon=True, name="worker").start()

    def _hook_thread(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            log("SetWindowsHookEx failed: %d" % ctypes.get_last_error())
            self.on_event("error", "Не удалось перехватить клавиатуру")
            return
        self._mhook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mproc, kernel32.GetModuleHandleW(None), 0)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self._hook)
        if self._mhook:
            user32.UnhookWindowsHookEx(self._mhook)

    def _mouse_proc(self, n, wparam, lparam):
        try:
            if n == 0 and wparam in MOUSE_CLICKS:
                self.q.put(("click",))
        except Exception:
            pass
        return user32.CallNextHookEx(None, n, wparam, lparam)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT

    # ---------- перехват (должен работать очень быстро)
    def _hook_proc(self, n, wparam, lparam):
        try:
            if n == 0:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.dwExtraInfo != MARKER:
                    down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    injected = bool(kb.flags & LLKHF_INJECTED)
                    if self.hold and not injected and kb.vkCode not in MODIFIER_VKS:
                        with self.hold_lock:
                            if self.hold and time.time() - self.hold_since < 2.0 \
                                    and not (_down(VK_CONTROL) or _down(VK_MENU)):
                                if down:
                                    self.pending.append((kb.vkCode, kb.scanCode, kb.flags, _down(VK_SHIFT)))
                                return 1  # придержали — отправим после замены
                    if kb.vkCode == VK_CAPITAL and not injected and self.settings.get("enabled", True):
                        self.q.put(("caps", down, _down(VK_SHIFT)))
                        return 1  # глотаем CapsLock — дальше решаем сами
                    if down and kb.scanCode == SPACE_SCAN and not injected and self._auto_on() \
                            and not (_down(VK_CONTROL) or _down(VK_MENU)) and self._auto_here():
                        with self.hold_lock:
                            self.hold, self.hold_since = True, time.time()
                        # пробел печатаем сами после проверки — так он точно не опередит замену
                        self.q.put(("space_auto", _down(VK_SHIFT)))
                        return 1
                    if down:
                        self.q.put(("key", kb.vkCode, kb.scanCode, kb.flags, injected,
                                    _down(VK_SHIFT), _down(VK_CONTROL), _down(VK_MENU),
                                    _down(VK_LWIN) or _down(VK_RWIN)))
        except Exception:
            pass
        return user32.CallNextHookEx(None, n, wparam, lparam)

    # ---------- обработка
    def _worker(self):
        while True:
            ev = self.q.get()
            try:
                if ev[0] == "key":
                    self._on_key(*ev[1:])
                elif ev[0] == "space_auto":
                    self._on_space_auto(ev[1])
                elif ev[0] == "caps":
                    self._on_caps(*ev[1:])
                elif ev[0] == "hold":
                    self._on_hold(ev[1])
                elif ev[0] == "click":
                    self.reset()
                    self.last_fix = None
            except Exception:
                log("worker: " + traceback.format_exc())

    def reset(self):
        self.buf, self.buf_hwnd = [], None

    def _segment(self):
        """Что исправлять: всю фразу или только последнее слово (с пробелами после него)."""
        if self.settings.get("scope", "phrase") == "phrase":
            i = 0
            while i < len(self.buf) and self.buf[i][0] == SPACE_SCAN:
                i += 1  # пробелы в начале не трогаем
            return len(self.buf) - i
        j = len(self.buf)
        while j > 0 and self.buf[j - 1][0] == SPACE_SCAN:
            j -= 1
        while j > 0 and self.buf[j - 1][0] != SPACE_SCAN:
            j -= 1
        return len(self.buf) - j

    def _on_key(self, vk, scan, flags, injected, shift, ctrl, alt, win):
        self.last_fix = None
        if vk in MODIFIER_VKS:
            return
        if injected or ctrl or alt or win:
            self.reset()  # вставка другой программой, сочетания клавиш
            return
        extended = bool(flags & 1)
        if vk == VK_BACK:
            if self.buf:
                self.buf.pop()
                self.last_type = time.time()
            return
        if (scan in KEYS or scan == SPACE_SCAN) and not extended:
            hwnd = foreground()
            if hwnd != self.buf_hwnd:
                self.buf = []
            self.buf_hwnd = hwnd
            self.buf.append((scan, shift, self.caps_on))
            self.last_type = time.time()
            if len(self.buf) > 400:
                self.buf = self.buf[-400:]
            if scan == SPACE_SCAN:
                self._flush_pending(hwnd)
            return
        self.reset()  # Enter, Tab, стрелки, Esc и т. п.

    def _on_caps(self, down, shift):
        if os.environ.get("CAPSFIX_DEBUG"):
            log(f"caps down={down} shift={shift} buf={len(self.buf)}")
        if down:
            if self.caps_down:
                return  # автоповтор
            self.caps_down = True
            self.caps_toggled = False
            self.caps_shift = shift
            self.caps_press_id += 1
            pid = self.caps_press_id
            hold = max(0.2, self.settings.get("hold_ms", 450) / 1000)
            threading.Timer(hold, lambda: self.q.put(("hold", pid))).start()
            return
        self.caps_down = False
        if self.caps_toggled:
            return
        hwnd = foreground()
        if self._excluded(hwnd):
            self._toggle_caps()
            return
        if self.caps_shift and self.settings.get("selection", True):
            self._convert_selection(hwnd)
        else:
            self._fix_word(hwnd)

    def _on_hold(self, pid):
        if self.caps_down and pid == self.caps_press_id and not self.caps_toggled:
            self.caps_toggled = True
            self._toggle_caps()

    def _toggle_caps(self):
        tap(VK_CAPITAL)
        self.caps_on = not self.caps_on
        self.on_event("caps", "Caps Lock включён" if self.caps_on else "Caps Lock выключен")

    def _excluded(self, hwnd):
        ex = [x.strip().lower() for x in self.settings.get("exclude", "").replace(";", ",").split(",") if x.strip()]
        if not ex:
            return False
        name = process_name(hwnd)
        return any(name == e or name == e + ".exe" for e in ex)

    # ---------- действия
    def _auto_on(self):
        return bool(self.settings.get("auto") and self.settings.get("enabled", True) and self.auto and self.auto.ready())

    def _auto_here(self):
        """Автоисправление работает в активном окне? (запоминаем по окну, чтобы перехват был быстрым)"""
        hwnd = foreground()
        c = getattr(self, "_here_cache", None)
        key = (hwnd, self.settings.get("auto_only", ""), self.settings.get("exclude", ""))
        if c and c[0] == key:
            return c[1]
        ok = not self._excluded(hwnd) and self._auto_allowed(process_name(hwnd))
        self._here_cache = (key, ok)
        return ok

    def _lang(self, hwnd):
        h = self.lang_hint
        if h and h[0] == hwnd and time.time() - h[2] < 1.5:
            return h[1]
        return window_lang(hwnd)

    def _on_space_auto(self, shift):
        """Пробел, который мы придержали: проверить слово перед ним, напечатать пробел и всё, что набрали дальше."""
        self.last_fix = None
        hwnd = foreground()
        try:
            if hwnd != self.buf_hwnd:
                self.buf = []
            self.buf_hwnd = hwnd
            self.buf.append((SPACE_SCAN, shift, self.caps_on))
            self.last_type = time.time()
            if not self._auto_check(hwnd):
                type_text(" ")
        finally:
            self._flush_pending(hwnd)

    def _auto_check(self, hwnd):
        """Слово перед пробелом (пробел ещё не напечатан) набрано не в той раскладке? Тогда заменить его
        вместе с пробелом и переключить язык. Возвращает True, если заменили."""
        if not self._auto_on():
            return False
        j = len(self.buf) - 1                      # buf[-1] — только что нажатый пробел
        i = j
        while i > 0 and self.buf[i - 1][0] != SPACE_SCAN:
            i -= 1
        keys = self.buf[i:j]
        if len(keys) < 3 or self._excluded(hwnd) or not self._auto_allowed(process_name(hwnd)):
            return False
        src = self._lang(hwnd)
        if src not in ("ru", "en"):
            return False
        tgt = "en" if src == "ru" else "ru"
        old, new = render(keys, src), render(keys, tgt)
        if not self.auto.decide(old, new, src):
            return False
        tap(VK_BACK, len(keys))
        type_text(new + " ")
        switch_layout(hwnd, tgt)
        self.lang_hint = (hwnd, tgt, time.time())
        self.reset()
        self.buf_hwnd = hwnd
        self.last_fix = {"hwnd": hwnd, "old": old + " ", "new": new + " ", "src": src, "auto": True}
        self.on_event("auto", f"{old} → {new}")
        return True

    def _flush_pending(self, hwnd):
        """Отправить нажатия, придержанные во время проверки, — уже в правильной раскладке."""
        while True:
            with self.hold_lock:
                items, self.pending = self.pending, []
                if not items:
                    self.hold = False
                    return
            for vk, scan, flags, shift in items:
                extended = bool(flags & 1)
                if (scan in KEYS or scan == SPACE_SCAN) and not extended:
                    ch = key_char(scan, shift, self.caps_on, self._lang(hwnd) or "en")
                    self.buf.append((scan, shift, self.caps_on))
                    self.last_type = time.time()
                    if scan == SPACE_SCAN:
                        self.last_fix = None
                        if self._auto_check(hwnd):
                            continue
                    if ch:
                        type_text(ch)
                elif vk == VK_BACK:
                    tap(VK_BACK)
                    if self.buf:
                        self.buf.pop()
                else:
                    _send([_key_input(vk), _key_input(vk, True)])
                    self.reset()

    def _auto_allowed(self, name):
        """Автоисправление — только в выбранных программах (пустой список — везде)."""
        only = [x.strip().lower() for x in self.settings.get("auto_only", "").replace(";", ",").split(",") if x.strip()]
        if not only:
            return True
        name = (name or "").lower().replace(".exe", "").replace(".app", "")
        return bool(name) and any(e.replace(".exe", "").replace(".app", "") in name for e in only)

    def _fix_word(self, hwnd):
        lf = self.last_fix
        if lf and lf["hwnd"] == hwnd:
            # отмена последнего исправления
            tap(VK_BACK, len(lf["new"]))
            type_text(lf["old"])
            switch_layout(hwnd, lf["src"])
            self.last_fix = None
            self.on_event("undo", lf["old"].strip())
            if lf.get("auto") and self.auto:
                w = lf["old"].strip().lower()
                self.auto.skip.add(w)
                if self.on_auto_skip:
                    self.on_auto_skip(w)
            return
        n = self._segment() if hwnd == self.buf_hwnd else 0
        if n == 0 and self._fix_existing(hwnd):
            return
        if n == 0:
            cur = window_lang(hwnd)
            switch_layout(hwnd, None if cur is None else ("en" if cur == "ru" else "ru"))
            self.on_event("switch", "")
            return
        keys = self.buf[-n:]
        src = window_lang(hwnd) or "en"
        tgt = "ru" if src == "en" else "en"
        old, new = render(keys, src), render(keys, tgt)
        tap(VK_BACK, len(keys))
        type_text(new)
        switch_layout(hwnd, tgt)
        self.last_fix = {"hwnd": hwnd, "old": old, "new": new, "src": src}
        self.on_event("fix", f"{old.strip()} → {new.strip()}")
        if os.environ.get("CAPSFIX_DEBUG"):
            log(f"fix {old!r} -> {new!r}")

    def _fix_existing(self, hwnd):
        """Фраза была в поле раньше (до запуска, до клика) — берём текст строки до курсора."""
        clip_quiet()
        deadline = time.time() + 1.0
        while (_down(VK_SHIFT) or _down(VK_CONTROL)) and time.time() < deadline:
            time.sleep(0.02)
        saved = get_clipboard()
        marker = "\u2063capsfix\u2063"
        set_clipboard(marker)
        _send([_key_input(VK_SHIFT), _key_input(0x24), _key_input(0x24, True), _key_input(VK_SHIFT, True)])  # Shift+Home
        time.sleep(0.03)
        combo(VK_CONTROL, 0x43)  # Ctrl+C
        text = marker
        for _ in range(20):
            time.sleep(0.02)
            text = get_clipboard()
            if text != marker:
                break
        if saved is not None:
            threading.Thread(target=lambda: (time.sleep(0.3), set_clipboard(saved)), daemon=True).start()

        def unselect():
            _send([_key_input(0x27), _key_input(0x27, True)])  # стрелка вправо — снять выделение

        if text == marker:
            unselect()  # программа не умеет Ctrl+C — оставляем как было
            return False
        if not text or "\n" in text or len(text) > 500:
            if text:
                unselect()
            return False
        if self.settings.get("scope", "phrase") == "word":
            m = len(text)
            while m > 0 and text[m - 1] == " ":
                m -= 1
            while m > 0 and text[m - 1] != " ":
                m -= 1
            seg = text[m:]
        else:
            seg = text.lstrip(" ")
        prefix = text[:len(text) - len(seg)]
        new, tgt = convert_text(seg)
        if tgt is None or new == seg:
            unselect()
            return False
        time.sleep(0.02)
        type_text(prefix + new)  # печать поверх выделения заменяет его
        switch_layout(hwnd, tgt)
        self.reset()
        self.last_fix = {"hwnd": hwnd, "old": seg, "new": new, "src": "ru" if tgt == "en" else "en"}
        self.on_event("fix", f"{seg.strip()} → {new.strip()}")
        if os.environ.get("CAPSFIX_DEBUG"):
            log(f"fix existing {seg!r} -> {new!r}")
        return True

    def _convert_selection(self, hwnd):
        clip_quiet()
        # ждём, пока отпустят Shift, иначе Ctrl+C превратится в Ctrl+Shift+C
        deadline = time.time() + 1.5
        while _down(VK_SHIFT) and time.time() < deadline:
            time.sleep(0.02)
        saved = get_clipboard()
        marker = "⁣capsfix⁣"
        set_clipboard(marker)
        combo(VK_CONTROL, 0x43)  # Ctrl+C
        text = marker
        for _ in range(25):
            time.sleep(0.02)
            text = get_clipboard()
            if text != marker:
                break
        if not text or text == marker:
            if saved is not None:
                set_clipboard(saved)
            self._fix_word(hwnd)  # ничего не выделено — как обычное нажатие
            return
        new, tgt = convert_text(text)
        if tgt is None:
            if saved is not None:
                set_clipboard(saved)
            return
        set_clipboard(new)
        combo(VK_CONTROL, 0x56)  # Ctrl+V
        switch_layout(hwnd, tgt)
        self.reset()
        self.on_event("selection", new[:40])

        def restore():
            time.sleep(0.4)
            if saved is not None:
                set_clipboard(saved)
        threading.Thread(target=restore, daemon=True).start()
