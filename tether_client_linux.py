#!/usr/bin/env python3
"""
tether_client_linux.py  —  Linux/Ubuntu side of tether (the viewer/controller)

The Linux counterpart to tether_client.py (Mac). Runs on an Ubuntu box and
controls a remote machine running tether_daemon.py, over Tailscale:

  1. Shows the remote screen with ffplay (low-latency).
  2. Reads your keyboard + mouse via evdev. While ARMED (toggle F8) it
     EXCLUSIVELY GRABS those devices (EVIOCGRAB) so the events don't reach your
     local session, and forwards them to the daemon. Disarmed = normal local use.
  3. F9 toggles remote stream quality; F7 types your clipboard into the remote.
  4. A chat window (Tk) shows text the remote user streams in chat mode.

Why evdev instead of pynput: evdev sits below X11/Wayland, so grabbing gives
perfect local suppression AND raw relative mouse deltas (no screen-edge stall),
and it works the same on X11 and Wayland.

Usage:
    python3 tether_client_linux.py <daemon-tailscale-ip>

Needs:  python3-evdev, ffmpeg (ffplay); python3-tk for the chat window;
        a clipboard tool for F7 (wl-clipboard on Wayland, or xclip/xsel on X11).
Permissions: your user must be able to read/grab /dev/input/* — add yourself to
the 'input' group once and re-login:  sudo usermod -aG input $USER
"""

import json
import os
import queue
import select
import shutil
import socket
import subprocess
import sys
import threading
import time

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:
    sys.exit("Missing dependency: pip install evdev  (or apt install python3-evdev)")

try:
    import tkinter as tk
    HAVE_TK = not os.environ.get("TETHER_NO_TK")
except ImportError:
    HAVE_TK = False

VIDEO_PORT   = int(os.environ.get("TETHER_VIDEO_PORT", 9000))
CONTROL_PORT = int(os.environ.get("TETHER_CONTROL_PORT", 9001))
ARM_KEY      = ecodes.KEY_F8      # toggle grab + forwarding
QUALITY_KEY  = ecodes.KEY_F9      # toggle remote stream quality low<->high
PASTE_KEY    = ecodes.KEY_F7      # type local clipboard into the remote
HOTKEYS      = {ARM_KEY, QUALITY_KEY, PASTE_KEY}
MOUSE_SENS   = float(os.environ.get("TETHER_MOUSE_SENS", "1.0"))
# Touchpad (absolute EV_ABS devices) tuning: finger motion is converted to
# relative deltas. GAIN is pixels-per-mm when the pad reports a resolution;
# FLAT is the fallback units->px factor when it doesn't.
TOUCHPAD_GAIN = float(os.environ.get("TETHER_TOUCHPAD_GAIN", "8.0"))
TOUCHPAD_FLAT = float(os.environ.get("TETHER_TOUCHPAD_FLAT", "0.35"))

VIRTUAL_NAMES = ("tether-virtual-kbd", "tether-virtual-mouse")  # never grab these
MOUSE_BUTTONS = {
    ecodes.BTN_LEFT: "left", ecodes.BTN_RIGHT: "right", ecodes.BTN_MIDDLE: "middle",
}
# multi-finger tool codes — used to suppress cursor jumps during gestures
MULTI_FINGER = (ecodes.BTN_TOOL_DOUBLETAP, ecodes.BTN_TOOL_TRIPLETAP,
                ecodes.BTN_TOOL_QUADTAP)


def clipboard_text():
    """Read the local clipboard via whatever tool is installed."""
    for cmd in (["wl-paste", "-n"],
                ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "-b", "-o"]):
        if shutil.which(cmd[0]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=2).stdout
            except Exception:
                return ""
    return None   # no clipboard tool found


class Client:
    def __init__(self, host):
        self.host = host
        self.armed = False
        self.running = False
        self.sock = None
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.quality = "low"
        self._pending_restart = False
        self.video = None
        self.root = None
        self.chat_win = None
        self.chat_text = None
        self.devices = []
        self._grabbed = False
        # relative-pointer accumulators flushed on SYN_REPORT
        self._adx = self._ady = 0
        self._asx = self._asy = 0
        # touchpad (absolute) state: current finger pos, last pos while down,
        # contact/gesture flags, and fractional remainder for smooth slow motion
        self._abs_x = self._abs_y = None
        self._pad_prev = None
        self._pad_fx = self._pad_fy = 0.0
        self._touching = False
        self._two_finger = False
        self._pad_scale = TOUCHPAD_FLAT

    # ------------------------------------------------------------------ control
    def _control_loop(self):
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
            print(f"[tether] remote chat mode {'ON' if on else 'OFF'}")

    def send(self, obj):
        with self.lock:
            if not self.sock:
                return
            try:
                self.sock.sendall((json.dumps(obj) + "\n").encode())
            except OSError:
                self.sock = None

    # -------------------------------------------------------------------- video
    def start_video(self):
        url = f"tcp://{self.host}:{VIDEO_PORT}"
        # -autoexit so ffplay quits on stream-end (quality switch / capture
        # restart) and the loop can reconnect instead of freezing on last frame.
        cmd = ["ffplay", "-loglevel", "error", "-autoexit",
               "-fflags", "nobuffer", "-flags", "low_delay", "-framedrop",
               "-probesize", "32", "-sync", "ext",
               "-window_title", "tether — remote", url]
        return subprocess.Popen(cmd)

    def _video_loop(self):
        print(f"[tether] connecting video to {self.host}:{VIDEO_PORT} "
              f"(capture can take a few seconds to warm up)…")
        while self.running:
            started = time.monotonic()
            self.video = self.start_video()
            self.video.wait()
            if not self.running:
                break
            if self._pending_restart:
                self._pending_restart = False
                time.sleep(1.0)
                continue
            if time.monotonic() - started > 3.0:
                break                 # watched, then closed the window => quit
            time.sleep(1.0)           # refused => listener not up yet => retry
        self.running = False

    # ------------------------------------------------------------ input (evdev)
    def _find_devices(self):
        found = []
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if dev.name in VIRTUAL_NAMES:
                continue
            caps = dev.capabilities(absinfo=True)   # EV_ABS -> [(code, AbsInfo), ...]
            keys = caps.get(ecodes.EV_KEY, [])
            rels = caps.get(ecodes.EV_REL, [])
            abs_list = caps.get(ecodes.EV_ABS, [])
            abs_codes = {c for (c, _info) in abs_list}
            is_kbd = ecodes.KEY_ESC in keys and ecodes.KEY_A in keys
            is_ptr = ecodes.REL_X in rels and ecodes.REL_Y in rels
            # a touchpad: reports contact (BTN_TOUCH) and absolute X/Y (ST or MT)
            has_xy = ((ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes) or
                      (ecodes.ABS_MT_POSITION_X in abs_codes and
                       ecodes.ABS_MT_POSITION_Y in abs_codes))
            is_pad = ecodes.BTN_TOUCH in keys and has_xy
            if is_kbd or is_ptr or is_pad:
                found.append(dev)
                if is_pad:
                    self._pad_scale = self._compute_pad_scale(abs_list, abs_codes)
                kinds = [k for k, c in (("keyboard", is_kbd), ("pointer", is_ptr),
                                        ("touchpad", is_pad)) if c]
                print(f"[tether] input device: {dev.name} ({path}) [{'+'.join(kinds)}]")
        if not found:
            print("[tether] WARNING: no keyboard/mouse found. Are you in the "
                  "'input' group? (sudo usermod -aG input $USER, then re-login)")
        return found

    def _compute_pad_scale(self, abs_list, abs_codes):
        """units->pixels factor for a touchpad. Prefer the pad's reported
        resolution (units/mm) so speed is consistent across hardware."""
        xcode = (ecodes.ABS_X if ecodes.ABS_X in abs_codes
                 else ecodes.ABS_MT_POSITION_X)
        for code, info in abs_list:
            if code == xcode:
                res = getattr(info, "resolution", 0) or 0
                if res > 0:
                    return TOUCHPAD_GAIN / res
                break
        return TOUCHPAD_FLAT

    def _set_grab(self, on):
        if on == self._grabbed:
            return
        for d in self.devices:
            try:
                d.grab() if on else d.ungrab()
            except OSError:
                pass
        self._grabbed = on

    def _input_loop(self):
        self.devices = self._find_devices()
        if not self.devices:
            return
        fds = {d.fd: d for d in self.devices}
        while self.running:
            try:
                r, _, _ = select.select(list(fds), [], [], 0.2)
            except (OSError, ValueError):
                break
            for fd in r:
                dev = fds.get(fd)
                if dev is None:
                    continue
                try:
                    for e in dev.read():
                        self._on_event(e)
                except OSError:
                    pass

    def _on_event(self, e):
        # flush accumulated relative motion / scroll once per SYN_REPORT
        if e.type == ecodes.EV_SYN and e.code == ecodes.SYN_REPORT:
            if self.armed and (self._adx or self._ady):
                self.send({"t": "mmove",
                           "dx": int(self._adx * MOUSE_SENS),
                           "dy": int(self._ady * MOUSE_SENS)})
            if self.armed and (self._asx or self._asy):
                self.send({"t": "mscroll", "dx": self._asx, "dy": self._asy})
            self._adx = self._ady = self._asx = self._asy = 0
            self._flush_touchpad()
            return

        if e.type == ecodes.EV_KEY:
            code, val = e.code, e.value          # val: 1 down, 0 up, 2 repeat
            if code in HOTKEYS:                   # tether controls — never forward
                if val == 1:
                    self._hotkey(code)
                return
            if code == ecodes.BTN_TOUCH:          # finger on/off the touchpad
                self._touching = (val == 1)
                if not self._touching:
                    self._pad_prev = None         # lift => no delta across gaps
                return
            if code in MULTI_FINGER:              # 2+ fingers: a gesture, not a move
                self._two_finger = (val == 1)
                self._pad_prev = None             # avoid a jump when fingers change
                return
            if code in MOUSE_BUTTONS:             # incl. physical clickpad press
                if self.armed and val in (0, 1):
                    self.send({"t": "mbtn", "btn": MOUSE_BUTTONS[code], "val": val})
                return
            if self.armed:
                self.send({"t": "key", "code": code, "val": val})
            return

        if e.type == ecodes.EV_REL:
            if e.code == ecodes.REL_X:
                self._adx += e.value
            elif e.code == ecodes.REL_Y:
                self._ady += e.value
            elif e.code == ecodes.REL_WHEEL:
                self._asy += e.value
            elif e.code == ecodes.REL_HWHEEL:
                self._asx += e.value
            return

        if e.type == ecodes.EV_ABS:
            if e.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X):
                self._abs_x = e.value
            elif e.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y):
                self._abs_y = e.value

    def _flush_touchpad(self):
        """Convert the touchpad's absolute finger position into relative motion.
        Called once per SYN frame. Single finger only — two-finger frames are
        suppressed so gestures don't jump the cursor."""
        if (not self._touching or self._two_finger
                or self._abs_x is None or self._abs_y is None):
            return
        if self._pad_prev is not None and self.armed:
            dx = (self._abs_x - self._pad_prev[0]) * self._pad_scale * MOUSE_SENS
            dy = (self._abs_y - self._pad_prev[1]) * self._pad_scale * MOUSE_SENS
            self._pad_fx += dx
            self._pad_fy += dy
            idx, idy = int(self._pad_fx), int(self._pad_fy)   # keep fractional part
            if idx or idy:
                self.send({"t": "mmove", "dx": idx, "dy": idy})
                self._pad_fx -= idx
                self._pad_fy -= idy
        self._pad_prev = (self._abs_x, self._abs_y)

    def _hotkey(self, code):
        if code == ARM_KEY:
            self.armed = not self.armed
            self._set_grab(self.armed)
            print(f"[tether] forwarding {'ARMED — local keyboard+mouse drive remote' if self.armed else 'DISARMED'}")
        elif code == QUALITY_KEY:
            if self._pending_restart:
                print("[tether] quality switch already in progress — ignoring")
                return
            self.quality = "high" if self.quality == "low" else "low"
            self._pending_restart = True
            self.send({"t": "quality", "level": self.quality})
            print(f"[tether] stream quality -> {self.quality.upper()} "
                  f"({'native res' if self.quality == 'high' else 'low res, lower lag'})")
        elif code == PASTE_KEY:
            text = clipboard_text()
            if text is None:
                print("[tether] no clipboard tool found — install wl-clipboard "
                      "(Wayland) or xclip/xsel (X11)")
                return
            if not text:
                print("[tether] clipboard empty — nothing to paste")
                return
            self.send({"t": "paste", "text": text})
            print(f"[tether] pasted {len(text)} chars from clipboard → remote")

    # --------------------------------------------------------------- chat (Tk)
    def _open_chat_window(self):
        if not HAVE_TK:
            return
        self._close_chat_window()
        self.chat_win = tk.Toplevel(self.root)
        self.chat_win.title("tether chat — remote")
        self.chat_win.geometry("520x320")
        self.chat_win.configure(bg="#11131a")
        self.chat_text = tk.Text(
            self.chat_win, bg="#11131a", fg="#d7e0ff",
            insertbackground="#d7e0ff", font=("monospace", 14),
            wrap="word", bd=0, padx=14, pady=12,
        )
        self.chat_text.pack(fill="both", expand=True)
        self.chat_text.insert("end", "— remote is typing —\n\n")
        self.chat_win.protocol("WM_DELETE_WINDOW", lambda: None)

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

    # -------------------------------------------------------------------- main
    def run(self):
        self.running = True
        threading.Thread(target=self._control_loop, daemon=True).start()
        threading.Thread(target=self._input_loop, daemon=True).start()
        threading.Thread(target=self._video_loop, daemon=True).start()
        print("[tether] F8 arm/disarm input · F9 toggle quality · F7 paste "
              "clipboard → remote · Ctrl-C to quit.")
        try:
            if HAVE_TK:
                self.root = tk.Tk()
                self.root.withdraw()
                self.root.after(40, self._poll_tk)
                self.root.mainloop()
            else:
                print("[tether] Tk not found — chat prints to this terminal "
                      "(apt install python3-tk for the window).")
                self._poll_headless()
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self._set_grab(False)       # ALWAYS release the devices on exit
            if self.video and self.video.poll() is None:
                self.video.terminate()
            print("\n[tether] client stopped")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python3 tether_client_linux.py <daemon-tailscale-ip>")
    Client(sys.argv[1]).run()
