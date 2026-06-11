"""Windows key-sending engine for Visual Pianos automation."""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from jjs_piano_studio.constants import (
    COARSE_WAIT_SECONDS,
    FINE_WAIT_SECONDS,
    HIGH_RES_TIMER_MS,
    KeyBinding,
)


def begin_high_resolution_timer() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        return int(ctypes.windll.winmm.timeBeginPeriod(HIGH_RES_TIMER_MS)) == 0
    except Exception:
        return False


def end_high_resolution_timer(enabled: bool) -> None:
    if not enabled or not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.winmm.timeEndPeriod(HIGH_RES_TIMER_MS)
    except Exception:
        pass


def wait_until_precise(
    deadline: float,
    stop_event: threading.Event,
    pause_event: threading.Event | None = None,
) -> bool:
    while True:
        if stop_event.is_set():
            return False
        if pause_event is not None and pause_event.is_set():
            return False
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return True
        if remaining > COARSE_WAIT_SECONDS:
            time.sleep(max(0.0, remaining - COARSE_WAIT_SECONDS * 0.5))
        elif remaining > FINE_WAIT_SECONDS:
            time.sleep(FINE_WAIT_SECONDS)
        else:
            time.sleep(0)

# ── Windows API structures ──────────────────────────────────────
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

SCAN_CODE_MAP = {
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07,
    "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14,
    "y": 0x15, "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22,
    "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "n": 0x31, "m": 0x32,
}

VIRTUAL_KEY_MAP = {
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35, "6": 0x36,
    "7": 0x37, "8": 0x38, "9": 0x39, "0": 0x30,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46,
    "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C,
    "m": 0x4D, "n": 0x4E, "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52,
    "s": 0x53, "t": 0x54, "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58,
    "y": 0x59, "z": 0x5A,
    "space": 0x20,
}

VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1


class WindowsScanCodeSender:
    """Sends key events via Win32 SendInput using scan codes."""

    def _send_key(self, base_key: str, is_key_up: bool, shifted: bool) -> None:
        scan_code = SCAN_CODE_MAP.get(base_key.lower())
        if scan_code is None:
            return
        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]
        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("ki", KeyboardInput)]
        flags = KEYEVENTF_SCANCODE
        if is_key_up:
            flags |= KEYEVENTF_KEYUP
        inp = Input(INPUT_KEYBOARD, KeyboardInput(0, scan_code, flags, 0, None))
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def key_down(self, binding: KeyBinding) -> None:
        if binding.shifted:
            self._send_shift(False)
        self._send_key(binding.base_key, False, binding.shifted)

    def key_up(self, binding: KeyBinding) -> None:
        self._send_key(binding.base_key, True, binding.shifted)
        if binding.shifted:
            self._send_shift(True)

    @staticmethod
    def _send_shift(release: bool) -> None:
        flags = KEYEVENTF_KEYUP if release else 0
        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]
        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("ki", KeyboardInput)]
        inp = Input(INPUT_KEYBOARD, KeyboardInput(VK_LSHIFT, 0, flags, 0, None))
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


class WindowsVkSender:
    """Sends key events via Win32 SendInput using virtual-key codes."""

    def _send_key(self, base_key: str, is_key_up: bool, shifted: bool) -> None:
        vk = VIRTUAL_KEY_MAP.get(base_key.lower())
        if vk is None:
            return
        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]
        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("ki", KeyboardInput)]
        flags = KEYEVENTF_KEYUP if is_key_up else 0
        inp = Input(INPUT_KEYBOARD, KeyboardInput(vk, 0, flags, 0, None))
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def key_down(self, binding: KeyBinding) -> None:
        if binding.shifted:
            self._send_shift(False)
        self._send_key(binding.base_key, False, binding.shifted)

    def key_up(self, binding: KeyBinding) -> None:
        self._send_key(binding.base_key, True, binding.shifted)
        if binding.shifted:
            self._send_shift(True)

    @staticmethod
    def _send_shift(release: bool) -> None:
        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]
        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("ki", KeyboardInput)]
        flags = KEYEVENTF_KEYUP if release else 0
        inp = Input(INPUT_KEYBOARD, KeyboardInput(VK_LSHIFT, 0, flags, 0, None))
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


class KeybdEventSender:
    """Sends key events via keybd_event API (most compatible)."""

    def key_down(self, binding: KeyBinding) -> None:
        if binding.shifted:
            ctypes.windll.user32.keybd_event(VK_LSHIFT, 0, 0, 0)
        vk = VIRTUAL_KEY_MAP.get(binding.base_key.lower())
        if vk is not None:
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)

    def key_up(self, binding: KeyBinding) -> None:
        vk = VIRTUAL_KEY_MAP.get(binding.base_key.lower())
        if vk is not None:
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        if binding.shifted:
            ctypes.windll.user32.keybd_event(VK_LSHIFT, 0, KEYEVENTF_KEYUP, 0)


class KeySender:
    """Thread-safe key press manager with shift-state tracking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._backend: Callable[[KeyBinding, bool], None] | None = None
        self._backend_up: Callable[[KeyBinding, bool], None] | None = None
        self._send_shift: Callable[[bool], None] | None = None
        self._active_keys: dict[str, int] = {}
        self._shift_active = False
        self.method = "Windows SendInput scan"
        self.set_method(self.method)

    def set_method(self, method: str) -> None:
        self.method = method
        if method == "Windows SendInput scan":
            sender = WindowsScanCodeSender()
            self._backend = sender.key_down
            self._backend_up = sender.key_up
            self._send_shift = None
        elif method == "Windows SendInput vk":
            sender = WindowsVkSender()
            self._backend = sender.key_down
            self._backend_up = sender.key_up
            self._send_shift = None
        elif method == "Windows keybd_event":
            sender = KeybdEventSender()
            self._backend = sender.key_down
            self._backend_up = sender.key_up
            self._send_shift = None
        elif method == "PyAutoGUI":
            try:
                import pyautogui
            except Exception:
                raise RuntimeError("PyAutoGUI is not installed.")
            self._backend = lambda b, _: pyautogui.keyDown(b.label)
            self._backend_up = lambda b, _: pyautogui.keyUp(b.label)
            self._send_shift = None
        else:
            raise ValueError(f"Unknown input method: {method!r}")

    def key_down(self, binding: KeyBinding) -> None:
        key = binding.label
        with self._lock:
            self._active_keys[key] = self._active_keys.get(key, 0) + 1
            shift_needs_send = binding.shifted and not self._shift_active
            self._shift_active = self._shift_active or binding.shifted
            if self._send_shift and shift_needs_send:
                self._send_shift(False)
            if self._backend:
                self._backend(binding, shift_needs_send)

    def key_up(self, binding: KeyBinding) -> None:
        key = binding.label
        with self._lock:
            count = self._active_keys.get(key, 0)
            if count <= 0:
                return
            count -= 1
            if count == 0:
                self._active_keys.pop(key, None)
            else:
                self._active_keys[key] = count
                return
            if self._backend_up:
                self._backend_up(binding, False)
            still_shifted = any(
                getattr(KeyBinding("a", "a", True), "shifted", False)
            )
            if not still_shifted and self._shift_active:
                no_more_shifted = all(
                    not (k in self._active_keys) or not self._is_shifted_label(k)
                    for k in self._active_keys
                )
                if no_more_shifted:
                    self._shift_active = False
                    if self._send_shift:
                        self._send_shift(True)

    @staticmethod
    def _is_shifted_label(label: str) -> bool:
        return label.isupper() or label in "!@#$%^&*()"

    def release_all(self) -> None:
        with self._lock:
            if self._shift_active:
                self._shift_active = False
                if self._send_shift:
                    self._send_shift(True)
            self._active_keys.clear()
