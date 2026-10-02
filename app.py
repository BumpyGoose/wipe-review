"""Wipe Review - a small Discord-styled window: paste the raid's live log URL
into the message box, press Start, and each pull's review arrives as a bot
message with an embed a few seconds after the pull ends.

Run with:  python app.py   (or pythonw app.py for no console window)
"""

import json
import queue
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, simpledialog, ttk

from wipe_review import analysis, wcl
from wipe_review.watcher import LiveWatcher

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.local.json"
PLACEHOLDER = "Paste the live log URL, e.g. https://www.warcraftlogs.com/reports/AbC123..."

# Discord dark theme
BG = "#313338"          # chat background
BG_DARK = "#2b2d31"     # embeds, header
BG_DARKER = "#1e1f22"   # window chrome, scrollbar trough
INPUT = "#383a40"       # message box
TEXT = "#dbdee1"
MUTED = "#949ba4"
WHITE = "#f2f3f5"
BLURPLE = "#5865f2"
BLURPLE_HOVER = "#4752c4"
GREEN = "#23a55a"
YELLOW = "#f0b232"
RED = "#f23f43"
RED_HOVER = "#c03537"
LINK = "#00a8fc"

DEATH_LINE = re.compile(r"^(\d+:\d\d)\s+(.+?)( - killed by .*)$")

TAG_COLORS = {"normal": TEXT, "dim": MUTED, "info": WHITE, "header": WHITE, "kill": WHITE, "warn": YELLOW, "bad": RED}


def load_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def pick_font(root, *names):
    available = set(tkfont.families(root))
    return next((n for n in names if n in available), "TkDefaultFont")


class FlatButton(tk.Label):
    """A Discord-style flat button (tk.Button ignores colours on Windows)."""

    def __init__(self, master, text, command, bg, hover, **kw):
        super().__init__(master, text=text, bg=bg, fg=WHITE, cursor="hand2", padx=16, pady=6, **kw)
        self.command, self.base, self.hover = command, bg, hover
        self.bind("<Button-1>", lambda _e: self.command())
        self.bind("<Enter>", lambda _e: self.configure(bg=self.hover))
        self.bind("<Leave>", lambda _e: self.configure(bg=self.base))

    def restyle(self, text, bg, hover):
        self.base, self.hover = bg, hover
        self.configure(text=text, bg=bg)


class App:
    def __init__(self, root):
        self.root = root
        self.watcher = None
        self.events = queue.Queue()  # (kind, payload) from the watcher thread
        self.texts = []              # embed Text widgets, re-fitted when the width changes
        settings = load_settings()

        self.f_base = pick_font(root, "gg sans", "Segoe UI", "Helvetica")
        self.font = (self.f_base, 10)
        self.font_bold = (self.f_base, 10, "bold")
        self.font_small = (self.f_base, 8)
        self.font_title = (self.f_base, 11, "bold")

        root.title("Wipe Review")
        root.geometry("760x620")
        root.minsize(520, 360)
        root.configure(bg=BG)

        self.include_kills = tk.BooleanVar(value=settings.get("include_kills", False))
        self.review_existing = tk.BooleanVar(value=settings.get("review_existing", False))
        self.on_top = tk.BooleanVar(value=settings.get("on_top", False))
        self.detail_deaths = tk.IntVar(value=settings.get("detail_deaths", 8))
        root.attributes("-topmost", self.on_top.get())

        self._build_header()
        self._build_chat()
        self._build_input(settings.get("url", ""))

        self.post(text="Paste the Warcraft Logs link of the report your raid is **live-logging** below and press **Start**. "
                       "Every pull gets reviewed here a few seconds after it ends.")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self.drain_events)

    # --- layout ---------------------------------------------------------------
    def _build_header(self):
        bar = tk.Frame(self.root, bg=BG, height=48)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="#", bg=BG, fg=MUTED, font=(self.f_base, 18)).pack(side="left", padx=(16, 4))
        tk.Label(bar, text="wipe-review", bg=BG, fg=WHITE, font=self.font_title).pack(side="left")
        self.status = tk.Label(bar, text="idle", bg=BG, fg=MUTED, font=self.font_small, anchor="e")
        self.status.pack(side="right", padx=16, fill="x", expand=True)
        tk.Frame(self.root, bg=BG_DARKER, height=1).pack(fill="x")

    def _build_chat(self):
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Discord.Vertical.TScrollbar", troughcolor=BG_DARK, background=BG_DARKER, bordercolor=BG,
                        arrowcolor=BG_DARK, lightcolor=BG_DARKER, darkcolor=BG_DARKER, gripcount=0, arrowsize=0, width=8)
        style.map("Discord.Vertical.TScrollbar", background=[("active", "#404249")])

        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview, style="Discord.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 2), pady=4)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.feed = tk.Frame(self.canvas, bg=BG)
        self.feed_id = self.canvas.create_window(0, 0, window=self.feed, anchor="nw")
        self.canvas.bind("<Configure>", self._on_resize)
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self._refit_pending = None

    def _build_input(self, url):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="x", padx=16, pady=(4, 16))
        box = tk.Frame(outer, bg=INPUT)
        box.pack(fill="x")

        gear = tk.Label(box, text="⚙", bg=INPUT, fg=MUTED, font=(self.f_base, 14), cursor="hand2", padx=10)
        gear.pack(side="left")
        gear.bind("<Enter>", lambda _e: gear.configure(fg=WHITE))
        gear.bind("<Leave>", lambda _e: gear.configure(fg=MUTED))
        gear.bind("<Button-1>", lambda e: self._options_menu().tk_popup(e.x_root, e.y_root))

        self.entry = tk.Entry(box, bg=INPUT, fg=TEXT, insertbackground=TEXT, relief="flat", font=self.font,
                              highlightthickness=0, bd=0, disabledbackground=INPUT, disabledforeground=MUTED)
        self.entry.pack(side="left", fill="x", expand=True, ipady=10)
        self.entry.bind("<Return>", lambda _e: self.toggle())
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<FocusOut>", self._show_placeholder)
        if url:
            self.entry.insert(0, url)
        else:
            self._show_placeholder()

        self.button = FlatButton(box, "Start", self.toggle, BLURPLE, BLURPLE_HOVER, font=self.font_bold)
        self.button.pack(side="right", padx=6, pady=6)

    def _options_menu(self):
        m = tk.Menu(self.root, tearoff=False, bg=BG_DARKER, fg=TEXT, activebackground=BLURPLE, activeforeground=WHITE,
                    selectcolor=WHITE, bd=0, font=self.font)
        m.add_checkbutton(label="Include kills", variable=self.include_kills, command=self._save)
        m.add_checkbutton(label="Review pulls already in the log", variable=self.review_existing, command=self._save)
        m.add_checkbutton(label="Keep window on top", variable=self.on_top,
                          command=lambda: (self.root.attributes("-topmost", self.on_top.get()), self._save()))
        sub = tk.Menu(m, tearoff=False, bg=BG_DARKER, fg=TEXT, activebackground=BLURPLE, activeforeground=WHITE,
                      selectcolor=WHITE, font=self.font)
        for n in (3, 5, 8, 12, 20):
            sub.add_radiobutton(label=str(n), value=n, variable=self.detail_deaths, command=self._save)
        m.add_cascade(label="Detailed deaths per pull", menu=sub)
        m.add_separator()
        m.add_command(label="Clear chat", command=self.clear)
        return m

    def _clear_placeholder(self, _e=None):
        if self.entry.get() == PLACEHOLDER:
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT)

    def _show_placeholder(self, _e=None):
        if not self.entry.get():
            self.entry.insert(0, PLACEHOLDER)
            self.entry.configure(fg=MUTED)

    def _url(self):
        v = self.entry.get().strip()
        return "" if v == PLACEHOLDER else v

    # --- scrolling / sizing -------------------------------------------------------
    def _on_resize(self, e):
        self.canvas.itemconfigure(self.feed_id, width=e.width)
        self._schedule_refit()

    def _schedule_refit(self, stick=False):
        """Text widgets only know their wrapped line count once laid out at
        their real width, so sizing is one deferred pass: fit every embed
        text, then the scroll region, then (optionally) jump to the bottom."""
        self._stick = getattr(self, "_stick", False) or stick
        if self._refit_pending:
            self.root.after_cancel(self._refit_pending)
        self._refit_pending = self.root.after(30, self._refit_all)

    def _refit_all(self):
        self._refit_pending = None
        self.root.update_idletasks()
        for t in self.texts:
            self._fit(t)
        self.root.update_idletasks()
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), max(self.feed.winfo_reqheight(), self.canvas.winfo_height())))
        if self._stick:
            self.canvas.yview_moveto(1.0)
        self._stick = False

    def _fit(self, t):
        width = t.winfo_width()
        if width <= 1 or getattr(t, "_fitted_width", None) == width:
            return
        t._fitted_width = width
        n = t.count("1.0", "end-1c", "displaylines")
        n = n[0] if isinstance(n, tuple) else n
        t.configure(height=max(1, (n or 0) + 1))

    def _on_wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")
        return "break"

    def _at_bottom(self):
        return self.canvas.yview()[1] >= 0.995


    # --- messages -----------------------------------------------------------------
    def _rich_text(self, parent, bg):
        t = tk.Text(parent, bg=bg, fg=TEXT, font=self.font, relief="flat", bd=0, highlightthickness=0, wrap="word",
                    width=1, height=1, cursor="arrow", padx=0, pady=0, spacing1=1, spacing3=1,
                    selectbackground=BLURPLE, inactiveselectbackground=BLURPLE)
        for tag, color in TAG_COLORS.items():
            t.tag_configure(tag, foreground=color)
        t.tag_configure("section", foreground=WHITE, font=self.font_bold, spacing1=10, spacing3=2)
        t.tag_configure("strong", foreground=WHITE, font=self.font_bold)
        t.bind("<MouseWheel>", self._on_wheel)
        return t

    def _insert_markdown(self, t, text, tag="normal"):
        """Tiny **bold** support for plain bot messages."""
        for i, part in enumerate(text.split("**")):
            t.insert("end", part, ("strong",) if i % 2 else (tag,))

    def post(self, text=None, embed=None, color=None):
        """Add a bot message. embed = {"title", "subtitle", "lines": [Line]}."""
        stick = self._at_bottom()
        msg = tk.Frame(self.feed, bg=BG)
        msg.pack(fill="x", padx=(16, 12), pady=(10, 2))

        av = tk.Canvas(msg, width=40, height=40, bg=BG, highlightthickness=0)
        av.create_oval(0, 0, 40, 40, fill=BLURPLE, outline="")
        av.create_text(20, 20, text="WR", fill=WHITE, font=(self.f_base, 11, "bold"))
        av.pack(side="left", anchor="n", padx=(0, 14))

        body = tk.Frame(msg, bg=BG)
        body.pack(side="left", fill="x", expand=True)
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="Wipe Review", bg=BG, fg=WHITE, font=self.font_bold).pack(side="left")
        tk.Label(head, text="APP", bg=BLURPLE, fg=WHITE, font=(self.f_base, 7, "bold"), padx=4).pack(side="left", padx=6)
        tk.Label(head, text=f"Today at {datetime.now():%H:%M}", bg=BG, fg=MUTED, font=self.font_small).pack(side="left")

        if text:
            t = self._rich_text(body, BG)
            self._insert_markdown(t, text, color or "normal")
            t.configure(state="disabled")
            t.pack(fill="x", pady=(2, 0))
            self.texts.append(t)

        if embed:
            card = tk.Frame(body, bg=BG_DARK)
            card.pack(fill="x", pady=(4, 0), padx=(0, 40))
            tk.Frame(card, bg=color or BLURPLE, width=4).pack(side="left", fill="y")
            inner = tk.Frame(card, bg=BG_DARK)
            inner.pack(side="left", fill="x", expand=True, padx=(12, 16), pady=(10, 12))
            tk.Label(inner, text=embed["title"], bg=BG_DARK, fg=WHITE, font=self.font_title, anchor="w",
                     justify="left").pack(fill="x")
            if embed.get("subtitle"):
                tk.Label(inner, text=embed["subtitle"], bg=BG_DARK, fg=MUTED, font=self.font_small, anchor="w").pack(fill="x", pady=(0, 4))
            t = self._rich_text(inner, BG_DARK)
            self._render_embed_lines(t, embed["lines"])
            t.configure(state="disabled")
            t.pack(fill="x")
            self.texts.append(t)

        self._schedule_refit(stick)

    def _render_embed_lines(self, t, lines):
        """Review lines -> embed body: indentation becomes margins (so wrapped
        lines hang-indent), blank lines are dropped in favour of section
        spacing, and death lines get a muted time and a bold player name."""
        first = True
        for line in lines:
            raw = line.text.rstrip()
            if not raw.strip():
                continue
            lead = len(raw) - len(raw.lstrip(" "))
            txt = raw.strip()
            indent = max(0, lead - 2) * 4
            margin = f"m{indent}"
            t.tag_configure(margin, lmargin1=indent, lmargin2=indent + 18)
            if not first:
                t.insert("end", "\n")
            first = False
            if line.tag == "info":
                t.insert("end", txt, ("section", margin))
                continue
            m = DEATH_LINE.match(txt) if line.tag == "normal" else None
            if m:
                t.insert("end", m.group(1) + "  ", ("dim", margin))
                t.insert("end", m.group(2), ("strong", margin))
                t.insert("end", m.group(3), (line.tag, margin))
            else:
                t.insert("end", txt, (line.tag, margin))

    def post_review(self, lines):
        lines = list(lines)
        while lines and not lines[0].text.strip():
            lines.pop(0)
        if not lines:
            return
        header = lines[0]
        parts = header.text.strip(" =").split(" | ")
        kind, _, rest = parts[0].partition(" - ")
        icon = "✔" if kind == "KILL" else "☠"
        title = f"{icon}  {kind.title()} — {rest}"
        body = lines[1:]
        while body and not body[0].text.strip():
            body.pop(0)
        self.post(embed={"title": title, "subtitle": "  •  ".join(parts[1:]), "lines": body},
                  color=GREEN if kind == "KILL" else RED)

    def clear(self):
        for w in self.feed.winfo_children():
            w.destroy()
        self.texts.clear()
        self._schedule_refit()
        self.canvas.yview_moveto(0)

    def drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "lines":
                    if payload and any(l.text.startswith("===") for l in payload):
                        self.post_review(payload)
                    else:
                        for l in payload:
                            self.post(text=l.text, color=l.tag)
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "stopped":
                    self.watcher = None
                    self._set_running(False)
                    if payload:
                        self.status.configure(text="stopped")
                        self.post(text=f"**Stopped:** {payload}", color="bad")
                    else:
                        self.status.configure(text="stopped")
                        self.post(text="Stopped watching.", color="dim")
        except queue.Empty:
            pass
        self.root.after(150, self.drain_events)

    # --- start / stop -----------------------------------------------------------
    def _save(self):
        save_settings({"url": self._url(), "include_kills": self.include_kills.get(), "review_existing": self.review_existing.get(),
                       "detail_deaths": int(self.detail_deaths.get()), "on_top": self.on_top.get()})

    def _set_running(self, running):
        if running:
            self.button.restyle("Stop", RED, RED_HOVER)
            self.entry.configure(state="disabled")
        else:
            self.button.restyle("Start", BLURPLE, BLURPLE_HOVER)
            self.entry.configure(state="normal")

    def toggle(self):
        if self.watcher:
            self.watcher.stop()
            self.status.configure(text="stopping...")
            return

        code = analysis.report_code(self._url())
        if not code:
            self.post(text="That doesn't look like a Warcraft Logs report link - expected something like "
                           "**https://www.warcraftlogs.com/reports/AbCdEf123456**", color="bad")
            return
        if not self.ensure_credentials():
            return
        self._save()

        self.post(text=f"Watching report **{code}** - I'll post here as each pull ends.")
        self.status.configure(text=f"connecting to {code}...")
        self.watcher = LiveWatcher(
            code,
            on_lines=lambda lines: self.events.put(("lines", lines)),
            on_status=lambda s: self.events.put(("status", s)),
            on_stopped=lambda err: self.events.put(("stopped", err)),
            include_kills=self.include_kills.get(),
            review_existing=self.review_existing.get(),
            detail_deaths=int(self.detail_deaths.get()),
        )
        self.watcher.start()
        self._set_running(True)

    def ensure_credentials(self):
        try:
            wcl.load_credentials()
            return True
        except wcl.MissingCredentials:
            pass
        messagebox.showinfo("Wipe Review", "This needs a Warcraft Logs API client.\n\n"
                            "Create one at https://www.warcraftlogs.com/api/clients/ (any name, redirect URL "
                            "http://localhost), then paste its client ID and secret on the next prompts.")
        cid = simpledialog.askstring("Wipe Review", "Client ID:", parent=self.root)
        if not cid:
            return False
        secret = simpledialog.askstring("Wipe Review", "Client secret:", parent=self.root, show="*")
        if not secret:
            return False
        wcl.save_credentials(cid.strip(), secret.strip())
        return True

    def on_close(self):
        if self.watcher:
            self.watcher.stop()
        self.root.destroy()


def main():
    try:  # crisp text on high-DPI Windows displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
