# tether — remote admin for your Linux laptop, from your Mac

A hand-rolled remote desktop over Tailscale: the Linux daemon streams its
screen to the Mac and injects the Mac's keyboard/trackpad back into Linux, with
a few hotkey "modes" driven from the Linux keyboard.

```
  Linux (tether_daemon.py)                          Mac (tether_client.py)
  ├─ ffmpeg screen capture ──── video  tcp:9000 ───▶ ffplay window
  ├─ uinput virtual kbd+mouse ◀─ input  tcp:9001 ──── pynput capture (F8 to arm)
  └─ evdev physical monitor ──── chat   tcp:9001 ───▶ chat window (Tk)
```

Works on **X11**, **wlroots Wayland** (Hyprland/Sway, incl. Omarchy). GNOME-Wayland
is not wired (see Limits). Both machines must be on the same Tailscale tailnet.

---

# Daemon setup (the Linux machine)

Do these in order. Steps 1–2 are a one-time setup; after that you just run
step 4.

### 1. Install dependencies

**Omarchy / Arch (Hyprland, wlroots)**
```bash
sudo pacman -S ffmpeg python-evdev wf-recorder
# wf-recorder uses software libx264 — no VAAPI setup, works out of the box.
# Optional lower-latency hardware path instead (needs working VAAPI):
#   yay -S wl-screenrec                              # AUR
#   sudo pacman -S libva-utils intel-media-driver    # or libva-mesa-driver (AMD)
# The daemon prefers wl-screenrec if installed, else falls back to wf-recorder.
```

**Ubuntu / Debian (X11, or GNOME-on-Xorg)**
```bash
sudo apt install ffmpeg python3-evdev
# wlroots grabber only needed if you run a wlroots compositor on Debian/Ubuntu:
#   sudo apt install wf-recorder
```

### 2. Grant input access (REQUIRED — do not skip)

The daemon reads your physical keyboard from `/dev/input/*` and injects a
virtual keyboard/mouse via `/dev/uinput`. Both are normally root-only. This
one-time setup lets the daemon use them **as your own user**, which is
mandatory on Wayland (see "Why not sudo" below) and the clean path everywhere:

```bash
# a) add yourself to the 'input' group — grants read on /dev/input/*
sudo usermod -aG input $USER

# b) udev rule so /dev/uinput is writable by the 'input' group
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | \
  sudo tee /etc/udev/rules.d/99-tether-uinput.rules

# c) load the uinput module now (it auto-loads on boot afterward)
sudo modprobe uinput

# d) apply the new rule without rebooting
sudo udevadm control --reload-rules && sudo udevadm trigger
```

**Then log fully out and back in** (a new terminal is NOT enough — Linux group
membership only refreshes on a new login session).

### 3. Verify the setup took

```bash
id | grep -o input                 # must print: input
echo $XDG_SESSION_TYPE             # wayland (Hyprland) or x11
ls -l /dev/uinput                  # group should be 'input', perms crw-rw----
python3 -c "import evdev; print('evdev ok')"
```

On Hyprland, after the daemon is running you can also confirm the virtual
devices exist with `hyprctl devices` — you should see `tether-virtual-kbd` and
`tether-virtual-mouse`. libinput picks them up automatically; no compositor
config needed.

### 4. Run the daemon — as your user, NO sudo

```bash
python3 tether_daemon.py
```

You should see `backend=wlroots` (Hyprland) or `backend=x11`, then
`control server listening` and `ffmpeg listening for viewer`. Leave it running.

> **Why not sudo (important on Wayland):** `sudo` strips `WAYLAND_DISPLAY` and
> `XDG_RUNTIME_DIR`, and even `sudo -E` can't reach your compositor because the
> Wayland socket lives in *your* user's `XDG_RUNTIME_DIR` and root isn't allowed
> in. Capture then fails and port 9000 never opens. Running as your user (after
> step 2) is the only reliable way. On X11, sudo fails differently —
> "Authorization required" — because root isn't trusted by your X server. Either
> way: **run as your user.**

---

# Mac client setup

### 1. Install dependencies

```bash
brew install ffmpeg
brew install python-tk          # for the chat window (see venv note below)
pip3 install pynput
```

### 2. Run it (replace with the daemon's Tailscale IP)

```bash
python3 tether_client.py 100.x.y.z          # get the IP via `tailscale ip -4` on Linux
```

The ffplay window opens within a few seconds (capture takes a moment to warm
up — a couple of quiet "connecting…" retries is normal). macOS prompts for
**Accessibility + Input Monitoring** the first time you arm — grant both to your
terminal app and re-run.

### Mac-side hotkeys

| Key    | Action                                                           |
|--------|------------------------------------------------------------------|
| **F8** | Arm / disarm input. While armed, your Mac keyboard + trackpad drive Linux and are suppressed locally (Mac cursor freezes). |
| **F9** | Toggle stream quality: **low** (default, lower lag) ↔ **high** (native resolution) for detail work. The video briefly drops and reconnects. |
| **F7** | Type the Mac clipboard into Linux — `pbpaste` → sent over the control channel → typed by the virtual keyboard at wherever Linux focus is. Works armed or not. |

> F7/F8/F9 are consumed by tether. While **disarmed** they may still trigger
> their normal macOS function (brightness/media), same as F8 always has — arm
> first if that bothers you.

### Chat window & the venv / Tk gotcha

`import tkinter` must work for the **exact interpreter** you launch the client
with. `brew install python-tk` only adds Tk to Homebrew's python3 — a **venv
built from a different python (pyenv/system) will not have it**, and no amount
of `brew install python-tk` fixes that venv. Check:

```bash
python3 -c "import sys,tkinter; print(sys.executable, tkinter.TkVersion)"
cat "$VIRTUAL_ENV/pyvenv.cfg"     # the 'home =' line is the base python
```

Fixes:
- Simplest — **skip the venv** for the client (its only dep is pynput):
  `/opt/homebrew/bin/python3 -m pip install pynput` then run with that python.
- Or rebuild the venv from the brew python: `python3 -m venv .venv` using
  `/opt/homebrew/bin/python3`, then `pip install pynput`.

Tk is **optional** — without it, chat just prints in the client terminal. To
force that terminal fallback deliberately (e.g. to dodge a macOS Tk/pynput
conflict), set `TETHER_NO_TK=1`:
```bash
TETHER_NO_TK=1 python3 tether_client.py 100.x.y.z
```

---

# Running the daemon headless

The daemon needs no terminal — it answers the grabber's prompt itself, logs to
`~/.tether/tether.log`, and shuts down cleanly on SIGTERM.

### Quick (this session only)
```bash
setsid python3 tether_daemon.py >~/.tether/daemon.log 2>&1 </dev/null &
# stop it:
pkill -f tether_daemon.py          # or press Esc ×3 on the Linux keyboard
```
Run it from inside your Hyprland session so the Wayland env is present.

### Proper (survives reboot, auto-restarts) — systemd user service

Ships as `tether.service`. Edit the `ExecStart` path inside it to where your
`tether_daemon.py` lives, then:
```bash
mkdir -p ~/.config/systemd/user
cp tether.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tether.service
journalctl --user -u tether -f            # live logs
systemctl --user restart tether           # control
systemctl --user stop tether
```
Notes baked into the unit:
- Uses **`/usr/bin/python3`** so the pacman/apt `python-evdev` is importable —
  do **not** point it at a venv that lacks `evdev` (mirror image of the Mac Tk
  issue).
- If capture fails only under the service with a Wayland error, uncomment the
  `Environment=WAYLAND_DISPLAY=...` line and set it to match
  `echo $WAYLAND_DISPLAY`.

---

# Hotkeys (pressed on the Linux keyboard)

| Sequence        | Action                                   |
|-----------------|------------------------------------------|
| `Esc` ×3        | graceful exit                            |
| `Esc` ×5 (or +) | exit **and wipe** `~/.tether`            |
| `Space` ×4      | toggle chat mode (Linux typing → Mac)    |

A "run" is counted only while presses are faster than `TETHER_SEQ_WINDOW`
(default 1.2s) apart, and the action fires when the run ends — so 5 fast Esc
presses wipe rather than tripping the 3-press quit first.

> **Heads-up for headless use:** these hotkeys are always live on the daemon's
> keyboard. An accidental `Esc ×5` stops the service *and* deletes `~/.tether`.
> If that's risky for an unattended box, ask to gate the destructive hotkeys
> behind a config flag.

# Chat mode

Press `Space` ×4 on the **Linux** keyboard. A chat window opens on the Mac and
streams whatever you type on Linux. Press `Space` ×4 again to toggle it off —
the window is destroyed and its contents cleared. Without Tk, chat text falls
back to the client terminal.

---

# Config (env vars on the daemon)

| Var                   | Default   | Meaning                               |
|-----------------------|-----------|---------------------------------------|
| `TETHER_VIDEO_PORT`   | 9000      | video stream port                     |
| `TETHER_CONTROL_PORT` | 9001      | control/chat port                     |
| `TETHER_FPS`          | 30        | capture framerate                     |
| `TETHER_CAPTURE`      | auto      | `auto` / `x11` / `wlroots`            |
| `TETHER_QUALITY`      | low       | startup stream quality (`low`/`high`) |
| `TETHER_LOW_HEIGHT`   | 720       | scaled height in low-quality mode     |
| `TETHER_LOW_MAXRATE`  | 2M        | bitrate cap in low-quality mode       |
| `TETHER_SEQ_WINDOW`   | 1.2       | max seconds between presses in a run  |
| `TETHER_CMD`          | super     | map Mac Cmd → `super` or `ctrl`       |
| `TETHER_DIR`          | ~/.tether | runtime/log dir (wiped on Esc ×5)     |

Client env vars: `TETHER_NO_TK=1` forces terminal chat instead of the Tk
window; `MOUSE_SENS` (edit in the script) scales pointer speed.

## Stream quality / latency

The stream defaults to **low** (scaled to `TETHER_LOW_HEIGHT`, bitrate-capped)
to keep latency down. Press **F9** on the Mac for **high** (native resolution)
when you need detail, and again to drop back. The daemon re-execs capture on
switch and the Mac reconnects automatically.

How scaling happens by backend:
- **x11** — ffmpeg already encodes, so low mode just adds a scale filter. Cheap.
- **wlroots** — high mode is zero-copy (remux the grabber's native H.264). Low
  mode has ffmpeg **re-encode** to scale down, which costs a decode+encode on
  the Linux box. That's the right trade when the *network* is the bottleneck;
  if your Linux CPU is the limit instead, tell me and we'll scale at the source.

## Clipboard paste (Mac → Linux)

Copy anything on the Mac, focus where you want it on Linux, press **F7**. The
Mac sends the clipboard text over the control channel and the daemon types it
via the virtual keyboard (synthesizing Shift for capitals/symbols). US-layout
only — characters outside it (emoji, accents) are skipped.

# Mouse / trackpad

Forwarded while armed, as **relative** movement (deltas → `REL_X/REL_Y` on a
second uinput device), plus left/right/middle buttons and scroll. Notes:
- On macOS, movement is read as **raw hardware deltas** off the Quartz event, so
  it keeps flowing even when the Mac cursor is frozen or pinned at a screen edge
  (the old absolute-position method stalled there — that was the "won't move
  past the top edge" bug). Pointer speed: tweak `MOUSE_SENS` in the client.
- Scroll direction follows macOS; if it feels inverted, negate `dy` in
  `on_scroll`.

---

# Client disconnects / reconnecting

The daemon is built to **outlive the client**. If the Mac quits abruptly
(Cmd+Q, terminal closed, crash), it only drops two TCP sockets — the daemon
logs "viewer disconnected", goes back to listening on both ports, and a new
`tether_client.py` can connect straight away. It keeps running until you stop it
deliberately (Esc ×3, SIGTERM, or `systemctl --user stop`). Once video capture
has worked even once, a client that connects and leaves quickly can never make
it permanently give up on the stream, and it ignores SIGHUP so closing the
launching terminal won't kill it either.

# Troubleshooting

- **Mac: "Connection refused" on 9000 / client sits on "connecting…"** — the
  daemon's capture isn't producing frames. Check the daemon terminal/log. Most
  common: launched with sudo on Wayland, or `input`-group setup not applied
  (did you re-login?).
- **Daemon: "control disconnected" right after connecting** — the daemon
  process exited. Check it's still alive (`ps aux | grep '[t]ether_daemon'`); an
  accidental `Esc ×3` on the Linux keyboard quits it by design.
- **Daemon logs are noisy** (per-frame stats from wf-recorder) — the grabber's
  stderr is left visible for debugging. Once capture is reliable, add
  `2>/dev/null` right after the grabber command in the wlroots pipe in
  `tether_daemon.py` to silence it.
- **Isolate a capture problem** — run just the grabber half on the Linux box:
  ```bash
  wf-recorder --codec libx264 --muxer=mpegts --file=/dev/stdout 2>/dev/null | ffplay -
  ```
  Screen shows → the daemon pipeline will too; errors → paste them.
- **Mac: client aborts on start (SIGABRT)** — Tk/pynput Cocoa clash. You should
  be on the build that inits Tk on the main thread first; if it persists, run
  with `TETHER_NO_TK=1`.

# Known limits / next up

- **GNOME-Wayland** screen capture isn't covered (needs the xdg portal). Only
  wlroots (Hyprland/Sway) and X11 are wired. On stock Ubuntu GNOME, log into
  "Ubuntu on Xorg".
- **Keymap** is US-layout; non-US symbols may mismap.
- No encryption on the TCP channels — fine because Tailscale (WireGuard)
  already encrypts the transport, but don't expose these ports off-tailnet.
- Held-key autorepeat isn't forwarded (discrete down/up per key).
- Absolute pointer mapping (cursor tracks the video window 1:1) is a later add.
