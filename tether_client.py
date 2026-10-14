#!/usr/bin/env python3
"""
tether_client.py  —  Mac side of tether

Runs on the Mac. It:
  1. Launches ffplay to show the Ubuntu screen stream (low-latency).
  2. Connects to the Ubuntu control channel.
  3. When ARMED (toggle with F8), forwards keyboard AND trackpad/mouse to
     Ubuntu, suppressing them locally so your Mac isn't driven at the same
     time. Disarmed by default.
  4. When Ubuntu enters chat mode (Space x4 on the Ubuntu keyboard), a chat
     window opens here showing what the Ubuntu user types; it closes and is
     cleared when chat mode is toggled back off.

Usage:
    python3 tether_client.py <ubuntu-tailscale-ip>

Needs:  pynput  (pip install pynput)  and  ffmpeg  (for ffplay).
Chat window needs Tk: python.org/conda Python include it; with Homebrew do
    brew install python-tk
macOS will prompt for Accessibility + Input Monitoring the first time you
arm forwarding — grant both to your terminal app, then re-run.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

try:
    from pynput import keyboard, mouse
except ImportError:
    sys.exit("Missing dependency: pip install pynput")


def _patch_pyobjc_trust():
    """Work around pynput crashing on bleeding-edge Python (e.g. 3.15).

    pynput does `import HIServices; HIServices.AXIsProcessTrusted()` to check the
    macOS Accessibility permission. On a Python that pyobjc doesn't yet ship
    metadata for, pyobjc's lazy loader can't resolve that symbol and throws
    `KeyError: 'AXIsProcessTrusted'`, killing the listener thread — so input
    forwarding silently dies. We load the function straight from the
    ApplicationServices framework via ctypes (no pyobjc metadata involved) and
    splice it into the exact name pynput reads. No-op if pyobjc already works,
    or off macOS."""
    if sys.platform != "darwin":
        return
    try:
        from pynput._util import darwin as _d
    except Exception:
        return
    try:
        _d.HIServices.AXIsProcessTrusted          # already resolvable? leave it.
        return
    except Exception:
        pass
    try:
        import ctypes, ctypes.util
        fw = (ctypes.util.find_library("ApplicationServices")
              or "/System/Library/Frameworks/ApplicationServices.framework/"
                 "ApplicationServices")
        lib = ctypes.CDLL(fw)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        lib.AXIsProcessTrusted.argtypes = []
        _real = _d.HIServices

        class _HIShim:
            def AXIsProcessTrusted(self):
                return bool(lib.AXIsProcessTrusted())
            def __getattr__(self, name):
                return getattr(_real, name)   # delegate anything else to pyobjc

        _d.HIServices = _HIShim()
        print("[tether] applied pyobjc/AXIsProcessTrusted compatibility shim")
    except Exception as e:
        print(f"[tether] could not apply pyobjc shim ({e!r}); if the listener "
              f"fails, use Python 3.13 (see README).")


_patch_pyobjc_trust()

# Set TETHER_NO_TK=1 to force terminal chat even when Tk is importable (escape
# hatch for the macOS Tk/pynput Cocoa clash).
try:
    import tkinter as tk
    HAVE_TK = not os.environ.get("TETHER_NO_TK")
except ImportError:
    HAVE_TK = False

# macOS Quartz lets us read RAW mouse deltas off the event, instead of diffing
# the cursor's absolute position — which stalls the moment the cursor is frozen
# or hits a screen edge. This is the fix for "mouse won't move past the edges".
try:
    import Quartz
    HAVE_QUARTZ = True
    _MOVE_TYPES = {Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged,
                   Quartz.kCGEventRightMouseDragged, Quartz.kCGEventOtherMouseDragged}
except Exception:
    HAVE_QUARTZ = False
    _MOVE_TYPES = set()

VIDEO_PORT    = 9000
CONTROL_PORT  = 9001
ARM_KEY       = keyboard.Key.f8   # toggle keyboard+mouse forwarding on/off
QUALITY_KEY   = keyboard.Key.f9   # toggle stream quality low <-> high (native)
PASTE_KEY     = keyboard.Key.f7   # type the Mac clipboard into Linux
SUPPRESS_MOUSE = True             # freeze Mac cursor while armed (best-effort)
MOUSE_SENS     = 1.0              # multiplier for forwarded mouse movement


# pynput special keys -> the "name" the Ubuntu daemon understands
SPECIAL = {
    keyboard.Key.enter: "enter", keyboard.Key.space: "space",
    keyboard.Key.tab: "tab", keyboard.Key.backspace: "backspace",
    keyboard.Key.delete: "delete", keyboard.Key.esc: "esc",
    keyboard.Key.up: "up", keyboard.Key.down: "down",
    keyboard.Key.left: "left", keyboard.Key.right: "right",
    keyboard.Key.home: "home", keyboard.Key.end: "end",
    keyboard.Key.page_up: "page_up", keyboard.Key.page_down: "page_down",
    keyboard.Key.caps_lock: "caps_lock",
    keyboard.Key.shift: "shift", keyboard.Key.shift_r: "shift_r",
    keyboard.Key.ctrl: "ctrl", keyboard.Key.ctrl_r: "ctrl_r",
    keyboard.Key.alt: "alt", keyboard.Key.alt_r: "alt_r",
    keyboard.Key.cmd: "cmd", keyboard.Key.cmd_r: "cmd",
    keyboard.Key.f1: "f1", keyboard.Key.f2: "f2", keyboard.Key.f3: "f3",
    keyboard.Key.f4: "f4", keyboard.Key.f5: "f5", keyboard.Key.f6: "f6",
    keyboard.Key.f10: "f10", keyboard.Key.f11: "f11", keyboard.Key.f12: "f12",
    # NB: F7/F8/F9 are tether controls (paste/arm/quality), not forwarded.
}
MOUSE_BUTTONS = {
    mouse.Button.left: "left", mouse.Button.right: "right",
    mouse.Button.middle: "middle",
}


class Client:
    def __init__(self, host):
        self.host = host
        self.armed = False
        self.running = False
        self.sock = None
        self.lock = threading.Lock()
        self.q = queue.Queue()          # socket thread -> UI thread
        self._last_mouse = None         # last (x, y) for relative deltas (fallback)
        self._mac_delta = HAVE_QUARTZ   # read raw deltas from the Quartz event
        self.quality = "low"            # mirrors the daemon's default
        self._pending_restart = False   # a quality toggle will drop/replace video
        self.video = None
        self.root = None
        self.chat_win = None
        self.chat_text = None

    # -- control channel -----------------------------------------------------
    def _control_loop(self):
        """Keep a control connection up, reconnecting if the daemon restarts."""
        while self.running:
            try:
                sock = socket.create_connection((self.host, CONTROL_PORT), timeout=5)
            except OSError:
                time.sleep(2)
                continue
            with self.lock:
                self.sock = sock
            print(f"[tether] control connected to {self.host}:{CONTROL_PORT}")
            self._recv_until_closed(sock)
            with self.lock:
                self.sock = None
            if self.running:
                print("[tether] control dropped — reconnecting…")
                time.sleep(1)

    def _recv_until_closed(self, sock):
        buf = b""
        sock.settimeout(1.0)
        while self.running:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._on_msg(line)

    def _on_msg(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if msg.get("t") == "chat":
            self.q.put(("chat", msg.get("char", "")))
        elif msg.get("t") == "mode":
            on = bool(msg.get("chat"))
            self.q.put(("mode", on))
            print(f"[tether] ubuntu chat mode {'ON' if on else 'OFF'}")

    def send(self, obj):
        with self.lock:
            if not self.sock:
                return
            try:
                self.sock.sendall((json.dumps(obj) + "\n").encode())
            except OSError:
                self.sock = None

    # -- video ---------------------------------------------------------------
    def start_video(self):
        url = f"tcp://{self.host}:{VIDEO_PORT}"
        # -autoexit is essential: without it ffplay freezes on the last frame at
        # end-of-stream instead of quitting, so the reconnect loop never runs.
        # That end-of-stream happens on every quality switch (F9) and whenever
        # the daemon re-execs capture — ffplay must exit so we can reconnect.
        cmd = ["ffplay", "-loglevel", "error", "-autoexit",
               "-fflags", "nobuffer", "-flags", "low_delay", "-framedrop",
               "-probesize", "32", "-sync", "ext",
               "-window_title", "tether — ubuntu", url]
        return subprocess.Popen(cmd)

    def _video_loop(self):
        """Keep ffplay connected to the daemon's video stream.

        ffplay IS the connection — we never pre-probe the port, because the
        daemon's ffmpeg '-listen 1' serves exactly one connection, and a probe
        would consume it and break the real stream. If ffplay exits almost
        immediately it was refused (capture still warming up) → retry; if it
        ran a while, you closed the window → quit.
        """
        print(f"[tether] connecting video to {self.host}:{VIDEO_PORT} "
              f"(capture can take a few seconds to warm up)…")
        while self.running:
            started = time.monotonic()
            self.video = self.start_video()
            self.video.wait()
            if not self.running:
                break
            # A quality switch we triggered tears down the stream — expected, so
            # reconnect regardless of how long the old stream ran.
            if self._pending_restart:
                self._pending_restart = False
                time.sleep(1.0)       # give the daemon a moment to rebind 9000
                continue
            if time.monotonic() - started > 3.0:
                break                 # watched, then closed the window => quit
            time.sleep(1.0)           # refused => listener not up yet => retry
        self.running = False

    # -- arming & control hotkeys -------------------------------------------
    def _toggle_arm(self):
        self.armed = not self.armed
        self._last_mouse = None  # reset so the first delta after arming isn't a jump
        print(f"[tether] forwarding {'ARMED — Mac keyboard+trackpad drive Linux' if self.armed else 'DISARMED'}")

    def _toggle_quality(self):
        # Debounce: ignore a new toggle while the last switch is still landing,
        # so a quick double-press can't desync the Mac's and daemon's idea of
        # the current quality (which would leave the stream stuck mid-handoff).
        if self._pending_restart:
            print("[tether] quality switch already in progress — ignoring")
            return
        self.quality = "high" if self.quality == "low" else "low"
        self._pending_restart = True
        self.send({"t": "quality", "level": self.quality})
        print(f"[tether] stream quality -> {self.quality.upper()} "
              f"({'native res' if self.quality == 'high' else 'low res, lower lag'})")

    def _send_paste(self):
        try:
            text = subprocess.run(["pbpaste"], capture_output=True, text=True,
                                  timeout=2).stdout
        except Exception as e:
            print(f"[tether] clipboard read failed: {e}")
            return
        if not text:
            print("[tether] clipboard empty — nothing to paste")
            return
        self.send({"t": "paste", "text": text})
        print(f"[tether] pasted {len(text)} chars from clipboard → Linux")

    # -- keyboard capture ----------------------------------------------------
    def _descriptor(self, key):
        if key in SPECIAL:
            return {"name": SPECIAL[key]}
        ch = getattr(key, "char", None)
        if ch:
            return {"char": ch}
        return None

    def _is_hotkey(self, key):
        return key in (ARM_KEY, QUALITY_KEY, PASTE_KEY)

    def on_press(self, key):
        if self._is_hotkey(key):
            return                      # all tether hotkeys act on release
        if self.armed:
            d = self._descriptor(key)
            if d:
                self.send({"t": "key", "val": 1, **d})

    def on_release(self, key):
        if key == ARM_KEY:
            self._toggle_arm(); return
        if key == QUALITY_KEY:
            self._toggle_quality(); return
        if key == PASTE_KEY:
            self._send_paste(); return
        if self.armed:
            d = self._descriptor(key)
            if d:
                self.send({"t": "key", "val": 0, **d})

    def _kb_intercept(self, event_type, event):
        return None if (self.armed) else event

    # -- mouse capture -------------------------------------------------------
    def on_move(self, x, y):
        if not self.armed or self._mac_delta:
            return                      # mac reads deltas in the intercept instead
        # Fallback (non-mac): diff absolute position. Stalls at screen edges —
        # which is exactly why mac uses the delta-field path below.
        if self._last_mouse is None:
            self._last_mouse = (x, y)
            return
        dx = int(x - self._last_mouse[0])
        dy = int(y - self._last_mouse[1])
        self._last_mouse = (x, y)
        if dx or dy:
            self.send({"t": "mmove", "dx": dx, "dy": dy})

    def on_click(self, x, y, button, pressed):
        if not self.armed:
            return
        name = MOUSE_BUTTONS.get(button)
        if name:
            self.send({"t": "mbtn", "btn": name, "val": 1 if pressed else 0})

    def on_scroll(self, x, y, dx, dy):
        if not self.armed:
            return
        self.send({"t": "mscroll", "dx": int(dx), "dy": int(dy)})

    def _ms_intercept(self, event_type, event):
        if not self.armed:
            return event
        # Read the RAW hardware delta straight off the Quartz move event. Works
        # even though we suppress the event (cursor frozen) — the tap sees the
        # delta before the cursor would move, so motion never stalls at an edge.
        if self._mac_delta and event_type in _MOVE_TYPES:
            try:
                dx = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX)
                dy = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY)
            except Exception:
                dx = dy = 0
            if dx or dy:
                self.send({"t": "mmove",
                           "dx": int(dx * MOUSE_SENS), "dy": int(dy * MOUSE_SENS)})
        return None                     # suppress all input locally while armed

    # -- chat window ---------------------------------------------------------
    def _open_chat_window(self):
        if not HAVE_TK:
            return
        self._close_chat_window()        # always start fresh
        self.chat_win = tk.Toplevel(self.root)
        self.chat_win.title("tether chat — ubuntu")
        self.chat_win.geometry("520x320")
        self.chat_win.configure(bg="#11131a")
        self.chat_text = tk.Text(
            self.chat_win, bg="#11131a", fg="#d7e0ff",
            insertbackground="#d7e0ff", font=("Menlo", 14),
            wrap="word", bd=0, padx=14, pady=12,
        )
        self.chat_text.pack(fill="both", expand=True)
        self.chat_text.insert("end", "— ubuntu is typing —\n\n")
        self.chat_win.protocol("WM_DELETE_WINDOW", lambda: None)  # only chat-off closes it

    def _close_chat_window(self):
        if self.chat_win is not None:
            try:
                self.chat_win.destroy()
            except Exception:
                pass
        self.chat_win = None
        self.chat_text = None

    def _append_chat(self, ch):
        if self.chat_text is None:
            return
        if ch == "\b":
            self.chat_text.delete("end-2c", "end-1c")
        else:
            self.chat_text.insert("end", ch)
        self.chat_text.see("end")

    # -- main loops ----------------------------------------------------------
    def _build_listeners(self):
        def build(cls, intercept, **cbs):
            try:
                return cls(darwin_intercept=intercept, **cbs)
            except TypeError:
                return cls(**cbs)       # non-macOS or no intercept support
        self.kb = build(keyboard.Listener, self._kb_intercept,
                        on_press=self.on_press, on_release=self.on_release)
        self.ms = build(mouse.Listener, self._ms_intercept,
                        on_move=self.on_move, on_click=self.on_click,
                        on_scroll=self.on_scroll)
        self.kb.start()
        self.ms.start()
        # pynput's listeners run on background threads; a failure there (bad
        # pyobjc, no Accessibility permission) kills the thread silently and
        # input forwarding just never works. Catch that and say so loudly.
        threading.Thread(target=self._watch_listeners, daemon=True).start()

    def _watch_listeners(self):
        time.sleep(1.0)
        dead = [n for n, l in (("keyboard", self.kb), ("mouse", self.ms))
                if not l.is_alive()]
        if dead:
            print(f"\n[tether] WARNING: {', '.join(dead)} listener failed to start — "
                  f"input forwarding is NOT working.")
            print("[tether]   Likely pynput/pyobjc can't run on this Python. Reinstall "
                  "pyobjc (pip install -U pyobjc) or use Python 3.13 (see README). "
                  "Video still works.")
            return
        # Listeners are alive — but on macOS they receive nothing until the app
        # has Accessibility permission. Check and say so, since that's the most
        # common "armed but keys do nothing" cause.
        if sys.platform == "darwin":
            try:
                from pynput._util import darwin as _d
                if not bool(_d.HIServices.AXIsProcessTrusted()):
                    print("\n[tether] NOTE: this terminal doesn't have macOS "
                          "Accessibility permission yet — keys/mouse won't reach "
                          "Linux until you grant it.")
                    print("[tether]   System Settings → Privacy & Security → "
                          "Accessibility AND Input Monitoring → enable your "
                          "terminal app, then restart tether_client.py.")
            except Exception:
                pass

    def _poll_tk(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "mode":
                    self._open_chat_window() if payload else self._close_chat_window()
                elif kind == "chat":
                    self._append_chat(payload)
        except queue.Empty:
            pass
        if not self.running:
            self.root.quit()
            return
        self.root.after(40, self._poll_tk)

    def _poll_headless(self):
        # fallback when Tk is unavailable: chat text goes to stdout
        while self.running:
            try:
                kind, payload = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if kind == "chat":
                ch = payload
                sys.stdout.write("\b \b" if ch == "\b" else ch)
                sys.stdout.flush()
            elif kind == "mode":
                print(f"\n[tether] (no Tk) chat {'ON' if payload else 'OFF'}")

    def run(self):
        self.running = True
        # macOS: Tk must init NSApplication on the MAIN thread before pynput's
        # listeners touch Cocoa on their background threads, or the process
        # aborts (SIGABRT). So create the hidden Tk root FIRST, then listeners.
        if HAVE_TK:
            self.root = tk.Tk()
            self.root.withdraw()            # root stays hidden; chat uses a Toplevel
        else:
            print(f"[tether] Tk chat window off ({sys.executable} has no usable "
                  f"tkinter, or TETHER_NO_TK set) — chat prints to this terminal.")
        threading.Thread(target=self._control_loop, daemon=True).start()
        self._build_listeners()
        threading.Thread(target=self._video_loop, daemon=True).start()
        print("[tether] F8 arm/disarm input · F9 toggle quality · F7 paste "
              "clipboard → Linux · Ctrl-C to quit.")
        try:
            if HAVE_TK:
                self.root.after(40, self._poll_tk)
                self.root.mainloop()
            else:
                self._poll_headless()
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.kb.stop()
            self.ms.stop()
            if self.video and self.video.poll() is None:
                self.video.terminate()
            print("\n[tether] client stopped")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python3 tether_client.py <ubuntu-tailscale-ip>")
    Client(sys.argv[1]).run()
