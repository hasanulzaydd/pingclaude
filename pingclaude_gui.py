"""Settings window for pingclaude (tkinter, part of the standard library).

Pick the gap and the first-send time, press "Turn on", done: pingclaude saves
the settings, sets itself up to start at log-in, and starts in the background.
A running copy picks up later changes on its own.

The look borrows Claude's warm palette (grainy ivory paper, clay accent, serif
type). It uses no logos or trademarks and ships no fonts: Anthropic's own
typefaces are used only if they are already installed.
"""
from __future__ import annotations

import random
import sys
import threading
import time

try:
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import messagebox
except ImportError:  # e.g. Homebrew Python without python-tk
    tk = None

import pingclaude as pc
import pingclaude_autostart as autostart

REFRESH_MS = 2000

# Palette
PAGE = "#F0EEE6"     # warm paper
CARD = "#FAF9F5"     # ivory
FIELD = "#FFFFFF"
BORDER = "#DEDACE"
TRACK = "#E6E2D6"    # segmented control / disabled field
TEXT = "#141413"
MUTED = "#73726C"
ACCENT = "#C15F3C"   # clay
ACCENT_HOVER = "#A94F30"
ACCENT_SOFT = "#F3E3DA"
ACCENT_DISABLED = "#D9A48F"
ON = "#5E7F4A"       # olive
OFF = "#A3A29B"

GRAIN_TILE = 160     # px; the noise tile repeated across the window
GRAIN_AMOUNT = 9     # max brightness change per pixel (0-255)

# Anthropic's typefaces first (used only when installed), then close free or system fallbacks.
SERIF = ("Anthropic Serif", "Tiempos Text", "Tiempos Headline", "Source Serif 4", "Source Serif Pro",
         "Newsreader", "Georgia", "Cambria", "Times New Roman", "Times")
SANS = ("Anthropic Sans", "Styrene B", "Inter", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
        "Helvetica Neue", "Helvetica", "Arial")


def _pick(families, available):
    for name in families:
        if name in available:
            return name
    return "TkDefaultFont"


def _countdown(seconds):
    """'5h28m' style, rounded down to whole minutes."""
    if seconds < 60:
        return "under a minute"
    return pc.format_duration(int(seconds // 60) * 60)


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def grain_image(master, base, width, height, seed):
    """A `width` x `height` image of `base` color with fine film-like grain."""
    rnd = random.Random(seed)
    r0, g0, b0 = (int(base[i:i + 2], 16) for i in (1, 3, 5))
    rows = []
    for _ in range(GRAIN_TILE):
        row = []
        for _ in range(GRAIN_TILE):
            # sum of two uniforms: most pixels near the base, a few darker or lighter specks
            d = (rnd.randint(-GRAIN_AMOUNT, GRAIN_AMOUNT) + rnd.randint(-GRAIN_AMOUNT, GRAIN_AMOUNT)) // 2
            row.append("#%02x%02x%02x" % (
                max(0, min(255, r0 + d)), max(0, min(255, g0 + d)), max(0, min(255, b0 + d))))
        rows.append("{" + " ".join(row) + "}")
    tile = tk.PhotoImage(master=master, width=GRAIN_TILE, height=GRAIN_TILE)
    tile.put(" ".join(rows))
    full = tk.PhotoImage(master=master, width=width, height=height)
    full.tk.call(full, "copy", tile, "-to", 0, 0, width, height)  # Tk tiles the source
    return full


class CanvasText:
    """Lets a canvas text item be updated like a StringVar."""

    def __init__(self, canvas, item):
        self.canvas, self.item = canvas, item

    def set(self, text):
        self.canvas.itemconfigure(self.item, text=text)


class PillButton:
    """Rounded button drawn straight onto the grainy canvas. kind: primary, secondary, link.

    `labels` lists every text the button may show; it is sized for the widest.
    """

    def __init__(self, ui, x, y, labels, command, kind, anchor="w"):
        self.c, self.kind, self.command = ui.canvas, kind, command
        self.enabled, self.hover = True, False
        font = ui.font_body if kind == "link" else ui.font_button
        px = ui.px
        pad_x, height = (px(2), px(28)) if kind == "link" else (px(18), px(36))
        self.width = max(font.measure(t) for t in labels) + 2 * pad_x
        self.height = height
        if anchor == "e":
            x -= self.width
        tag = "pill%d" % id(self)
        x2, y2 = x + self.width, y + height
        self.shape = None
        if kind != "link":
            self.shape = _round_rect(self.c, x + 1, y + 1, x2 - 1, y2 - 1, height // 2, tags=tag)
        self.label = self.c.create_text((x + x2) // 2, y + height // 2, text=labels[0], font=font, tags=tag)
        underline_y = y + height // 2 + font.metrics("linespace") // 2
        self.underline = self.c.create_line(x + pad_x, underline_y, x2 - pad_x, underline_y,
                                            tags=tag, state="hidden")
        self.c.tag_bind(tag, "<Enter>", lambda e: self._hover(True))
        self.c.tag_bind(tag, "<Leave>", lambda e: self._hover(False))
        self.c.tag_bind(tag, "<ButtonRelease-1>", lambda e: self.enabled and self.command())
        self._paint()

    def set_text(self, text):
        self.c.itemconfigure(self.label, text=text)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._paint()

    def _hover(self, value):
        self.hover = value
        self.c.configure(cursor="hand2" if value and self.enabled else "")
        self._paint()

    def _paint(self):
        hot = self.hover and self.enabled
        if self.kind == "primary":
            fill = ACCENT_HOVER if hot else (ACCENT if self.enabled else ACCENT_DISABLED)
            self.c.itemconfigure(self.shape, fill=fill, outline=fill)
            self.c.itemconfigure(self.label, fill="#FFFFFF")
        elif self.kind == "secondary":
            self.c.itemconfigure(self.shape, fill=ACCENT_SOFT if hot else CARD, outline=BORDER)
            self.c.itemconfigure(self.label, fill=TEXT if self.enabled else OFF)
        else:
            color = ACCENT_HOVER if hot else ACCENT
            self.c.itemconfigure(self.label, fill=color)
            self.c.itemconfigure(self.underline, fill=color, state="normal" if hot else "hidden")


class SettingsWindow:
    def __init__(self, root):
        self.root = root
        root.title("PingClaude")
        root.resizable(False, False)
        root.configure(bg=PAGE)
        self.log = pc.setup_logging(console=False)

        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        families = set(tkfont.families(root))
        serif, sans = _pick(SERIF, families), _pick(SANS, families)
        self.font_title = tkfont.Font(root, family=serif, size=24)
        self.font_lede = tkfont.Font(root, family=serif, size=11, slant="italic")
        self.font_status = tkfont.Font(root, family=serif, size=12)
        self.font_note = tkfont.Font(root, family=serif, size=9)
        self.font_body = tkfont.Font(root, family=sans, size=10)
        self.font_small = tkfont.Font(root, family=sans, size=9)
        self.font_label = tkfont.Font(root, family=sans, size=10)
        self.font_button = tkfont.Font(root, family=sans, size=10, weight="bold")

        px = self.px
        c = self.canvas = tk.Canvas(root, bg=PAGE, highlightthickness=0, bd=0)
        c.pack()
        self._images = []  # keep PhotoImages alive

        margin, width = px(30), px(560)
        left, right = margin, margin + width
        y = margin

        # --- header ---
        item = c.create_text(left, y, text="PingClaude", font=self.font_title, fill=TEXT, anchor="nw")
        y = c.bbox(item)[3] + px(2)
        item = c.create_text(left, y, text="A quiet, steady schedule for Claude Code.",
                             font=self.font_lede, fill=MUTED, anchor="nw")
        y = c.bbox(item)[3] + px(18)

        dot = px(9)
        line_h = self.font_status.metrics("linespace")
        self.dot = c.create_oval(left, y + (line_h - dot) // 2, left + dot, y + (line_h - dot) // 2 + dot,
                                 fill=OFF, outline="")
        self.status = CanvasText(c, c.create_text(left + dot + px(10), y, text="Checking...",
                                                  font=self.font_status, fill=TEXT, anchor="nw"))
        y += line_h + px(2)
        self.last = CanvasText(c, c.create_text(left + dot + px(10), y, text="", font=self.font_small,
                                                fill=MUTED, anchor="nw"))
        y += self.font_small.metrics("linespace") + px(18)

        # --- settings card ---
        pad = px(22)
        card_top = y
        y += pad
        labels = ("Send every", "First send", "Message", "Command", "Retries")
        field_x = left + pad + max(self.font_label.measure(t) for t in labels) + px(22)
        field_w = right - pad - field_x

        self.hours, self.minutes = tk.StringVar(), tk.StringVar()
        y = self._row(y, "Send every", left + pad, [
            self._stepper(self.hours, 0, 48, 1), "hours",
            self._stepper(self.minutes, 0, 59, 5), "minutes"], field_x)

        self.when, self.start_at = tk.StringVar(value="now"), tk.StringVar()
        self.segments = self._segmented(self.when, (("now", "Right away"), ("at", "At a set time")))
        self.time_box = self._entry(self.start_at, width=5, justify="center")
        self.time_entry = self.time_box.entry
        y = self._row(y, "First send", left + pad, [self.segments, self.time_box, "24-hour"], field_x)
        self.time_hint = self._last_text

        self.message = tk.StringVar()
        y = self._row(y, "Message", left + pad, [self._entry(self.message, width=10)], field_x, field_w)
        self.command = tk.StringVar()
        y = self._row(y, "Command", left + pad, [self._entry(self.command, width=10)], field_x, field_w)
        self.retries = tk.StringVar()
        y = self._row(y, "Retries", left + pad, [self._stepper(self.retries, 0, 20, 1)], field_x, last=True)
        card_bottom = y + pad
        card_box = (left, card_top, right, card_bottom)

        # --- actions ---
        y = card_bottom + px(20)
        self.on_button = PillButton(self, left, y, ("Turn on", "Save changes"), self.turn_on, "primary")
        x = left + self.on_button.width + px(8)
        self.send_button = PillButton(self, x, y, ("Send now", "Sending..."), self.send_now, "secondary")
        x += self.send_button.width + px(8)
        self.off_button = PillButton(self, x, y, ("Turn off",), self.turn_off, "secondary")
        link = PillButton(self, right, y + px(4), ("Open log",), self.open_log, "link", anchor="e")
        y += self.on_button.height + px(18)

        note = ("Runs only while this computer is on, awake and online. "
                "Each send uses a little of your Claude plan.")
        item = c.create_text(left, y, text=note, font=self.font_note, fill=MUTED, anchor="nw", width=width)
        y = c.bbox(item)[3] + margin

        # --- grain: page, then the card on top of it, both under everything else ---
        total_w, total_h = right + margin, y
        c.configure(width=total_w, height=total_h)
        x1, y1, x2, y2 = card_box
        card_img = grain_image(root, CARD, x2 - x1, y2 - y1, seed=2)
        page_img = grain_image(root, PAGE, total_w, total_h, seed=1)
        self._images += [card_img, page_img]
        border = c.create_rectangle(x1, y1, x2, y2, outline=BORDER, width=1)
        c.tag_lower(border)
        c.tag_lower(c.create_image(x1, y1, image=card_img, anchor="nw"))
        c.tag_lower(c.create_image(0, 0, image=page_img, anchor="nw"))

        self.load_form()
        self.refresh()
        if pc.resolve_command(self.command.get() or pc.DEFAULTS["command"]) is None:
            self.root.after(300, lambda: messagebox.showwarning("Claude Code not found", pc.MISSING_CLAUDE_HINT))

    # --- layout helpers ---
    def px(self, n):
        return int(round(n * self.scale))

    def _row(self, y, label, label_x, parts, x, fill_width=None, last=False):
        """Place a label and a row of widgets/texts; returns the y of the next row."""
        c = self.canvas
        for part in parts:
            if not isinstance(part, str):
                part.update_idletasks()
        heights = [p.winfo_reqheight() for p in parts if not isinstance(p, str)]
        row_h = max(heights + [self.font_label.metrics("linespace")])
        mid = y + row_h // 2
        c.create_text(label_x, mid, text=label, font=self.font_label, fill=MUTED, anchor="w")
        for part in parts:
            if isinstance(part, str):
                self._last_text = c.create_text(x + self.px(6), mid, text=part, font=self.font_body,
                                                fill=MUTED, anchor="w")
                x = c.bbox(self._last_text)[2] + self.px(14)
            else:
                opts = {"width": fill_width} if fill_width else {}
                c.create_window(x, mid, window=part, anchor="w", **opts)
                x += (fill_width or part.winfo_reqwidth()) + self.px(4)
        return y + row_h + (0 if last else self.px(12))

    def _entry(self, var, width, justify="left"):
        """A flat text field with inner padding. Returns its frame; the field is `.entry`."""
        box = tk.Frame(self.canvas, bg=FIELD, highlightthickness=1, highlightbackground=BORDER,
                       padx=self.px(8))
        entry = tk.Entry(
            box, textvariable=var, width=width, justify=justify, font=self.font_body,
            relief="flat", bd=0, bg=FIELD, fg=TEXT, insertbackground=TEXT, highlightthickness=0,
            disabledbackground=TRACK, disabledforeground=OFF,
            selectbackground=ACCENT_SOFT, selectforeground=TEXT,
        )
        entry.pack(fill="x", ipady=self.px(5))
        entry.bind("<FocusIn>", lambda e: box.configure(highlightbackground=ACCENT))
        entry.bind("<FocusOut>", lambda e: box.configure(highlightbackground=BORDER))
        box.entry = entry
        return box

    def _stepper(self, var, low, high, step):
        box = tk.Frame(self.canvas, bg=FIELD, highlightthickness=1, highlightbackground=BORDER)

        def bump(delta):
            try:
                value = int(var.get() or 0)
            except ValueError:
                value = low
            var.set(str(max(low, min(high, value + delta))))

        def button(text, delta):
            lbl = tk.Label(box, text=text, font=self.font_button, bg=FIELD, fg=MUTED,
                           width=2, cursor="hand2")
            lbl.bind("<Button-1>", lambda e: bump(delta))
            lbl.bind("<Enter>", lambda e: lbl.configure(fg=ACCENT))
            lbl.bind("<Leave>", lambda e: lbl.configure(fg=MUTED))
            return lbl

        button("−", -step).pack(side="left")
        tk.Entry(box, textvariable=var, width=3, justify="center", font=self.font_body, relief="flat",
                 bd=0, bg=FIELD, fg=TEXT, insertbackground=TEXT, highlightthickness=0).pack(
            side="left", ipady=self.px(5))
        button("+", step).pack(side="left")
        return box

    def _segmented(self, var, options):
        track = tk.Frame(self.canvas, bg=TRACK, padx=self.px(3), pady=self.px(3))
        labels = {}

        def paint():
            for value, lbl in labels.items():
                chosen = var.get() == value
                lbl.configure(bg=FIELD if chosen else TRACK, fg=TEXT if chosen else MUTED)
            self._sync_time_field()

        for value, text in options:
            lbl = tk.Label(track, text=text, font=self.font_body, padx=self.px(12), pady=self.px(4),
                           cursor="hand2")
            lbl.bind("<Button-1>", lambda e, v=value: (var.set(v), paint()))
            lbl.pack(side="left")
            labels[value] = lbl
        track.repaint = paint
        return track

    # --- form <-> config ---
    def load_form(self):
        try:
            settings = pc.resolve_settings({})
        except pc.ConfigError:
            settings = pc.Settings.from_dict(pc.DEFAULTS)
        hours, rest = divmod(settings.interval, 3600)
        self.hours.set(str(hours))
        self.minutes.set(str(rest // 60))
        self.when.set("at" if settings.start_at else "now")
        self.start_at.set(settings.start_at or "06:00")
        self.message.set(settings.message)
        self.command.set(settings.command)
        self.retries.set(str(settings.retries))
        self.segments.repaint()

    def _sync_time_field(self):
        if not hasattr(self, "time_hint"):
            return
        active = self.when.get() == "at"
        self.time_entry.configure(state="normal" if active else "disabled")
        self.time_box.configure(bg=FIELD if active else TRACK)
        self.canvas.itemconfigure(self.time_hint, fill=MUTED if active else OFF)

    def read_form(self) -> pc.Settings:
        try:
            hours = int(self.hours.get() or 0)
            minutes = int(self.minutes.get() or 0)
        except ValueError:
            raise pc.ConfigError("hours and minutes must be whole numbers")
        if hours < 0 or minutes < 0 or hours * 60 + minutes < 1:
            raise pc.ConfigError("the gap must be at least 1 minute")
        return pc.Settings.from_dict({
            "interval": "%dh%dm" % (hours, minutes),
            "start_at": self.start_at.get() if self.when.get() == "at" else None,
            "message": self.message.get(),
            "command": self.command.get(),
            "retries": self.retries.get(),
        })

    # --- status ---
    def refresh(self):
        status = pc.read_status()
        if status["running"]:
            nxt = status.get("next_send")
            if status.get("paused"):
                text = "On · paused from the tray"
            elif isinstance(nxt, (int, float)):
                text = "On · next send at %s (in %s)" % (
                    time.strftime("%H:%M", time.localtime(nxt)), _countdown(nxt - time.time()))
            else:
                text = "On"
            self.canvas.itemconfigure(self.dot, fill=ON)
            self.on_button.set_text("Save changes")
        else:
            text = "Off · press Turn on to start"
            self.canvas.itemconfigure(self.dot, fill=OFF)
            self.on_button.set_text("Turn on")
        self.status.set(text)
        if status.get("last_send"):
            self.last.set("Last send %s · %s" % (
                time.strftime("%a %H:%M", time.localtime(status["last_send"])),
                "OK" if status.get("last_ok") else "failed, see the log"))
        else:
            self.last.set("No sends yet")
        self.root.after(REFRESH_MS, self.refresh)

    # --- actions ---
    def _validated(self):
        try:
            settings = self.read_form()
        except pc.ConfigError as exc:
            messagebox.showerror("Check your settings", str(exc))
            return None
        argv = pc.resolve_command(settings.command)
        if argv is None:
            messagebox.showerror("Claude Code not found", pc.MISSING_CLAUDE_HINT)
            return None
        try:
            pc.check_message_safe(argv, settings.message)
        except pc.ConfigError as exc:
            messagebox.showerror("Check your message", str(exc))
            return None
        return settings

    def turn_on(self):
        settings = self._validated()
        if settings is None:
            return
        pc.save_config(settings.to_config())
        if pc.read_status()["running"]:
            messagebox.showinfo("Saved", "Saved. PingClaude is already on and will use the new settings "
                                         "within %d seconds." % pc.MAX_CHUNK)
            return
        try:
            if autostart.is_installed():
                autostart.start()
            else:
                launch = autostart.launch_argv()
                steps = "\n".join("- " + line for line in autostart.describe_install(launch))
                if not messagebox.askyesno(
                        "Turn on PingClaude",
                        "PingClaude will start by itself every time you log in.\n\nThis will:\n%s\n\nContinue?" % steps):
                    return
                autostart.install(launch)
        except autostart.AutostartError as exc:
            messagebox.showerror("Could not turn on", str(exc))
            return
        self.status.set("Starting...")

    def turn_off(self):
        steps = "\n".join("- " + line for line in autostart.describe_uninstall())
        if not messagebox.askyesno(
                "Turn off PingClaude",
                "This will stop PingClaude and remove auto-start:\n%s\n\nYour settings are kept." % steps):
            return
        try:
            if autostart.is_installed():
                autostart.uninstall()
        except autostart.AutostartError as exc:
            messagebox.showerror("Could not remove auto-start", str(exc))
        pc.stop_running()
        self.status.set("Off")

    def send_now(self):
        settings = self._validated()
        if settings is None:
            return
        self.send_button.set_enabled(False)
        self.send_button.set_text("Sending...")
        argv = pc.resolve_command(settings.command)

        def work():
            self.log("Manual send from the settings window.")
            ok = pc.send_with_retries(argv, settings.message, 0, self.log)
            self.root.after(0, lambda: self._sent(ok))

        threading.Thread(target=work, daemon=True).start()

    def _sent(self, ok):
        self.send_button.set_enabled(True)
        self.send_button.set_text("Send now")
        if ok:
            messagebox.showinfo("Sent", "Message sent to Claude.")
        else:
            messagebox.showerror("Send failed", "The send failed. Open the log for details.")

    def open_log(self):
        pc.ensure_home()
        if not pc.log_path().exists():
            pc.log_path().write_text("", encoding="utf-8")
        pc.open_path(pc.log_path())


def main() -> int:
    if tk is None:
        print("The settings window needs tkinter. On macOS with Homebrew: brew install python-tk\n"
              "You can still use the command line: pingclaude --help", file=sys.stderr)
        return 1
    if sys.platform.startswith("win"):
        try:  # sharp text on high-DPI screens instead of a blurry bitmap-scaled window
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    SettingsWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
