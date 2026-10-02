"""Wipe Review - a small window: paste a Warcraft Logs report URL at the top,
then either Watch live (each pull is reviewed a few seconds after it ends) or
run a Full report (every pull in the report plus a summary of the evening).

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
from wipe_review.watcher import LiveWatcher, ReportRun

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.local.json"
ICON_ICO = Path(__file__).resolve().parent / "assets" / "icon.ico"
ICON_PNG = Path(__file__).resolve().parent / "assets" / "icon.png"
PLACEHOLDER = "https://www.warcraftlogs.com/reports/..."

# Colour palette (Discord's dark scheme)
BG = "#313338"          # results area
BG_DARK = "#2b2d31"     # cards, toolbar
BG_DARKER = "#1e1f22"   # dividers, scrollbar
INPUT = "#383a40"       # input field, stat chips
INPUT_HOVER = "#404249"
TEXT = "#dbdee1"
MUTED = "#949ba4"
WHITE = "#f2f3f5"
BLURPLE = "#5865f2"
BLURPLE_HOVER = "#4752c4"
GREEN = "#23a55a"
YELLOW = "#f0b232"
RED = "#f23f43"
RED_HOVER = "#c03537"

DEATH_LINE = re.compile(r"^(\d+:\d\d)\s+(.+?)( - killed by .*)$")

TAG_COLORS = {"normal": TEXT, "dim": MUTED, "info": WHITE, "header": WHITE, "kill": WHITE, "warn": YELLOW, "bad": RED, "good": GREEN}
ROW_A = "#232428"       # table rows (alternating)
ROW_B = "#2b2d31"
STATE_COLORS = {"idle": MUTED, "busy": YELLOW, "live": GREEN, "error": RED}


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
    """A flat coloured button (tk.Button ignores colours on Windows)."""

    def __init__(self, master, text, command, bg, hover, fg=WHITE, **kw):
        kw.setdefault("padx", 16)
        kw.setdefault("pady", 6)
        super().__init__(master, text=text, bg=bg, fg=fg, cursor="hand2", **kw)
        self.command, self.base, self.hover, self.fg = command, bg, hover, fg
        self.enabled = True
        self.bind("<Button-1>", lambda e: self.enabled and self.command(e))
        self.bind("<Enter>", lambda _e: self.enabled and self.configure(bg=self.hover))
        self.bind("<Leave>", lambda _e: self.enabled and self.configure(bg=self.base))

    def restyle(self, text, bg, hover, fg=None):
        self.base, self.hover = bg, hover
        self.fg = fg or self.fg
        self.configure(text=text, bg=bg, fg=self.fg)

    def set_enabled(self, enabled):
        self.enabled = enabled
        if enabled:
            self.configure(bg=self.base, fg=self.fg, cursor="hand2")
        else:
            self.configure(bg=BG_DARK, fg=MUTED, cursor="arrow")


class FlowFrame(tk.Frame):
    """Lays its children out left to right, wrapping onto new rows to fit
    the width (stat chips)."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._width = None
        self.bind("<Configure>", self._reflow)

    def _reflow(self, e=None):
        width = self.winfo_width()
        if width <= 1 or width == self._width:
            return
        self._width = width
        row = col = x = 0
        for w in self.winfo_children():
            w_width = w.winfo_reqwidth() + 6
            if x and x + w_width > width:
                row, col, x = row + 1, 0, 0
            w.grid(row=row, column=col, sticky="w", padx=(0, 6), pady=(0, 4))
            col, x = col + 1, x + w_width


class SortTable(tk.Frame):
    """A striped table (analysis.Table) whose column headers sort it when
    clicked - the first click sorts numbers high-to-low and text A-Z, the
    next click reverses."""

    def __init__(self, master, table, font, font_bold):
        super().__init__(master, bg=BG_DARKER)
        self.table, self.font, self.font_bold = table, font, font_bold
        self.rows = list(table.rows)
        self.sort_col, self.sort_desc = None, False
        self.cells = []
        for i in range(len(table.columns)):
            self.grid_columnconfigure(i, weight=1 if i == 0 else 0, minsize=0 if i == 0 else 64)
        self.headers = []
        for i, name in enumerate(table.columns):
            h = tk.Label(self, text=name, bg=BG_DARKER, fg=MUTED, font=font_bold, padx=8, pady=4, cursor="hand2",
                         anchor="w" if i == 0 else "center")
            h.grid(row=0, column=i, sticky="nsew")
            h.bind("<Button-1>", lambda _e, i=i: self.sort(i))
            h.bind("<Enter>", lambda _e, h=h: h.configure(fg=WHITE))
            h.bind("<Leave>", lambda _e, h=h: h.configure(fg=MUTED))
            self.headers.append(h)
        self._draw()

    def sort(self, col):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            sample = next((r[col].sort for r in self.rows if r[col].sort is not None), None)
            self.sort_col, self.sort_desc = col, isinstance(sample, (int, float))

        def key(r):
            v = r[col].sort if r[col].sort is not None else r[col].text
            return (0, v) if isinstance(v, (int, float)) else (1, str(v).lower())
        self.rows.sort(key=key, reverse=self.sort_desc)
        for i, h in enumerate(self.headers):
            arrow = (" \u25bc" if self.sort_desc else " \u25b2") if i == col else ""
            h.configure(text=self.table.columns[i] + arrow)
        self._draw()

    def _draw(self):
        for w in self.cells:
            w.destroy()
        self.cells = []
        for r, row in enumerate(self.rows, 1):
            bg = ROW_A if r % 2 else ROW_B
            for c, cell in enumerate(row):
                lbl = tk.Label(self, text=cell.text, bg=bg, fg=TAG_COLORS.get(cell.tag, TEXT), font=self.font,
                               padx=8, pady=3, anchor="w" if c == 0 else "center")
                lbl.grid(row=r, column=c, sticky="nsew", pady=(1, 0))
                self.cells.append(lbl)


class App:
    def __init__(self, root):
        self.root = root
        self.worker = None           # LiveWatcher or ReportRun while one is running
        self.mode = None             # "live" / "report" while running
        self.report_anchor = None    # where a full report's summary card goes
        self.events = queue.Queue()  # (kind, payload) from the watcher thread
        self.texts = []              # Text widgets, re-fitted when the width changes
        self._refit_pending = None
        self._stick = False
        settings = load_settings()

        self.f_base = pick_font(root, "Segoe UI", "Helvetica")
        self.font = (self.f_base, 10)
        self.font_bold = (self.f_base, 10, "bold")
        self.font_small = (self.f_base, 8)
        self.font_title = (self.f_base, 11, "bold")
        self.font_table = (self.f_base, 9)
        self.font_table_bold = (self.f_base, 9, "bold")
        self.line_px = tkfont.Font(root=root, font=self.font).metrics("linespace")

        root.title("Wipe Review")
        root.geometry("760x620")
        root.minsize(520, 360)
        root.configure(bg=BG)

        self.include_kills = tk.BooleanVar(value=settings.get("include_kills", False))
        self.review_existing = tk.BooleanVar(value=settings.get("review_existing", False))
        self.on_top = tk.BooleanVar(value=settings.get("on_top", False))
        self.detail_deaths = tk.IntVar(value=settings.get("detail_deaths", 8))
        root.attributes("-topmost", self.on_top.get())

        self._build_toolbar(settings.get("url", ""))
        self._build_results()
        self._build_statusbar()

        self.note("Paste a Warcraft Logs report link, then **Watch live** while your raid is logging (each pull is "
                  "reviewed a few seconds after it ends), or **Full report** for every pull of a report plus a summary "
                  "of the evening.")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self.drain_events)

    # --- layout ---------------------------------------------------------------
    def _build_toolbar(self, url):
        bar = tk.Frame(self.root, bg=BG_DARK)
        bar.pack(fill="x")

        title = tk.Frame(bar, bg=BG_DARK)
        title.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(title, text="Wipe Review", bg=BG_DARK, fg=WHITE, font=(self.f_base, 13, "bold")).pack(side="left")
        tk.Label(title, text="live raid log reviewer", bg=BG_DARK, fg=MUTED, font=self.font_small).pack(side="left", padx=10, pady=(4, 0))

        row = tk.Frame(bar, bg=BG_DARK)
        row.pack(fill="x", padx=16, pady=(0, 12))
        field = tk.Frame(row, bg=INPUT)
        field.pack(side="left", fill="x", expand=True)
        tk.Label(field, text="Live log", bg=INPUT, fg=MUTED, font=self.font_small, padx=10).pack(side="left")
        self.entry = tk.Entry(field, bg=INPUT, fg=TEXT, insertbackground=TEXT, relief="flat", font=self.font,
                              highlightthickness=0, bd=0, disabledbackground=INPUT, disabledforeground=MUTED)
        self.entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 10))
        self.entry.bind("<Return>", lambda _e: self.toggle("live"))
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<FocusOut>", self._show_placeholder)
        if url:
            self.entry.insert(0, url)
        else:
            self._show_placeholder()

        self.live_btn = FlatButton(row, "Watch live", lambda _e: self.toggle("live"), BLURPLE, BLURPLE_HOVER,
                                   font=self.font_bold, width=9)
        self.live_btn.pack(side="left", padx=(8, 0), fill="y")
        self.report_btn = FlatButton(row, "Full report", lambda _e: self.toggle("report"), INPUT, INPUT_HOVER,
                                     fg=TEXT, font=self.font_bold, width=9)
        self.report_btn.pack(side="left", padx=(8, 0), fill="y")
        gear = FlatButton(row, "⚙", lambda e: self._options_menu().tk_popup(e.x_root, e.y_root), INPUT, INPUT_HOVER,
                          fg=TEXT, font=(self.f_base, 12), padx=10, pady=0)
        gear.pack(side="left", padx=(8, 0), fill="y")

        tk.Frame(self.root, bg=BG_DARKER, height=1).pack(fill="x")

    def _build_results(self):
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Feed.Vertical.TScrollbar", troughcolor=BG, background=BG_DARKER, bordercolor=BG,
                        arrowcolor=BG, lightcolor=BG_DARKER, darkcolor=BG_DARKER, gripcount=0, arrowsize=0, width=8)
        style.map("Feed.Vertical.TScrollbar", background=[("active", INPUT_HOVER)])

        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview, style="Feed.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 2), pady=4)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.feed = tk.Frame(self.canvas, bg=BG)
        self.feed_id = self.canvas.create_window(0, 0, window=self.feed, anchor="nw")
        tk.Frame(self.feed, bg=BG, height=6).pack(fill="x")  # top breathing room
        self.canvas.bind("<Configure>", self._on_resize)
        self.root.bind_all("<MouseWheel>", self._on_wheel)

    def _build_statusbar(self):
        tk.Frame(self.root, bg=BG_DARKER, height=1).pack(fill="x")
        bar = tk.Frame(self.root, bg=BG_DARK)
        bar.pack(fill="x")
        self.dot = tk.Canvas(bar, width=10, height=10, bg=BG_DARK, highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")
        self.dot.pack(side="left", padx=(16, 6), pady=6)
        self.status = tk.Label(bar, text="Idle", bg=BG_DARK, fg=MUTED, font=self.font_small, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

    def set_status(self, text, state=None):
        self.status.configure(text=text)
        if state:
            self.dot.itemconfigure(self.dot_id, fill=STATE_COLORS[state])

    def _options_menu(self):
        style = dict(tearoff=False, bg=BG_DARKER, fg=TEXT, activebackground=BLURPLE, activeforeground=WHITE,
                     selectcolor=WHITE, font=self.font)
        m = tk.Menu(self.root, bd=0, **style)
        m.add_checkbutton(label="Include kills", variable=self.include_kills, command=self._save)
        m.add_checkbutton(label="Review pulls already in the log", variable=self.review_existing, command=self._save)
        m.add_checkbutton(label="Keep window on top", variable=self.on_top,
                          command=lambda: (self.root.attributes("-topmost", self.on_top.get()), self._save()))
        sub = tk.Menu(m, **style)
        for n in (3, 5, 8, 12, 20):
            sub.add_radiobutton(label=str(n), value=n, variable=self.detail_deaths, command=self._save)
        m.add_cascade(label="Detailed deaths per pull", menu=sub)
        m.add_separator()
        m.add_command(label="Clear results", command=self.clear)
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
        their real width, so sizing is one deferred pass: fit every text,
        then the scroll region, then (optionally) jump to the bottom."""
        self._stick = self._stick or stick
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
        # Height is set in lines, but section headings carry extra spacing, so
        # a line count can clip the bottom. Over-size from the pixel estimate,
        # then shrink to wherever the last line actually ends.
        # one "line" of Text height = font linespace + the widget's per-line spacing
        line = self.line_px + int(t.cget("spacing1")) + int(t.cget("spacing3"))
        px = t.count("1.0", "end", "ypixels")
        px = px[0] if isinstance(px, tuple) else px
        t.configure(height=max(1, -(-(px or 0) // line) + 2))
        t.update_idletasks()
        last = t.dlineinfo("end-1c")
        if last:
            t.configure(height=max(1, -(-(last[1] + last[3]) // line)))

    def _on_wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")
        return "break"

    def _at_bottom(self):
        return self.canvas.yview()[1] >= 0.995

    # --- results ------------------------------------------------------------------
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

    def note(self, text, tag="dim"):
        """A one-line activity note between cards: time, then text with **bold** bits."""
        stick = self._at_bottom()
        row = tk.Frame(self.feed, bg=BG)
        row.pack(fill="x", padx=16, pady=(6, 2))
        tk.Label(row, text=f"{datetime.now():%H:%M}", bg=BG, fg=MUTED, font=self.font_small, width=5, anchor="nw").pack(side="left", anchor="n", pady=(2, 0))
        t = self._rich_text(row, BG)
        for i, part in enumerate(text.split("**")):
            t.insert("end", part, ("strong",) if i % 2 else (tag,))
        t.configure(state="disabled")
        t.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.texts.append(t)
        self._schedule_refit(stick)
        return row

    def card(self, kind, title, stats, lines=None, collapsed=False, after=None, when=None, build_body=None):
        """A result card: badge, title, stat chips, then the review body.
        Clicking the header folds the body away."""
        stick = self._at_bottom() and after is None
        card = tk.Frame(self.feed, bg=BG_DARK)
        if after is not None:
            card.pack(fill="x", padx=16, pady=6, after=after)
        else:
            card.pack(fill="x", padx=16, pady=6)
        inner = tk.Frame(card, bg=BG_DARK)
        inner.pack(fill="x", padx=14, pady=12)

        head = tk.Frame(inner, bg=BG_DARK, cursor="hand2")
        head.pack(fill="x")
        badge_bg = {"KILL": GREEN, "WIPE": RED}.get(kind, BLURPLE)
        arrow = tk.Label(head, bg=BG_DARK, fg=TEXT, font=(self.f_base, 12), width=2, anchor="w")
        arrow.pack(side="left")
        tk.Label(head, text=kind, bg=badge_bg, fg=WHITE, font=(self.f_base, 8, "bold"), padx=7, pady=1).pack(side="left")
        tk.Label(head, text=title, bg=BG_DARK, fg=WHITE, font=self.font_title).pack(side="left", padx=(10, 0))
        when = f"{datetime.now():%H:%M}" if when is None else when
        if when:
            tk.Label(head, text=when, bg=BG_DARK, fg=MUTED, font=self.font_small).pack(side="right")

        if stats:
            chips = FlowFrame(inner, bg=BG_DARK)
            chips.pack(fill="x", pady=(8, 0))
            for s in stats:
                tk.Label(chips, text=s, bg=INPUT, fg=TEXT, font=self.font_small, padx=8, pady=2)

        body = tk.Frame(inner, bg=BG_DARK)
        tk.Frame(body, bg=INPUT, height=1).pack(fill="x", pady=(4, 6))
        if build_body:
            build_body(body)
        else:
            t = self._rich_text(body, BG_DARK)
            self._render_review_lines(t, lines)
            t.configure(state="disabled")
            t.pack(fill="x")
            self.texts.append(t)

        def set_open(open_):
            arrow.configure(text="\u25be" if open_ else "\u25b8")
            if open_:
                body.pack(fill="x")
            else:
                body.pack_forget()
            card._open = open_
            self._schedule_refit()

        for w in [head] + list(head.winfo_children()):
            w.bind("<Button-1>", lambda _e: set_open(not card._open))
        set_open(not collapsed)
        self._schedule_refit(stick)
        return card

    def _render_review_lines(self, t, lines):
        """Review lines -> card body: indentation becomes margins (so wrapped
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

    def show_review(self, lines, collapsed=False, after=None, when=None):
        lines = list(lines)
        while lines and not lines[0].text.strip():
            lines.pop(0)
        if not lines:
            return None
        # "=== WIPE - Ula'tek Heroic pull #1 | 3:50 | boss 42.5% | phase 2 | 27 players | 28 deaths ==="
        parts = lines[0].text.strip(" =").split(" | ")
        kind, _, title = parts[0].partition(" - ")
        return self.card(kind, title, parts[1:], lines[1:], collapsed=collapsed, after=after, when=when)

    def show_summary(self, summary, after=None):
        """The evening summary: one sortable table per section."""
        parts = summary.header.text.strip(" =").split(" | ")
        _, _, title = parts[0].partition(" - ")

        def build(body):
            for table in summary.tables:
                tk.Label(body, text=table.title, bg=BG_DARK, fg=WHITE, font=self.font_bold, anchor="w").pack(fill="x", pady=(10, 4))
                SortTable(body, table, self.font_table, self.font_table_bold).pack(fill="x")
                if table.note:
                    note = tk.Label(body, text=table.note, bg=BG_DARK, fg=MUTED, font=self.font_small, anchor="w", justify="left")
                    note.pack(fill="x", pady=(3, 0))
                    note.bind("<Configure>", lambda e, n=note: n.configure(wraplength=max(100, e.width - 4)))
            for note in summary.notes:
                tk.Label(body, text=note, bg=BG_DARK, fg=MUTED, font=self.font_small, anchor="w").pack(fill="x", pady=(10, 0))

        return self.card("REPORT", title, parts[1:], after=after, when="", build_body=build)

    def _scroll_to(self, widget):
        def go():
            self._refit_all()
            total = max(1, self.feed.winfo_reqheight())
            self.canvas.yview_moveto(max(0, widget.winfo_y() - 8) / total)
        self.root.after(120, go)

    def clear(self):
        for w in self.feed.winfo_children()[1:]:  # keep the top spacer
            w.destroy()
        self.texts.clear()
        self.report_anchor = None
        self._schedule_refit()
        self.canvas.yview_moveto(0)

    def drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "lines":
                    if payload and any(l.text.startswith("===") for l in payload):
                        self.show_review(payload)
                    else:
                        for l in payload:
                            self.note(l.text, l.tag)
                elif kind == "pull":
                    when = f"{datetime.fromtimestamp(payload.started_at / 1000):%H:%M}" if payload.started_at else ""
                    self.show_review(payload.lines, collapsed=True, when=when)
                elif kind == "summary":
                    anchor = self.report_anchor if self.report_anchor and self.report_anchor.winfo_exists() else None
                    card = self.show_summary(payload, after=anchor)
                    if card is not None:
                        self._scroll_to(card)
                elif kind == "status":
                    busy = payload.startswith(("Reviewing", "Reviewed", "Loading"))
                    self.set_status(payload, "busy" if busy else "live")
                elif kind == "stopped":
                    finished_report = self.mode == "report" and not payload
                    self.worker, self.mode = None, None
                    self._set_running(None)
                    if payload:
                        self.set_status("Stopped - error", "error")
                        self.note(f"**Stopped:** {payload}", "bad")
                    elif finished_report:
                        self.set_status("Report done - summary at the top, click a pull to expand it", "idle")
                    else:
                        self.set_status("Idle", "idle")
                        self.note("Stopped.")
        except queue.Empty:
            pass
        self.root.after(150, self.drain_events)

    # --- start / stop -----------------------------------------------------------
    def _save(self):
        save_settings({"url": self._url(), "include_kills": self.include_kills.get(), "review_existing": self.review_existing.get(),
                       "detail_deaths": int(self.detail_deaths.get()), "on_top": self.on_top.get()})

    def _set_running(self, mode):
        """mode: "live" / "report" while running, None when idle."""
        self.live_btn.restyle("Watch live", BLURPLE, BLURPLE_HOVER, WHITE)
        self.report_btn.restyle("Full report", INPUT, INPUT_HOVER, TEXT)
        self.live_btn.set_enabled(mode in (None, "live"))
        self.report_btn.set_enabled(mode in (None, "report"))
        if mode == "live":
            self.live_btn.restyle("Stop", RED, RED_HOVER, WHITE)
        elif mode == "report":
            self.report_btn.restyle("Stop", RED, RED_HOVER, WHITE)
        self.entry.configure(state="normal" if mode is None else "disabled")

    def toggle(self, mode):
        if self.worker:
            if mode == self.mode:
                self.worker.stop()
                self.set_status("Stopping...", "busy")
            return

        code = analysis.report_code(self._url())
        if not code:
            self.note("That doesn't look like a Warcraft Logs report link - expected something like "
                      "**https://www.warcraftlogs.com/reports/AbCdEf123456**", "bad")
            return
        if not self.ensure_credentials():
            return
        self._save()

        status = lambda s: self.events.put(("status", s))
        stopped = lambda err: self.events.put(("stopped", err))
        if mode == "live":
            self.note(f"Watching report **{code}** live.")
            self.set_status(f"Connecting to {code}...", "busy")
            self.worker = LiveWatcher(code, on_lines=lambda lines: self.events.put(("lines", lines)),
                                      on_status=status, on_stopped=stopped,
                                      include_kills=self.include_kills.get(), review_existing=self.review_existing.get(),
                                      detail_deaths=int(self.detail_deaths.get()))
        else:
            self.report_anchor = self.note(f"Full report for **{code}** - reviewing every pull, then summarising the evening.")
            self.worker = ReportRun(code, on_pull=lambda r: self.events.put(("pull", r)),
                                    on_summary=lambda lines: self.events.put(("summary", lines)),
                                    on_status=status, on_stopped=stopped,
                                    include_kills=self.include_kills.get(), detail_deaths=int(self.detail_deaths.get()))
        self.mode = mode
        self.worker.start()
        self._set_running(mode)

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
        if self.worker:
            self.worker.stop()
        self.root.destroy()


def set_icon(root):
    """Window/taskbar icon from assets/ (regenerate with tools/make_icon.py)."""
    try:
        if ICON_ICO.exists():
            root.iconbitmap(default=str(ICON_ICO))  # Windows: crisp small sizes
        if ICON_PNG.exists():
            root._icon = tk.PhotoImage(file=str(ICON_PNG))  # keep a reference or Tk drops it
            root.iconphoto(True, root._icon)
    except tk.TclError:
        pass


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI displays
        # Own taskbar identity, so Windows shows our icon instead of grouping under python.exe.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WipeReview.App")
    except Exception:
        pass
    root = tk.Tk()
    set_icon(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()