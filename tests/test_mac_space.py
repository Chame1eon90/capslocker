"""Exercise the real Mac callback with fake Quartz events; no native hooks or permissions."""
import ast
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest


class MacSpaceTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "mac" / "mac_input.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MacInput")
        callback = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_callback")
        constants = ("kCGEventTapDisabledByTimeout", "kCGEventTapDisabledByUserInput",
                     "kCGEventSourceUserData", "kCGEventLeftMouseDown", "kCGEventRightMouseDown",
                     "kCGEventOtherMouseDown", "kCGKeyboardEventKeycode", "kCGEventKeyDown",
                     "kCGEventKeyUp", "kCGEventFlagMaskShift")
        self.ns = {name: i + 1 for i, name in enumerate(constants)}
        self.ns.update(MARKER=100, VK_F18=79, VK_SPACE=49, VK_ESC=53, MODIFIER_KEYCODES=set(),
                       CGEventGetIntegerValueField=lambda ev, field: ev.get(field, 0),
                       CGEventGetFlags=lambda ev: 0,
                       Quartz=SimpleNamespace(kCGKeyboardEventAutorepeat=200))
        exec(compile(ast.Module(body=[callback], type_ignores=[]), str(path), "exec"), self.ns)
        self.e = SimpleNamespace(settings={"enabled": True, "auto": True}, q=queue.Queue(),
                                 capture_cb=None, _hotkey_slot=lambda code, flags: None)

    def event(self, code, down):
        event = {self.ns["kCGKeyboardEventKeycode"]: code}
        etype = self.ns["kCGEventKeyDown" if down else "kCGEventKeyUp"]
        result = self.ns["_callback"](self.e, None, etype, event, None)
        return event, result

    def test_double_space_passes_original_events(self):
        for down in (True, False, True, False):
            event, result = self.event(49, down)
            self.assertIs(result, event)
        self.assertEqual([self.e.q.get_nowait() for _ in range(2)], [("key", 49, 0)] * 2)
        self.assertTrue(self.e.q.empty())

    def test_typing_then_held_space_never_starts_auto_correction(self):
        for code, down in ((17, True), (31, True), (17, True),
                           (49, True), (49, True), (49, True), (49, False)):
            event, result = self.event(code, down)
            self.assertIs(result, event)
        self.assertEqual([self.e.q.get_nowait()[0] for _ in range(6)], ["key"] * 6)

    def test_capslock_still_dispatches_manual_action(self):
        _, result = self.event(79, True)
        self.assertIsNone(result)
        self.assertEqual(self.e.q.get_nowait(), ("caps", True, False))


if __name__ == "__main__":
    unittest.main()
