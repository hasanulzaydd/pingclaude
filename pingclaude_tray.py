"""Tray icon for pingclaude. Optional: pip install "pingclaude[tray]".

Runs the same Scheduler as the command line in a background thread.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pingclaude as pc

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None


def make_image(paused: bool = False):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (163, 162, 155, 255) if paused else (193, 95, 60, 255)  # warm gray / clay
    draw.ellipse((4, 4, size - 4, size - 4), fill=color)
    draw.ellipse((26, 26, 38, 38), fill=(250, 249, 245, 255))  # ivory
    return img


def main() -> int:
    if pystray is None:
        print('The tray app needs extras: pip install "pingclaude[tray]"  (or: pip install pystray Pillow)',
              file=sys.stderr)
        return 1
    log = pc.setup_logging(console=sys.stdout is not None)
    try:
        scheduler, lock = pc.open_scheduler({}, log)
    except pc.StartupError as exc:
        print(str(exc), file=sys.stderr)
        log(str(exc).splitlines()[0])
        return exc.code
    except pc.ConfigError as exc:
        print("pingclaude: %s" % exc, file=sys.stderr)
        return 2

    worker = threading.Thread(target=scheduler.run, name="pingclaude-scheduler", daemon=True)

    def next_text(item):
        if scheduler.paused:
            return "Paused"
        if scheduler.due is None:
            return "Starting..."
        left = max(0, int(scheduler.due - time.time()))
        return "Next send in %02d:%02d" % (left // 3600, (left % 3600) // 60)

    def toggle_pause(icon, item):
        scheduler.resume() if scheduler.paused else scheduler.pause()
        icon.icon = make_image(scheduler.paused)

    def open_settings(icon, item):
        gui = Path(__file__).resolve().with_name("pingclaude_gui.py")
        subprocess.Popen([sys.executable, str(gui)])

    def quit_app(icon, item):
        scheduler.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(next_text, None, enabled=False),
        pystray.MenuItem("Send now", lambda icon, item: scheduler.send_now()),
        pystray.MenuItem(lambda item: "Resume" if scheduler.paused else "Pause", toggle_pause),
        pystray.MenuItem("Settings...", open_settings),
        pystray.MenuItem("Open log", lambda icon, item: pc.open_path(pc.log_path())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("pingclaude", make_image(), "PingClaude", menu)

    def keep_menu_fresh():
        while not scheduler.stopped:
            icon.title = "PingClaude - " + next_text(None)
            try:
                icon.update_menu()
            except Exception:
                pass
            time.sleep(15)

    worker.start()
    threading.Thread(target=keep_menu_fresh, daemon=True).start()
    try:
        icon.run()  # must own the main thread (macOS requirement)
    finally:
        scheduler.stop()
        worker.join(timeout=10)
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
