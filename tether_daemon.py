#!/usr/bin/env python3
"""
tether_daemon.py  —  Ubuntu side of tether (remote admin over Tailscale)

Runs on the Ubuntu machine. It:
  1. Streams the screen to the Mac via ffmpeg over TCP (Ubuntu listens).
  2. Accepts a control connection from the Mac and injects received
     keystrokes into Ubuntu via a kernel-level virtual keyboard (uinput).
  3. Monitors the *physical* keyboard for hotkey sequences:
         Esc x3  -> graceful exit
         Esc x5+ -> exit AND wipe runtime files
         Space x4 -> toggle chat mode (local typing streams to the Mac)
  4. In chat mode, local keystrokes are translated to text and streamed
     to the Mac instead of being treated as normal input.

Requires:  python3-evdev, ffmpeg  (plus wl-screenrec/wf-recorder on wlroots)
Run as root for first tests (needs /dev/input/* and /dev/uinput). See README
for the non-root udev + input-group setup.

    sudo python3 tether_daemon.py
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import random
import threading
import time

try:
    from evdev import InputDevice, UInput, categorize, ecodes, list_devices
except ImportError:
    sys.exit("Missing dependency: pip install evdev  (or apt install python3-evdev)")


# ----------------------------------------------------------------------------
# Config  (override any of these with env vars, e.g. TETHER_VIDEO_PORT=9100)
# ----------------------------------------------------------------------------
class Cfg:
    VIDEO_PORT   = int(os.environ.get("TETHER_VIDEO_PORT", 9000))
    CONTROL_PORT = int(os.environ.get("TETHER_CONTROL_PORT", 9001))
    BIND_ADDR    = os.environ.get("TETHER_BIND", "0.0.0.0")  # Tailscale iface is fine on 0.0.0.0

    FPS          = int(os.environ.get("TETHER_FPS", 30))
    CAPTURE      = os.environ.get("TETHER_CAPTURE", "auto")  # auto|x11|wlroots

    # Stream quality. Default LOW to cut latency; toggle to HIGH (native) from
    # the Mac with F9 for detail work. LOW scales to LOW_HEIGHT and caps bitrate.
    DEFAULT_QUALITY = os.environ.get("TETHER_QUALITY", "low")   # low|high
    LOW_HEIGHT      = int(os.environ.get("TETHER_LOW_HEIGHT", 720))
    LOW_MAXRATE     = os.environ.get("TETHER_LOW_MAXRATE", "2M")

    # Paste typing cadence (F7). Base delay between keystrokes + random jitter,
    # so pasted text types out like a person instead of dumping instantly.
    PASTE_DELAY  = float(os.environ.get("TETHER_PASTE_DELAY", 0.045))   # base seconds/char
    PASTE_JITTER = float(os.environ.get("TETHER_PASTE_JITTER", 0.05))   # extra random 0..J

    # Hotkey tuning
    SEQ_WINDOW   = float(os.environ.get("TETHER_SEQ_WINDOW", 1.2))  # seconds between presses in a run
    ESC_GRACEFUL = 3
    ESC_WIPE     = 5
    SPACE_CHAT   = 4

    # How "cmd" from the Mac is mapped on Linux: "super" (literal) or "ctrl"
    CMD_MAPS_TO  = os.environ.get("TETHER_CMD", "super")

    RUNTIME_DIR  = os.path.expanduser(os.environ.get("TETHER_DIR", "~/.tether"))
    LOG_FILE     = os.path.join(RUNTIME_DIR, "tether.log")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(Cfg.LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------
# evdev keycode <-> character tables (US layout, good enough for v1)
# ----------------------------------------------------------------------------
# name/char coming from the Mac -> base evdev key. Shift is sent separately
# by the Mac as its own modifier event, so shifted symbols map to their base.
NAME_TO_KEY = {
    "enter": ecodes.KEY_ENTER, "return": ecodes.KEY_ENTER,
    "space": ecodes.KEY_SPACE, "tab": ecodes.KEY_TAB,
    "backspace": ecodes.KEY_BACKSPACE, "delete": ecodes.KEY_DELETE,
    "esc": ecodes.KEY_ESC, "escape": ecodes.KEY_ESC,
    "up": ecodes.KEY_UP, "down": ecodes.KEY_DOWN,
    "left": ecodes.KEY_LEFT, "right": ecodes.KEY_RIGHT,
    "home": ecodes.KEY_HOME, "end": ecodes.KEY_END,
    "page_up": ecodes.KEY_PAGEUP, "page_down": ecodes.KEY_PAGEDOWN,
    "insert": ecodes.KEY_INSERT, "caps_lock": ecodes.KEY_CAPSLOCK,
    "shift": ecodes.KEY_LEFTSHIFT, "shift_r": ecodes.KEY_RIGHTSHIFT,
    "ctrl": ecodes.KEY_LEFTCTRL, "ctrl_r": ecodes.KEY_RIGHTCTRL,
    "alt": ecodes.KEY_LEFTALT, "alt_r": ecodes.KEY_RIGHTALT,
    "super": ecodes.KEY_LEFTMETA,
    "f1": ecodes.KEY_F1, "f2": ecodes.KEY_F2, "f3": ecodes.KEY_F3,
    "f4": ecodes.KEY_F4, "f5": ecodes.KEY_F5, "f6": ecodes.KEY_F6,
    "f7": ecodes.KEY_F7, "f8": ecodes.KEY_F8, "f9": ecodes.KEY_F9,
    "f10": ecodes.KEY_F10, "f11": ecodes.KEY_F11, "f12": ecodes.KEY_F12,
}
# "cmd" is resolved at runtime based on Cfg.CMD_MAPS_TO
def resolve_cmd():
    return ecodes.KEY_LEFTCTRL if Cfg.CMD_MAPS_TO == "ctrl" else ecodes.KEY_LEFTMETA

# printable char (base or shifted) -> base evdev key
_CHAR_ROWS = {
    "1234567890-=": [ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
                     ecodes.KEY_5, ecodes.KEY_6, ecodes.KEY_7, ecodes.KEY_8,
                     ecodes.KEY_9, ecodes.KEY_0, ecodes.KEY_MINUS, ecodes.KEY_EQUAL],
    "!@#$%^&*()_+": [ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
                     ecodes.KEY_5, ecodes.KEY_6, ecodes.KEY_7, ecodes.KEY_8,
                     ecodes.KEY_9, ecodes.KEY_0, ecodes.KEY_MINUS, ecodes.KEY_EQUAL],
    "qwertyuiop[]\\": [ecodes.KEY_Q, ecodes.KEY_W, ecodes.KEY_E, ecodes.KEY_R,
                       ecodes.KEY_T, ecodes.KEY_Y, ecodes.KEY_U, ecodes.KEY_I,
                       ecodes.KEY_O, ecodes.KEY_P, ecodes.KEY_LEFTBRACE,
                       ecodes.KEY_RIGHTBRACE, ecodes.KEY_BACKSLASH],
    "asdfghjkl;'": [ecodes.KEY_A, ecodes.KEY_S, ecodes.KEY_D, ecodes.KEY_F,
                    ecodes.KEY_G, ecodes.KEY_H, ecodes.KEY_J, ecodes.KEY_K,
                    ecodes.KEY_L, ecodes.KEY_SEMICOLON, ecodes.KEY_APOSTROPHE],
    "zxcvbnm,./": [ecodes.KEY_Z, ecodes.KEY_X, ecodes.KEY_C, ecodes.KEY_V,
                   ecodes.KEY_B, ecodes.KEY_N, ecodes.KEY_M, ecodes.KEY_COMMA,
                   ecodes.KEY_DOT, ecodes.KEY_SLASH],
    "`": [ecodes.KEY_GRAVE],
    "~": [ecodes.KEY_GRAVE],
}
CHAR_TO_KEY = {}
for chars, keys in _CHAR_ROWS.items():
    for ch, kc in zip(chars, keys):
        CHAR_TO_KEY[ch] = kc
for ch in "QWERTYUIOPASDFGHJKLZXCVBNM":
    CHAR_TO_KEY[ch] = CHAR_TO_KEY[ch.lower()]
CHAR_TO_KEY[" "] = ecodes.KEY_SPACE

# reverse: evdev key (+shift) -> character, for CHAT mode text streaming
KEY_TO_CHAR = {}
KEY_TO_CHAR_SHIFT = {}
for chars, keys in _CHAR_ROWS.items():
    for ch, kc in zip(chars, keys):
        if ch in "!@#$%^&*()_+~" or ch.isupper():
            KEY_TO_CHAR_SHIFT.setdefault(kc, ch)
        else:
            KEY_TO_CHAR.setdefault(kc, ch)
for ch in "abcdefghijklmnopqrstuvwxyz":
    KEY_TO_CHAR_SHIFT[CHAR_TO_KEY[ch]] = ch.upper()
KEY_TO_CHAR[ecodes.KEY_SPACE] = " "
KEY_TO_CHAR_SHIFT[ecodes.KEY_SPACE] = " "

# Complete char -> (keycode, needs_shift) table for typing clipboard text
# (the PASTE feature). Unlike live keys from the Mac — which carry their own
# shift events — pasted text must synthesize shift for capitals and symbols.
_TYPE_KEYS = [
    ("1", "!", ecodes.KEY_1), ("2", "@", ecodes.KEY_2), ("3", "#", ecodes.KEY_3),
    ("4", "$", ecodes.KEY_4), ("5", "%", ecodes.KEY_5), ("6", "^", ecodes.KEY_6),
    ("7", "&", ecodes.KEY_7), ("8", "*", ecodes.KEY_8), ("9", "(", ecodes.KEY_9),
    ("0", ")", ecodes.KEY_0), ("-", "_", ecodes.KEY_MINUS), ("=", "+", ecodes.KEY_EQUAL),
    ("[", "{", ecodes.KEY_LEFTBRACE), ("]", "}", ecodes.KEY_RIGHTBRACE),
    ("\\", "|", ecodes.KEY_BACKSLASH), (";", ":", ecodes.KEY_SEMICOLON),
    ("'", '"', ecodes.KEY_APOSTROPHE), (",", "<", ecodes.KEY_COMMA),
    (".", ">", ecodes.KEY_DOT), ("/", "?", ecodes.KEY_SLASH),
    ("`", "~", ecodes.KEY_GRAVE),
]
TYPE_MAP = {" ": (ecodes.KEY_SPACE, False)}
for _lo, _hi, _kc in _TYPE_KEYS:
    TYPE_MAP[_lo] = (_kc, False)
    TYPE_MAP[_hi] = (_kc, True)
for _c in "abcdefghijklmnopqrstuvwxyz":
    TYPE_MAP[_c] = (CHAR_TO_KEY[_c], False)
    TYPE_MAP[_c.upper()] = (CHAR_TO_KEY[_c], True)


# ----------------------------------------------------------------------------
# Virtual keyboard (injection target for Mac keystrokes)
# ----------------------------------------------------------------------------
VIRTUAL_NAME = "tether-virtual-kbd"
VIRTUAL_MOUSE_NAME = "tether-virtual-mouse"

BUTTONS = {
    "left": ecodes.BTN_LEFT,
    "right": ecodes.BTN_RIGHT,
    "middle": ecodes.BTN_MIDDLE,
}

class VirtualKeyboard:
    def __init__(self):
        caps = {ecodes.EV_KEY: list(range(1, 256))}
        self.ui = UInput(caps, name=VIRTUAL_NAME)
        self._lock = threading.Lock()   # live keys and paste-typing share the device
        log(f"virtual keyboard created: {VIRTUAL_NAME}")

    def press(self, code, value):
        # value: 1 down, 0 up, 2 autorepeat
        with self._lock:
            self.ui.write(ecodes.EV_KEY, code, value)
            self.ui.syn()

    def _tap(self, code, shift=False):
        if shift:
            self.ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1); self.ui.syn()
        self.ui.write(ecodes.EV_KEY, code, 1); self.ui.syn()
        self.ui.write(ecodes.EV_KEY, code, 0); self.ui.syn()
        if shift:
            self.ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0); self.ui.syn()

    def type_text(self, text):
        """Type a string by synthesizing key events (used by the PASTE feature).
        Runs on its own thread; the lock keeps it from interleaving with live
        keystrokes. Keys land with a human-like cadence (base delay + jitter,
        plus a small beat after spaces/punctuation) so it reads as typing rather
        than an instant dump."""
        for ch in text:
            with self._lock:
                if ch in ("\n", "\r"):
                    self._tap(ecodes.KEY_ENTER)
                elif ch == "\t":
                    self._tap(ecodes.KEY_TAB)
                else:
                    entry = TYPE_MAP.get(ch)
                    if entry is None:
                        continue            # skip chars we can't map (US layout); no pause
                    self._tap(entry[0], entry[1])
            # cadence: base + random jitter, with a longer beat at word/line
            # breaks and after punctuation, the way real typing ebbs and flows.
            delay = Cfg.PASTE_DELAY + random.uniform(0.0, Cfg.PASTE_JITTER)
            if ch in " \n\t":
                delay += Cfg.PASTE_DELAY * 0.8
            elif ch in ".,;:!?":
                delay += Cfg.PASTE_DELAY * 1.5
            time.sleep(delay)

    def close(self):
        try:
            self.ui.close()
        except Exception:
            pass


class VirtualMouse:
    """Relative pointing device: REL_X/Y movement, buttons, scroll wheels."""
    def __init__(self):
        caps = {
            ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y,
                            ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
            ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
        }
        self.ui = UInput(caps, name=VIRTUAL_MOUSE_NAME)
        log(f"virtual mouse created: {VIRTUAL_MOUSE_NAME}")

    def move(self, dx, dy):
        if dx:
            self.ui.write(ecodes.EV_REL, ecodes.REL_X, dx)
        if dy:
            self.ui.write(ecodes.EV_REL, ecodes.REL_Y, dy)
        self.ui.syn()

    def button(self, name, value):
        code = BUTTONS.get(name)
        if code is None:
            return
        self.ui.write(ecodes.EV_KEY, code, value)
        self.ui.syn()

    def scroll(self, dx, dy):
        if dy:
            self.ui.write(ecodes.EV_REL, ecodes.REL_WHEEL, dy)
        if dx:
            self.ui.write(ecodes.EV_REL, ecodes.REL_HWHEEL, dx)
        self.ui.syn()

    def close(self):
        try:
            self.ui.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Mode / hotkey state machine
# ----------------------------------------------------------------------------
class Modes:
    def __init__(self, daemon):
        self.d = daemon
        self.chat = False
        self._run_key = None
        self._run_count = 0
        self._last_ts = 0.0

    def feed(self, code, name):
        """Called for every physical key-DOWN. Detects Esc/Space runs."""
        now = time.monotonic()
        trigger = code in (ecodes.KEY_ESC, ecodes.KEY_SPACE)

        # Continue an existing run?
        if trigger and code == self._run_key and (now - self._last_ts) <= Cfg.SEQ_WINDOW:
            self._run_count += 1
            self._last_ts = now
            return
        # A new key (or a different trigger) ends any pending run first.
        self._finalize_run()
        if trigger:
            self._run_key = code
            self._run_count = 1
            self._last_ts = now
        else:
            self._run_key = None
            self._run_count = 0

    def tick(self):
        """Called periodically; finalizes a run once the window lapses."""
        if self._run_key is not None and (time.monotonic() - self._last_ts) > Cfg.SEQ_WINDOW:
            self._finalize_run()

    def _finalize_run(self):
        key, count = self._run_key, self._run_count
        self._run_key, self._run_count = None, 0
        if key is None or count == 0:
            return
        if key == ecodes.KEY_ESC:
            if count >= Cfg.ESC_WIPE:
                log(f"Esc x{count} -> EXIT + WIPE")
                self.d.shutdown(wipe=True)
            elif count == Cfg.ESC_GRACEFUL:
                log(f"Esc x{count} -> graceful exit")
                self.d.shutdown(wipe=False)
        elif key == ecodes.KEY_SPACE:
            if count == Cfg.SPACE_CHAT:
                self.chat = not self.chat
                state = "ON" if self.chat else "OFF"
                log(f"Space x{count} -> chat mode {state}")
                self.d.control.send({"t": "mode", "chat": self.chat})


# ----------------------------------------------------------------------------
# Physical keyboard monitor (hotkeys + chat capture)
# ----------------------------------------------------------------------------
class KeyboardMonitor(threading.Thread):
    def __init__(self, daemon):
        super().__init__(daemon=True)
        self.d = daemon
        self.devices = self._find_keyboards()
        self.shift = False

    def _find_keyboards(self):
        found = []
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if dev.name in (VIRTUAL_NAME, VIRTUAL_MOUSE_NAME):  # skip our own injectors
                continue
            caps = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_ESC in caps and ecodes.KEY_A in caps:
                found.append(dev)
                log(f"monitoring keyboard: {dev.name} ({path})")
        if not found:
            log("WARNING: no physical keyboard found to monitor")
        return found

    def run(self):
        from select import select
        fds = {d.fd: d for d in self.devices}
        while self.d.running:
            r, _, _ = select(fds, [], [], 0.2)
            self.d.modes.tick()
            for fd in r:
                for event in fds[fd].read():
                    if event.type != ecodes.EV_KEY:
                        continue
                    k = categorize(event)
                    code, val = event.code, event.value  # val 1 down, 0 up, 2 repeat
                    if code in (ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT):
                        self.shift = val in (1, 2)
                    if val == 1:  # key down
                        self.d.modes.feed(code, k.keycode)
                        if self.d.modes.chat:
                            self._chat_char(code)

    def _chat_char(self, code):
        table = KEY_TO_CHAR_SHIFT if self.shift else KEY_TO_CHAR
        if code == ecodes.KEY_ENTER:
            self.d.control.send({"t": "chat", "char": "\n"})
        elif code == ecodes.KEY_BACKSPACE:
            self.d.control.send({"t": "chat", "char": "\b"})
        elif code in table:
            self.d.control.send({"t": "chat", "char": table[code]})


# ----------------------------------------------------------------------------
# Screen capture via ffmpeg (Ubuntu LISTENS; Mac's ffplay connects)
# ----------------------------------------------------------------------------
class Capture(threading.Thread):
    def __init__(self, daemon):
        super().__init__(daemon=True)
        self.d = daemon
        self.proc = None
        self._restart = False          # set by a quality switch to re-exec capture

    def _backend(self):
        mode = Cfg.CAPTURE
        if mode == "auto":
            session = os.environ.get("XDG_SESSION_TYPE", "").lower()
            # Check Wayland FIRST: on Hyprland/Sway, Xwayland also sets DISPLAY,
            # so a truthy DISPLAY does NOT mean an X11 session.
            if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
                mode = "wlroots"
            elif session == "x11" or os.environ.get("DISPLAY"):
                mode = "x11"
            else:
                mode = "x11"
        return mode

    def _x264_opts(self, low):
        """ffmpeg x264 output args, shared by x11 and wlroots-low paths."""
        opts = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", str(Cfg.FPS), "-an"]
        if low:
            # scale down + cap bitrate — this is the latency win
            opts = ["-vf", f"scale=-2:{Cfg.LOW_HEIGHT}"] + opts
            opts += ["-crf", "30", "-maxrate", Cfg.LOW_MAXRATE, "-bufsize", "1M"]
        else:
            opts += ["-crf", "20"]
        return opts

    def _command(self):
        url = f"tcp://{Cfg.BIND_ADDR}:{Cfg.VIDEO_PORT}?listen=1"
        backend = self._backend()
        low = (self.d.quality == "low")
        if backend == "x11":
            disp = os.environ.get("DISPLAY", ":0")
            enc = self._x264_opts(low) + ["-f", "mpegts", url]
            return (["ffmpeg", "-loglevel", "warning", "-f", "x11grab",
                     "-framerate", str(Cfg.FPS), "-draw_mouse", "1",
                     "-i", disp] + enc), False
        elif backend == "wlroots":
            # wlroots grabbers (Hyprland/Sway) encode H.264 themselves. Neither
            # has a literal --stdout flag — you write to /dev/stdout AND force the
            # muxer, since the container can't be guessed from that path. mpegts
            # because the pipe is non-seekable.
            if shutil.which("wl-screenrec"):
                grab = "wl-screenrec --codec avc --ffmpeg-muxer mpegts -f /dev/stdout"
            elif shutil.which("wf-recorder"):
                grab = (f"wf-recorder --codec libx264 --muxer=mpegts "
                        f"--framerate {Cfg.FPS} --file=/dev/stdout")
            else:
                log("ERROR: Wayland (wlroots) session but neither wl-screenrec nor "
                    "wf-recorder is installed.")
                log("       Install one (see README) or set TETHER_CAPTURE=x11.")
                return None, False
            if low:
                # LOW: ffmpeg re-encodes to scale down + cap bitrate. Costs a
                # decode+encode on this box, but shrinks what crosses the wire —
                # the latency win when the link, not the CPU, is the bottleneck.
                tail = " ".join(self._x264_opts(low=True)) + f" -f mpegts {url}"
            else:
                # HIGH: zero-copy remux of the grabber's native-res H.264.
                tail = f"-c copy -f mpegts {url}"
            # stderr left visible while debugging; add '2>/dev/null' after {grab}
            # once capture is reliable (see README troubleshooting).
            pipe = (f"{grab} | ffmpeg -loglevel warning -fflags nobuffer -i - {tail}")
            return pipe, True
        return None, False

    def _kill_proc(self, grace=3.0):
        """Kill the current capture and its whole process group, and wait for the
        OS to release port 9000 before returning."""
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.terminate()
            except OSError:
                pass
        try:
            p.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    p.kill()
                except OSError:
                    pass

    def restart(self):
        """Force the capture to re-exec with current settings (quality switch)."""
        self._restart = True
        self._kill_proc()

    def run(self):
        if self._backend() == "wlroots" and os.geteuid() == 0:
            log("WARNING: Wayland capture under sudo usually fails — the compositor "
                "socket is in your user's XDG_RUNTIME_DIR, unreachable as root. "
                "Run as your user (do the README non-root setup first).")
        fast_fails = 0
        ever_ran = False               # has capture ever streamed successfully?
        while self.d.running:
            cmd, shell = self._command()
            if cmd is None:
                self.d.running = False
                return
            log(f"ffmpeg listening for viewer on tcp:{Cfg.VIDEO_PORT} "
                f"(backend={self._backend()})")
            started = time.monotonic()
            try:
                # start_new_session=True puts the capture (and, for the wlroots
                # shell pipeline, BOTH wf-recorder and ffmpeg) in its own process
                # group, so restart()/stop() can kill the whole group. Without
                # this, terminating a shell pipeline orphans ffmpeg, which keeps
                # holding port 9000 and breaks the next capture / quality switch.
                self.proc = subprocess.Popen(
                    cmd, shell=shell,
                    stdin=subprocess.PIPE if shell else None,
                    start_new_session=True)
                if shell and self.proc.stdin:
                    # wf-recorder asks "Output file /dev/stdout exists. Overwrite?"
                    # Answer it so capture never blocks on the terminal. (Harmless
                    # for wl-screenrec, which doesn't read stdin.)
                    try:
                        self.proc.stdin.write(b"y\n")
                        self.proc.stdin.flush()
                        self.proc.stdin.close()
                    except OSError:
                        pass
                self.proc.wait()
            except Exception as e:
                # Never let an unexpected error kill the capture thread — the
                # daemon must stay alive and keep re-listening for new clients.
                log(f"capture error: {e!r} — retrying in 1s")
                time.sleep(1)
                continue
            if not self.d.running:
                break
            # An intentional quality switch terminated the proc — re-exec at once
            # with the new settings, don't treat it as a crash.
            if self._restart:
                self._restart = False
                fast_fails = 0
                log(f"switching stream to {self.d.quality} quality — restarting capture")
                time.sleep(0.5)        # let the socket + old viewer fully clear
                continue
            ran_for = time.monotonic() - started
            if ran_for >= 2.0:
                # A real streaming session happened (a viewer connected, watched,
                # then left — e.g. Cmd+Q on the Mac). Normal. Reset and relisten.
                ever_ran = True
                fast_fails = 0
                log("viewer disconnected — re-listening for a new connection")
                time.sleep(1)
                continue
            # Exited in under 2s. Before the FIRST successful stream this means
            # capture can't start (bad env); after it, it's just a client that
            # connected and left quickly — so NEVER permanently give up once
            # capture has worked. The daemon keeps running for the next client.
            if ever_ran:
                time.sleep(1)
                continue
            fast_fails += 1
            if fast_fails >= 3:
                log("ERROR: capture keeps failing immediately at startup — "
                    "giving up on video (daemon stays up for input/chat).")
                if self._backend() == "x11":
                    log("  X11 hint: running under sudo usually can't reach your "
                        "X server ('Authorization required'). Run as your user "
                        "(see README non-root setup), or once per session: "
                        "xhost +SI:localuser:root")
                else:
                    log("  Wayland hint: run as your user (not sudo) so "
                        "WAYLAND_DISPLAY/XDG_RUNTIME_DIR are set, and install "
                        "wf-recorder or wl-screenrec.")
                return
            time.sleep(1)

    def stop(self):
        self._kill_proc()


# ----------------------------------------------------------------------------
# Control channel server (Mac keys IN, chat/mode notices OUT)
# ----------------------------------------------------------------------------
class ControlServer(threading.Thread):
    def __init__(self, daemon):
        super().__init__(daemon=True)
        self.d = daemon
        self.sock = None
        self.conn = None
        self._lock = threading.Lock()

    def send(self, obj):
        with self._lock:
            if not self.conn:
                return
            try:
                self.conn.sendall((json.dumps(obj) + "\n").encode())
            except OSError:
                self.conn = None

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((Cfg.BIND_ADDR, Cfg.CONTROL_PORT))
        self.sock.listen(1)
        log(f"control server listening on tcp:{Cfg.CONTROL_PORT}")
        while self.d.running:
            try:
                self.sock.settimeout(1.0)
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log(f"Mac connected from {addr[0]}")
            with self._lock:
                self.conn = conn
            self._recv_loop(conn)
            log("Mac disconnected")
            with self._lock:
                self.conn = None

    def _recv_loop(self, conn):
        buf = b""
        conn.settimeout(1.0)
        while self.d.running:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    self._dispatch(line)

    # NB: not named _handle — Python 3.13's threading.Thread has its own
    # self._handle (_thread._ThreadHandle), which would shadow this method and
    # cause "'_thread._ThreadHandle' object is not callable" when invoked.
    def _dispatch(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return
        t = msg.get("t")
        if t == "key":
            val = int(msg.get("val", 1))
            code = None
            if "code" in msg:               # raw evdev keycode (Linux client)
                code = int(msg["code"])
            elif "name" in msg:             # named key (Mac client)
                nm = msg["name"]
                code = resolve_cmd() if nm == "cmd" else NAME_TO_KEY.get(nm)
            elif "char" in msg:             # printable char (Mac client)
                code = CHAR_TO_KEY.get(msg["char"])
            if code is not None:
                self.d.vkbd.press(code, val)
        elif t == "mmove":
            self.d.vmouse.move(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
        elif t == "mbtn":
            self.d.vmouse.button(msg.get("btn"), int(msg.get("val", 1)))
        elif t == "mscroll":
            self.d.vmouse.scroll(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
        elif t == "quality":
            self.d.set_quality(msg.get("level"))
        elif t == "paste":
            text = msg.get("text", "")
            if text:
                # type on its own thread so a long paste doesn't stall live input
                threading.Thread(target=self.d.vkbd.type_text,
                                 args=(text,), daemon=True).start()

    def stop(self):
        try:
            if self.conn:
                self.conn.close()
            if self.sock:
                self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------------
# Daemon
# ----------------------------------------------------------------------------
class Daemon:
    def __init__(self):
        os.makedirs(Cfg.RUNTIME_DIR, exist_ok=True)
        self.running = True
        self.quality = Cfg.DEFAULT_QUALITY if Cfg.DEFAULT_QUALITY in ("low", "high") else "low"
        self.vkbd = VirtualKeyboard()
        self.vmouse = VirtualMouse()
        self.modes = Modes(self)
        self.monitor = KeyboardMonitor(self)
        self.capture = Capture(self)
        self.control = ControlServer(self)

    def set_quality(self, level):
        if level not in ("low", "high") or level == self.quality:
            return
        self.quality = level
        log(f"quality -> {level} (requested from Mac)")
        self.capture.restart()

    def start(self):
        log("tether daemon starting")
        self.control.start()
        self.capture.start()
        self.monitor.start()
        signal.signal(signal.SIGINT, lambda *_: self.shutdown(False))
        signal.signal(signal.SIGTERM, lambda *_: self.shutdown(False))
        # Survive the launching terminal closing (SIGHUP) — so a foreground run
        # keeps going if you close the shell. Esc-hotkeys / SIGTERM still stop it.
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        log(f"ready. quality={self.quality}. hotkeys: Esc x{Cfg.ESC_GRACEFUL}=quit, "
            f"Esc x{Cfg.ESC_WIPE}=quit+wipe, Space x{Cfg.SPACE_CHAT}=chat toggle")
        while self.running:
            time.sleep(0.3)

    def shutdown(self, wipe=False):
        if not self.running:
            return
        self.running = False
        log("shutting down...")
        self.capture.stop()
        self.control.stop()
        self.vkbd.close()
        self.vmouse.close()
        if wipe:
            log(f"wiping runtime dir {Cfg.RUNTIME_DIR}")
            shutil.rmtree(Cfg.RUNTIME_DIR, ignore_errors=True)
        log("bye")
        os._exit(0)


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("warning: not root — if evdev/uinput access fails, run with sudo "
              "or set up the udev rule (see README).", file=sys.stderr)
    Daemon().start()
