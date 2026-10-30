# -*- coding: utf-8 -*-
"""
Капслокер для macOS — работа с клавиатурой.

• Перехват клавиш через Quartz Event Tap (нужны разрешения «Универсальный доступ»
  и «Мониторинг ввода»).
• CapsLock на время работы программы переназначается в F18 (через hidutil), чтобы
  macOS сама не включала Caps Lock. При выходе всё возвращается как было.
• Исправление раскладки: стираем набранное, переключаем раскладку и «повторяем»
  те же физические клавиши — macOS сама напечатает их в новой раскладке.
• Горячая клавиша переводчика: выделить всё → скопировать → перевести → вставить.
"""
import atexit
import ctypes
import json
import os
import queue
import subprocess
import threading
import time
import traceback

import objc
import Quartz
from Quartz import (
    CGEventTapCreate, CGEventTapEnable, CGEventMaskBit, CGEventGetIntegerValueField,
    CGEventSetIntegerValueField, CGEventGetFlags, CGEventSetFlags, CGEventCreateKeyboardEvent,
    CGEventKeyboardSetUnicodeString, CGEventPost,
    kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault, kCGHIDEventTap,
    kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged,
    kCGEventLeftMouseDown, kCGEventRightMouseDown, kCGEventOtherMouseDown,
    kCGKeyboardEventKeycode, kCGEventSourceUserData,
    kCGEventFlagMaskShift, kCGEventFlagMaskCommand, kCGEventFlagMaskControl,
    kCGEventFlagMaskAlternate, kCGEventFlagMaskAlphaShift,
    kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
)
from CoreFoundation import (
    CFMachPortCreateRunLoopSource, CFRunLoopGetCurrent, CFRunLoopAddSource, CFRunLoopRun,
    kCFRunLoopCommonModes,
)

from core import log
from layouts import convert_text

MARKER = 0x0CAF5F1C

# --- виртуальные коды клавиш macOS (физические, не зависят от раскладки)
VK_BACKSPACE, VK_RETURN, VK_SPACE, VK_TAB, VK_ESC = 51, 36, 49, 48, 53
VK_LEFT, VK_RIGHT, VK_DOWN, VK_UP = 123, 124, 125, 126
VK_A, VK_C, VK_V = 0, 8, 9
VK_F18 = 79
VK_CAPSLOCK = 57
MODIFIER_KEYCODES = {54, 55, 56, 57, 58, 59, 60, 61, 62, 63}
# клавиши, которые печатают символы (буквы, цифры, знаки)
CHAR_KEYCODES = set(range(0, 51)) - {36, 48} | {10}

KEY_NAMES = {0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X", 8: "C", 9: "V", 11: "B",
             12: "Q", 13: "W", 14: "E", 15: "R", 16: "Y", 17: "T", 18: "1", 19: "2", 20: "3", 21: "4",
             22: "6", 23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8", 29: "0", 30: "]", 31: "O",
             32: "U", 33: "[", 34: "I", 35: "P", 37: "L", 38: "J", 39: "'", 40: "K", 41: ";", 42: "\\",
             43: ",", 44: "/", 45: "N", 46: "M", 47: ".", 50: "`", 36: "Return", 49: "Space",
             122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6", 98: "F7", 100: "F8",
             101: "F9", 109: "F10", 103: "F11", 111: "F12", 105: "F13", 107: "F14", 113: "F15",
             106: "F16", 64: "F17", 79: "F18", 80: "F19"}
MOD_BITS = [("ctrl", kCGEventFlagMaskControl, "⌃"), ("alt", kCGEventFlagMaskAlternate, "⌥"),
            ("shift", kCGEventFlagMaskShift, "⇧"), ("cmd", kCGEventFlagMaskCommand, "⌘")]


def hotkey_label(hk):
    mods = "".join(sym for name, _, sym in MOD_BITS if name in hk.get("mods", []))
    return mods + KEY_NAMES.get(hk.get("key"), f"#{hk.get('key')}")


# ------------------------------------------------------------------ отправка нажатий
def _post(ev):
    CGEventSetIntegerValueField(ev, kCGEventSourceUserData, MARKER)
    CGEventPost(kCGHIDEventTap, ev)


ARROW_FLAGS = 0x200000 | 0x800000   # kCGEventFlagMaskNumericPad | kCGEventFlagMaskSecondaryFn


def key(code, flags=0):
    if code in (VK_LEFT, VK_RIGHT, VK_UP, VK_DOWN, 115, 119):  # стрелки, Home, End
        flags |= ARROW_FLAGS
    for down in (True, False):
        ev = CGEventCreateKeyboardEvent(None, code, down)
        CGEventSetFlags(ev, flags)
        _post(ev)
    time.sleep(0.004)


def taps(code, n):
    time.sleep(0.015)
    for _ in range(n):
        key(code)


def type_text(text):
    time.sleep(0.02)
    for ch in text:
        for down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, 0, down)
            CGEventSetFlags(ev, 0)
            CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            _post(ev)
        time.sleep(0.004)


def cmd(code):
    key(code, kCGEventFlagMaskCommand)


def mods_down():
    flags = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState)
    return flags & (kCGEventFlagMaskShift | kCGEventFlagMaskCommand | kCGEventFlagMaskControl
                    | kCGEventFlagMaskAlternate)


def wait_mods_released(timeout=1.0):
    end = time.time() + timeout
    while mods_down() and time.time() < end:
        time.sleep(0.02)


# ------------------------------------------------------------------ буфер обмена
# Прямой доступ к буферу macOS. Команды pbcopy/pbpaste не годятся: при запуске из Finder
# у программы нет настроек языка, и они портят русские буквы.
def _pb():
    from AppKit import NSPasteboard
    return NSPasteboard.generalPasteboard()


def get_clipboard():
    try:
        from AppKit import NSPasteboardTypeString
        s = _pb().stringForType_(NSPasteboardTypeString)
        return str(s) if s is not None else ""
    except Exception:
        log("get_clipboard: " + traceback.format_exc())
        return None


def set_clipboard(text):
    try:
        from AppKit import NSPasteboardTypeString
        pb = _pb()
        pb.clearContents()
        pb.setString_forType_(text or "", NSPasteboardTypeString)
    except Exception:
        log("set_clipboard: " + traceback.format_exc())


def clip_count():
    try:
        return int(_pb().changeCount())
    except Exception:
        return -1


def copy_selection(timeout=0.8):
    """⌘C и ждём, пока буфер действительно изменится. Возвращает текст или None."""
    before = clip_count()
    cmd(VK_C)
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.03)
        if clip_count() != before:
            time.sleep(0.02)
            return get_clipboard()
    return None


# ------------------------------------------------------------------ раскладки (Text Input Sources)
_carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
_cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_c = ctypes.c_void_p
_carbon.TISCreateInputSourceList.restype = _c
_carbon.TISCreateInputSourceList.argtypes = [_c, ctypes.c_bool]
_carbon.TISGetInputSourceProperty.restype = _c
_carbon.TISGetInputSourceProperty.argtypes = [_c, _c]
_carbon.TISSelectInputSource.restype = ctypes.c_int32
_carbon.TISSelectInputSource.argtypes = [_c]
_carbon.TISCopyCurrentKeyboardInputSource.restype = _c
_cf.CFArrayGetCount.restype = ctypes.c_long
_cf.CFArrayGetCount.argtypes = [_c]
_cf.CFArrayGetValueAtIndex.restype = _c
_cf.CFArrayGetValueAtIndex.argtypes = [_c, ctypes.c_long]
_cf.CFRelease.argtypes = [_c]
_cf.CFEqual.restype = ctypes.c_bool
_cf.CFEqual.argtypes = [_c, _c]
_cf.CFBooleanGetValue.restype = ctypes.c_bool
_cf.CFBooleanGetValue.argtypes = [_c]


def _const(name):
    return _c.in_dll(_carbon, name).value


K_LANGS = _const("kTISPropertyInputSourceLanguages")
K_CATEGORY = _const("kTISPropertyInputSourceCategory")
K_KEYBOARD = _const("kTISCategoryKeyboardInputSource")
K_SELECTABLE = _const("kTISPropertyInputSourceIsSelectCapable")


def _lang_of(src):
    p = _carbon.TISGetInputSourceProperty(src, K_LANGS)
    if not p:
        return None
    langs = objc.objc_object(c_void_p=p)
    return str(langs[0])[:2] if len(langs) else None


def current_lang():
    src = _carbon.TISCopyCurrentKeyboardInputSource()
    try:
        lang = _lang_of(src)
    finally:
        _cf.CFRelease(src)
    return lang if lang in ("ru", "en") else ("en" if lang else None)


def select_lang(lang):
    arr = _carbon.TISCreateInputSourceList(None, False)
    try:
        for i in range(_cf.CFArrayGetCount(arr)):
            src = _cf.CFArrayGetValueAtIndex(arr, i)
            cat = _carbon.TISGetInputSourceProperty(src, K_CATEGORY)
            if not cat or not _cf.CFEqual(cat, K_KEYBOARD):
                continue
            sel = _carbon.TISGetInputSourceProperty(src, K_SELECTABLE)
            if not sel or not _cf.CFBooleanGetValue(sel):
                continue
            if _lang_of(src) == lang:
                _carbon.TISSelectInputSource(src)
                return True
    finally:
        _cf.CFRelease(arr)
    return False


# ------------------------------------------------------------------ Caps Lock: переназначение и состояние
CAPS_SRC, F18_DST = 0x700000039, 0x70000006D


def _hidutil_get():
    try:
        out = subprocess.run(["hidutil", "property", "--get", "UserKeyMapping"], capture_output=True,
                             timeout=3).stdout.decode()
        # вывод в формате plist-подобного текста; ищем наши числа
        return out
    except Exception:
        return ""


def _hidutil_set(mappings):
    data = json.dumps({"UserKeyMapping": [{"HIDKeyboardModifierMappingSrc": s,
                                           "HIDKeyboardModifierMappingDst": d} for s, d in mappings]})
    subprocess.run(["hidutil", "property", "--set", data], capture_output=True, timeout=3)


def remap_caps(on):
    """Caps Lock → F18 (on) или обратно (off). Чужие переназначения не трогаем, если их нет."""
    try:
        current = _hidutil_get()
        others_present = "HIDKeyboardModifierMappingSrc" in current and str(CAPS_SRC) not in current
        if others_present:
            log("hidutil: у пользователя уже есть свои переназначения клавиш — не трогаю их")
        if on:
            _hidutil_set([(CAPS_SRC, F18_DST)])
        else:
            _hidutil_set([])
    except Exception:
        log("hidutil: " + traceback.format_exc())


_iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
_iokit.IOServiceMatching.restype = _c
_iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
_iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
_iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, _c]
_iokit.IOServiceOpen.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
_iokit.IOHIDGetModifierLockState.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_bool)]
_iokit.IOHIDSetModifierLockState.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_bool]
_libc = ctypes.CDLL(None)


def _hid_conn():
    svc = _iokit.IOServiceGetMatchingService(0, _iokit.IOServiceMatching(b"IOHIDSystem"))
    conn = ctypes.c_uint32()
    task = ctypes.c_uint32.in_dll(_libc, "mach_task_self_").value
    _iokit.IOServiceOpen(svc, task, 1, ctypes.byref(conn))
    return conn.value


def toggle_caps_lock():
    conn = _hid_conn()
    state = ctypes.c_bool()
    _iokit.IOHIDGetModifierLockState(conn, 1, ctypes.byref(state))
    _iokit.IOHIDSetModifierLockState(conn, 1, not state.value)
    return not state.value


def caps_lock_on():
    try:
        conn = _hid_conn()
        state = ctypes.c_bool()
        _iokit.IOHIDGetModifierLockState(conn, 1, ctypes.byref(state))
        return bool(state.value)
    except Exception:
        return False


# ------------------------------------------------------------------ разрешения
def has_permissions(prompt=False):
    ok_ax = True
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        ok_ax = bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: bool(prompt)}))
    except Exception:
        pass
    ok_listen = True
    try:
        ok_listen = bool(Quartz.CGPreflightListenEventAccess())
        if not ok_listen and prompt:
            Quartz.CGRequestListenEventAccess()
    except Exception:
        pass
    return ok_ax and ok_listen


def open_privacy_settings(which="Accessibility"):
    subprocess.run(["open", f"x-apple.systempreferences:com.apple.preference.security?Privacy_{which}"])


def frontmost_app():
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return (str(app.localizedName() or "").lower(), int(app.processIdentifier()))
    except Exception:
        return ("", 0)


# ------------------------------------------------------------------ движок
CLIP_QUIET_UNTIL = 0.0


def clip_quiet(sec=1.5):
    global CLIP_QUIET_UNTIL
    CLIP_QUIET_UNTIL = time.time() + sec


class MacInput:
    """Перехват клавиатуры: исправление раскладки по CapsLock + горячая клавиша переводчика."""

    def __init__(self, caps_settings, call_main, on_event=lambda kind, text: None):
        self.settings = caps_settings
        self.call_main = call_main        # выполнить функцию в главном потоке (раскладки macOS этого требуют)
        self.on_event = on_event
        self.q = queue.Queue()
        self.tap = None
        self.running = False
        self.hotkey = {"key": 17, "mods": ["ctrl", "alt"]}   # ⌃⌥T
        self.on_hotkey = None
        self.capture_cb = None            # перехват следующего сочетания для смены горячей клавиши

        self.buf, self.buf_app = [], None  # [(keycode, flags)]
        self.last_fix = None
        self.caps_down = False
        self.caps_press_id = 0
        self.caps_toggled = False
        self.caps_shift = False

    # ---------- запуск
    def start(self):
        if self.running:
            return True
        perm = has_permissions(prompt=False)
        if getattr(self, "_last_perm", None) != perm:
            log(f"разрешения macOS: {'есть' if perm else 'НЕТ (Универсальный доступ / Мониторинг ввода)'}")
            self._last_perm = perm
        if not perm:
            return False
        threading.Thread(target=self._tap_thread, daemon=True, name="tap").start()
        threading.Thread(target=self._worker, daemon=True, name="worker").start()
        for _ in range(50):
            if self.tap is not None:
                break
            time.sleep(0.02)
        if self.tap is None:
            return False
        self.running = True
        self.apply_caps_remap()
        log("перехват клавиатуры запущен, CapsLock переназначен")
        atexit.register(lambda: remap_caps(False))
        return True

    def apply_caps_remap(self):
        remap_caps(bool(self.settings.get("enabled", True)))

    def stop(self):
        remap_caps(False)
        if self.tap is not None:
            try:
                CGEventTapEnable(self.tap, False)
            except Exception:
                pass

    def _tap_thread(self):
        mask = (CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventKeyUp) | CGEventMaskBit(kCGEventFlagsChanged)
                | CGEventMaskBit(kCGEventLeftMouseDown) | CGEventMaskBit(kCGEventRightMouseDown)
                | CGEventMaskBit(kCGEventOtherMouseDown))
        tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault, mask,
                               self._callback, None)
        if tap is None:
            log("CGEventTapCreate: нет разрешений")
            self.tap = None
            return
        src = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        self.tap = tap
        CFRunLoopRun()

    # ---------- перехват (быстро!)
    def _callback(self, proxy, etype, event, refcon):
        try:
            if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
                if self.tap is not None:
                    CGEventTapEnable(self.tap, True)
                return event
            if CGEventGetIntegerValueField(event, kCGEventSourceUserData) == MARKER:
                return event
            if etype in (kCGEventLeftMouseDown, kCGEventRightMouseDown, kCGEventOtherMouseDown):
                self.q.put(("click",))
                return event
            code = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            flags = CGEventGetFlags(event)
            down = etype == kCGEventKeyDown
            if etype in (kCGEventKeyDown, kCGEventKeyUp):
                # CapsLock (переназначен в F18)
                if code == VK_F18 and self.settings.get("enabled", True):
                    autorepeat = CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
                    if not autorepeat:
                        self.q.put(("caps", down, bool(flags & kCGEventFlagMaskShift)))
                    return None
                if down:
                    if self.capture_cb is not None and code not in MODIFIER_KEYCODES and code != VK_ESC:
                        mods = [n for n, bit, _ in MOD_BITS if flags & bit]
                        cb, self.capture_cb = self.capture_cb, None
                        self.q.put(("capture", cb, {"key": code, "mods": mods}))
                        return None
                    if self.capture_cb is not None and code == VK_ESC:
                        cb, self.capture_cb = self.capture_cb, None
                        self.q.put(("capture", cb, None))
                        return None
                    if self._is_hotkey(code, flags):
                        if not CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
                            self.q.put(("hotkey",))
                        return None
                    self.q.put(("key", code, flags))
        except Exception:
            pass
        return event

    def _is_hotkey(self, code, flags):
        hk = self.hotkey
        if not hk or code != hk.get("key") or self.on_hotkey is None:
            return False
        want = set(hk.get("mods", []))
        have = {n for n, bit, _ in MOD_BITS if flags & bit}
        return want == have

    # ---------- обработка
    def _worker(self):
        while True:
            ev = self.q.get()
            try:
                kind = ev[0]
                if kind == "key":
                    self._on_key(ev[1], ev[2])
                elif kind == "caps":
                    self._on_caps(ev[1], ev[2])
                elif kind == "hold":
                    self._on_hold(ev[1])
                elif kind == "click":
                    self.reset()
                    self.last_fix = None
                elif kind == "hotkey":
                    self.reset()
                    if self.on_hotkey:
                        self.on_hotkey()
                elif kind == "capture":
                    ev[1](ev[2])
            except Exception:
                log("mac worker: " + traceback.format_exc())

    def reset(self):
        self.buf, self.buf_app = [], None

    def _on_key(self, code, flags):
        self.last_fix = None
        if code in MODIFIER_KEYCODES:
            return
        if flags & (kCGEventFlagMaskCommand | kCGEventFlagMaskControl):
            self.reset()
            return
        if code == VK_BACKSPACE:
            if self.buf:
                self.buf.pop()
            return
        if code in CHAR_KEYCODES or code == VK_SPACE:
            app = frontmost_app()[1]
            if app != self.buf_app:
                self.buf = []
            self.buf_app = app
            self.buf.append((code, flags & (kCGEventFlagMaskShift | kCGEventFlagMaskAlternate)))
            if len(self.buf) > 400:
                self.buf = self.buf[-400:]
            return
        self.reset()

    def _segment(self):
        if self.settings.get("scope", "phrase") == "phrase":
            i = 0
            while i < len(self.buf) and self.buf[i][0] == VK_SPACE:
                i += 1
            return len(self.buf) - i
        j = len(self.buf)
        while j > 0 and self.buf[j - 1][0] == VK_SPACE:
            j -= 1
        while j > 0 and self.buf[j - 1][0] != VK_SPACE:
            j -= 1
        return len(self.buf) - j

    def _on_caps(self, down, shift):
        if down:
            if self.caps_down:
                return
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
        if self._excluded():
            self._toggle_caps()
            return
        if self.caps_shift and self.settings.get("selection", True):
            self._convert_selection()
        else:
            self._fix()

    def _on_hold(self, pid):
        if self.caps_down and pid == self.caps_press_id and not self.caps_toggled:
            self.caps_toggled = True
            self._toggle_caps()

    def _toggle_caps(self):
        try:
            on = toggle_caps_lock()
            self.on_event("caps", "Caps Lock включён" if on else "Caps Lock выключен")
        except Exception:
            log("caps toggle: " + traceback.format_exc())

    def _excluded(self):
        ex = [x.strip().lower() for x in self.settings.get("exclude", "").replace(";", ",").split(",") if x.strip()]
        if not ex:
            return False
        name = frontmost_app()[0]
        return any(name == e or name == e.replace(".app", "") for e in ex)

    def _lang(self):
        return self.call_main(current_lang) or "en"

    def _switch(self, lang):
        return self.call_main(lambda: select_lang(lang))

    def _replay(self, keys):
        caps = kCGEventFlagMaskAlphaShift if caps_lock_on() else 0
        for code, flags in keys:
            key(code, flags | caps)

    # ---------- действия
    def _fix(self):
        app = frontmost_app()[1]
        lf = self.last_fix
        if lf and lf["app"] == app:
            taps(VK_BACKSPACE, lf["n"])
            self._switch(lf["src"])
            time.sleep(0.08)
            self._replay(lf["keys"]) if lf.get("keys") else type_text(lf["old"])
            self.last_fix = None
            self.on_event("undo", "")
            return
        n = self._segment() if app == self.buf_app else 0
        log(f"caps: набрано {n} клавиш, раскладка будет определена")
        if n == 0:
            if self._fix_existing(app):
                return
            cur = self._lang()
            self._switch("en" if cur == "ru" else "ru")
            self.on_event("switch", "")
            return
        keys = self.buf[-n:]
        src = self._lang()
        tgt = "en" if src == "ru" else "ru"
        taps(VK_BACKSPACE, len(keys))
        if not self._switch(tgt):
            self.on_event("error", "Не нашёл вторую раскладку — добавь Русскую и Английскую в настройках клавиатуры")
            self._replay(keys)
            return
        time.sleep(0.08)  # даём macOS переключиться
        self._replay(keys)
        self.last_fix = {"app": app, "n": len(keys), "src": src, "keys": keys}
        self.on_event("fix", f"{len(keys)} симв.")

    def _fix_existing(self, app):
        """Фраза была в поле раньше — выделяем строку до курсора и переделываем её."""
        clip_quiet(3)
        wait_mods_released()
        saved = get_clipboard()
        text = None
        # 1) ⌘⇧← — выделить до начала строки (так работает почти везде в macOS)
        # 2) ⇧Home — запасной вариант для некоторых программ и игр
        for how, (code, flags) in (("cmd+shift+left", (VK_LEFT, kCGEventFlagMaskCommand | kCGEventFlagMaskShift)),
                                   ("shift+home", (115, kCGEventFlagMaskShift))):
            key(code, flags)
            time.sleep(0.04)
            text = copy_selection()
            log(f"fix_existing: {how} -> {'ничего' if text is None else str(len(text)) + ' симв.'}")
            if text:
                break
        if saved is not None:
            threading.Thread(target=lambda: (time.sleep(0.5), set_clipboard(saved)), daemon=True).start()
        if not text:
            return False
        if "\n" in text or len(text) > 500:
            key(VK_RIGHT)
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
        log(f"fix_existing: язык {tgt}, менять {'нечего' if (tgt is None or new == seg) else 'да'}")
        if tgt is None or new == seg:
            key(VK_RIGHT)
            return False
        type_text(prefix + new)   # печать поверх выделения заменяет его
        self._switch(tgt)
        self.reset()
        self.last_fix = {"app": app, "n": len(new), "src": "ru" if tgt == "en" else "en", "old": seg}
        self.on_event("fix", f"{seg.strip()} → {new.strip()}")
        return True

    def _convert_selection(self):
        clip_quiet(3)
        wait_mods_released(1.5)
        saved = get_clipboard()
        text = copy_selection()
        log(f"selection: {'ничего' if not text else str(len(text)) + ' симв.'}")
        if not text:
            self._fix()
            return
        new, tgt = convert_text(text)
        if tgt is None:
            if saved is not None:
                set_clipboard(saved)
            return
        set_clipboard(new)
        cmd(VK_V)
        self._switch(tgt)
        self.reset()
        self.on_event("selection", new[:40])
        if saved is not None:
            threading.Thread(target=lambda: (time.sleep(0.6), set_clipboard(saved)), daemon=True).start()
