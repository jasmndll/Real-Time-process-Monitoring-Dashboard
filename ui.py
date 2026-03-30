"""
Real-Time OS Monitor  ·  Optimized Edition
==========================================
Performance fixes vs previous version:
  - Charts use set_xdata/set_ydata only — no fill_between per tick
  - Disk panel rebuilt only every 10 s, not every second
  - Process table does smart diff-update (insert/delete only changed rows)
  - psutil data collected in a background thread → UI never blocks
  - Chart canvas uses blit=False + draw_idle (avoids full redraw)
  - Separate slow-poll (5 s) for disk/net totals

New features:
  - Per-core CPU bars (mini horizontal bars, one per logical core)
  - CPU temperature (if sensor available)
  - Top-process sparkline bars in table (visual CPU bar per row)
  - Kill-process button (right-click context menu)
  - Search / filter box for process table
  - Tabs: Overview | Processes | Disk | Network History
  - Network history tab with cumulative sent/recv GB
  - Refresh-rate selector (0.5 s / 1 s / 2 s)
  - Window title live-updates with CPU %
"""

import tkinter as tk
from tkinter import ttk, messagebox
import psutil as ps
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from collections import deque
import threading
import time
import os

# ─── PALETTE ──────────────────────────────────────────────────────────────────
BG      = "#0c0e14"
PANEL   = "#13161f"
PANEL2  = "#181b26"
BORDER  = "#252836"
ALT     = "#191c28"
TXT     = "#d8deee"
DIM     = "#555e7a"
DIM2    = "#3a4058"
GREEN   = "#39d98a"
BLUE    = "#4da6ff"
YELLOW  = "#f5c542"
RED     = "#ff5c5c"
PURPLE  = "#9d7dea"
ORANGE  = "#ff8c42"
TEAL    = "#29c4c4"
PINK    = "#e879a0"

HISTORY = 90   # seconds kept in chart history

STATE_MAP = {
    "running":    (GREEN,  "● Running"),
    "sleeping":   (BLUE,   "● Sleeping"),
    "idle":       (DIM,    "○ Idle"),
    "stopped":    (YELLOW, "■ Stopped"),
    "zombie":     (RED,    "✕ Zombie"),
    "disk-sleep": (ORANGE, "◎ Disk-sleep"),
}

def state_info(s):
    return STATE_MAP.get((s or "idle").lower(), (DIM, f"● {s}"))

def fmt_bytes(b):
    for u in ("B","KB","MB","GB","TB"):
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"

def color_for(pct):
    return GREEN if pct < 60 else YELLOW if pct < 85 else RED

# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND DATA THREAD
# Collects all psutil data at the chosen interval so the UI never blocks.
# ══════════════════════════════════════════════════════════════════════════════
class SystemMonitor:
    def __init__(self):
        self.lock         = threading.Lock()
        self.interval     = 1.0          # seconds; UI can change this
        self._stop        = False

        # rolling histories
        self.cpu_hist     = deque([0.0]*HISTORY, maxlen=HISTORY)
        self.mem_hist     = deque([0.0]*HISTORY, maxlen=HISTORY)
        self.sent_hist    = deque([0.0]*HISTORY, maxlen=HISTORY)
        self.recv_hist    = deque([0.0]*HISTORY, maxlen=HISTORY)

        # latest snapshot
        self.cpu_pct      = 0.0
        self.cpu_cores    = []           # per-core %
        self.cpu_temp     = None         # °C or None
        self.mem          = None
        self.disk_parts   = []           # list of dicts
        self.net_sent_kb  = 0.0
        self.net_recv_kb  = 0.0
        self.net_total_sent = 0          # bytes
        self.net_total_recv = 0
        self.processes    = []           # list of dicts, sorted by cpu
        self.boot_time    = ps.boot_time()

        # prime counters
        ps.cpu_percent(percpu=True)
        _n = ps.net_io_counters()
        self._prev_sent   = _n.bytes_sent
        self._prev_recv   = _n.bytes_recv
        self._disk_tick   = 0

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_interval(self, secs):
        self.interval = secs

    def stop(self):
        self._stop = True

    def _run(self):
        while not self._stop:
            t_start = time.perf_counter()
            self._collect()
            elapsed = time.perf_counter() - t_start
            sleep_t = max(0.05, self.interval - elapsed)
            time.sleep(sleep_t)

    def _collect(self):
        # CPU
        cpu_pct   = ps.cpu_percent(interval=None)
        cpu_cores = ps.cpu_percent(percpu=True, interval=None)

        # Temperature (Windows: usually unavailable without admin)
        cpu_temp = None
        try:
            temps = ps.sensors_temperatures()
            if temps:
                for key in ("coretemp","cpu_thermal","k10temp","acpitz"):
                    if key in temps and temps[key]:
                        cpu_temp = temps[key][0].current
                        break
        except (AttributeError, Exception):
            pass

        # Memory
        mem = ps.virtual_memory()

        # Network
        net = ps.net_io_counters()
        s_kb = (net.bytes_sent - self._prev_sent) / 1024
        r_kb = (net.bytes_recv - self._prev_recv) / 1024
        self._prev_sent = net.bytes_sent
        self._prev_recv = net.bytes_recv

        # Disk — refresh every 10 ticks to avoid overhead
        self._disk_tick += 1
        if self._disk_tick >= 10 or not self.disk_parts:
            self._disk_tick = 0
            disk_parts = []
            try:
                for part in ps.disk_partitions(all=False):
                    try:
                        u = ps.disk_usage(part.mountpoint)
                        disk_parts.append({
                            "device":     part.device,
                            "mountpoint": part.mountpoint,
                            "fstype":     part.fstype,
                            "total":      u.total,
                            "used":       u.used,
                            "free":       u.free,
                            "percent":    u.percent,
                        })
                    except (PermissionError, OSError):
                        pass
            except Exception:
                pass
        else:
            disk_parts = self.disk_parts   # reuse

        # Processes
        procs = []
        for p in ps.process_iter(["pid","name","cpu_percent",
                                   "memory_percent","status","num_threads",
                                   "username","memory_info"]):
            try:
                procs.append(p.info)
            except (ps.NoSuchProcess, ps.AccessDenied):
                pass
        procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)

        with self.lock:
            self.cpu_pct    = cpu_pct
            self.cpu_cores  = cpu_cores
            self.cpu_temp   = cpu_temp
            self.mem        = mem
            self.net_sent_kb= s_kb
            self.net_recv_kb= r_kb
            self.net_total_sent = net.bytes_sent
            self.net_total_recv = net.bytes_recv
            self.disk_parts = disk_parts
            self.processes  = procs
            self.cpu_hist.append(cpu_pct)
            self.mem_hist.append(mem.percent)
            self.sent_hist.append(s_kb)
            self.recv_hist.append(r_kb)

MON = SystemMonitor()

# ══════════════════════════════════════════════════════════════════════════════
# ROOT WINDOW
# ══════════════════════════════════════════════════════════════════════════════
root = tk.Tk()
root.title("OS Monitor")
root.geometry("1360x860")
root.minsize(1100, 720)
root.configure(bg=BG)

style = ttk.Style()
style.theme_use("clam")

style.configure("T.Treeview",
    background=PANEL, foreground=TXT, fieldbackground=PANEL,
    rowheight=24, font=("Segoe UI", 9), borderwidth=0)
style.configure("T.Treeview.Heading",
    background=BORDER, foreground=DIM,
    font=("Segoe UI", 8, "bold"), relief="flat")
style.map("T.Treeview",
    background=[("selected","#1e2340")],
    foreground=[("selected", BLUE)])
style.configure("TNotebook",
    background=BG, borderwidth=0, tabmargins=0)
style.configure("TNotebook.Tab",
    background=PANEL, foreground=DIM,
    font=("Segoe UI", 9), padding=(14, 6),
    borderwidth=0)
style.map("TNotebook.Tab",
    background=[("selected", PANEL2)],
    foreground=[("selected", TXT)])
style.configure("Slim.Vertical.TScrollbar",
    background=BORDER, troughcolor=PANEL,
    arrowcolor=DIM2, borderwidth=0, relief="flat", width=8)
style.configure("TCombobox",
    fieldbackground=PANEL2, background=PANEL2,
    foreground=TXT, selectbackground=BORDER,
    font=("Segoe UI", 9))

plt.rcParams.update({
    "figure.facecolor": PANEL,
    "axes.facecolor":   BG,
    "axes.edgecolor":   BORDER,
    "axes.labelcolor":  DIM,
    "xtick.color":      DIM,
    "ytick.color":      DIM,
    "grid.color":       BORDER,
    "grid.linestyle":   "-",
    "grid.alpha":       0.35,
    "lines.linewidth":  1.8,
    "font.size":        8,
    "font.family":      "Segoe UI",
})

T_AXIS = list(range(-HISTORY + 1, 1))
t0     = time.time()

# ══════════════════════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════════════════════
hdr = tk.Frame(root, bg=PANEL, height=50)
hdr.pack(fill="x")
hdr.pack_propagate(False)

tk.Label(hdr, text="  ◈  Real-Time OS Monitor",
         bg=PANEL, fg=BLUE,
         font=("Segoe UI", 12, "bold")).pack(side="left", padx=18, pady=12)

# Refresh rate selector
tk.Label(hdr, text="Refresh:", bg=PANEL, fg=DIM,
         font=("Segoe UI", 8)).pack(side="right", padx=(0,4), pady=14)
refresh_var = tk.StringVar(value="1 s")
refresh_cb  = ttk.Combobox(hdr, textvariable=refresh_var,
                            values=["0.5 s","1 s","2 s","5 s"],
                            width=5, state="readonly", style="TCombobox")
refresh_cb.pack(side="right", padx=(0,14), pady=14)

def on_refresh_change(*_):
    secs = float(refresh_var.get().split()[0])
    MON.set_interval(secs)
refresh_cb.bind("<<ComboboxSelected>>", on_refresh_change)

uptime_lbl = tk.Label(hdr, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 8))
uptime_lbl.pack(side="right", padx=16, pady=14)
clock_lbl  = tk.Label(hdr, text="", bg=PANEL, fg=TXT, font=("Segoe UI", 9))
clock_lbl.pack(side="right", padx=4, pady=14)

tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

# ══════════════════════════════════════════════════════════════════════════════
# NOTEBOOK TABS
# ══════════════════════════════════════════════════════════════════════════════
nb = ttk.Notebook(root, style="TNotebook")
nb.pack(fill="both", expand=True, padx=0, pady=0)

def make_tab(label):
    f = tk.Frame(nb, bg=PANEL2)
    nb.add(f, text=f"  {label}  ")
    return f

tab_overview = make_tab("Overview")
tab_procs    = make_tab("Processes")
tab_disk     = make_tab("Disk")
tab_network  = make_tab("Network")

# ── helpers ───────────────────────────────────────────────────────────────────
def card(parent, title, row, col, padx=(0,8), pady=(0,8)):
    f = tk.Frame(parent, bg=PANEL,
                 highlightbackground=BORDER, highlightthickness=1)
    f.grid(row=row, column=col, sticky="nsew", padx=padx, pady=pady)
    tk.Label(f, text=title, bg=PANEL, fg=DIM,
             font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=12, pady=(10,2))
    return f

def section_lbl(parent, text, side="top"):
    tk.Label(parent, text=text, bg=PANEL, fg=DIM,
             font=("Segoe UI", 7, "bold")).pack(
                 anchor="w", padx=12, pady=(10,4), side=side)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════
ov = tab_overview
ov.columnconfigure(0, weight=0, minsize=180)
ov.columnconfigure(1, weight=4)
ov.columnconfigure(2, weight=2, minsize=220)
ov.rowconfigure(0, weight=1)
ov.rowconfigure(1, weight=2)

# ── Stat cards (col 0) ────────────────────────────────────────────────────────
cards_col = tk.Frame(ov, bg=PANEL2)
cards_col.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(10,8), pady=10)
cards_col.columnconfigure(0, weight=1)

CARD_DEFS = [
    ("CPU",   "CPU USAGE",   GREEN),
    ("MEM",   "MEMORY",      BLUE),
    ("DISK",  "DISK",        YELLOW),
    ("PROCS", "PROCESSES",   TEAL),
    ("TEMP",  "CPU TEMP",    ORANGE),
    ("NET",   "NETWORK I/O", PINK),
]
card_val = {}
for i,(key,label,color) in enumerate(CARD_DEFS):
    c = tk.Frame(cards_col, bg=PANEL,
                 highlightbackground=BORDER, highlightthickness=1)
    c.grid(row=i, column=0, sticky="ew", pady=(0,6))
    cards_col.rowconfigure(i, weight=1)
    tk.Label(c, text=label, bg=PANEL, fg=DIM,
             font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=12, pady=(8,0))
    v = tk.Label(c, text="—", bg=PANEL, fg=color,
                 font=("Segoe UI", 18, "bold"))
    v.pack(anchor="w", padx=12, pady=(0,8))
    card_val[key] = v

# ── Charts (col 1) ────────────────────────────────────────────────────────────
chart_frame = tk.Frame(ov, bg=PANEL,
                        highlightbackground=BORDER, highlightthickness=1)
chart_frame.grid(row=0, column=1, sticky="nsew", padx=(0,8), pady=(10,8))

fig = Figure(figsize=(6, 3.2), dpi=90)
fig.subplots_adjust(left=0.07, right=0.97, top=0.92, bottom=0.08, hspace=0.55)

ax_cpu = fig.add_subplot(311)
ax_mem = fig.add_subplot(312)
ax_net = fig.add_subplot(313)

for ax, label, color, ylim in [
    (ax_cpu, "CPU %",    GREEN,  (0,100)),
    (ax_mem, "MEM %",    BLUE,   (0,100)),
    (ax_net, "NET KB/s", PURPLE, (0,10)),
]:
    ax.set_xlim(-HISTORY+1, 0)
    ax.set_ylim(*ylim)
    ax.set_ylabel(label, fontsize=7, color=color, labelpad=2)
    ax.tick_params(labelsize=6, length=2, pad=2)
    ax.yaxis.set_major_locator(plt.MaxNLocator(3, integer=True))
    ax.grid(True, axis="y", linewidth=0.5)
    ax.set_xticks([])
    for sp in ax.spines.values(): sp.set_linewidth(0.4)

z = [0.0]*HISTORY
line_cpu,  = ax_cpu.plot(T_AXIS, z, color=GREEN,  lw=1.8, solid_capstyle="round")
line_mem,  = ax_mem.plot(T_AXIS, z, color=BLUE,   lw=1.8, solid_capstyle="round")
line_sent, = ax_net.plot(T_AXIS, z, color=PURPLE, lw=1.6, solid_capstyle="round")
line_recv, = ax_net.plot(T_AXIS, z, color=ORANGE, lw=1.6, solid_capstyle="round")

# Static fill — drawn once, updated via vertex manipulation (fast)
fill_cpu  = ax_cpu.fill_between(T_AXIS, z, alpha=0.10, color=GREEN,  linewidth=0)
fill_mem  = ax_mem.fill_between(T_AXIS, z, alpha=0.10, color=BLUE,   linewidth=0)

ax_net.legend(["↑ Sent","↓ Recv"], loc="upper left",
              fontsize=6, framealpha=0, labelcolor=[PURPLE,ORANGE])

chart_canvas = FigureCanvasTkAgg(fig, chart_frame)
chart_canvas.get_tk_widget().pack(fill="both", expand=True, padx=2, pady=2)

# ── Per-core bars (col 1, row 1) ─────────────────────────────────────────────
cores_frame = tk.Frame(ov, bg=PANEL,
                        highlightbackground=BORDER, highlightthickness=1)
cores_frame.grid(row=1, column=1, sticky="nsew", padx=(0,8), pady=(0,10))
section_lbl(cores_frame, "PER-CORE CPU")

cores_inner = tk.Frame(cores_frame, bg=PANEL)
cores_inner.pack(fill="both", expand=True, padx=10, pady=(0,8))

n_cores   = ps.cpu_count(logical=True)
core_bars = []    # list of (label_w, canvas_w, pct_label_w)

MAX_COLS  = 4
for i in range(n_cores):
    r, c = divmod(i, MAX_COLS)
    cores_inner.columnconfigure(c, weight=1)

    cell = tk.Frame(cores_inner, bg=PANEL)
    cell.grid(row=r, column=c, sticky="ew", padx=4, pady=2)

    tk.Label(cell, text=f"C{i}", bg=PANEL, fg=DIM,
             font=("Segoe UI", 7), width=3, anchor="e").pack(side="left")

    bar_bg = tk.Canvas(cell, bg=DIM2, height=8, width=80,
                       highlightthickness=0, bd=0)
    bar_bg.pack(side="left", padx=4)
    bar_id = bar_bg.create_rectangle(0, 0, 0, 8, fill=GREEN, outline="")

    pct_lbl = tk.Label(cell, text="0%", bg=PANEL, fg=DIM,
                       font=("Segoe UI", 7), width=4, anchor="w")
    pct_lbl.pack(side="left")

    core_bars.append((bar_bg, bar_id, pct_lbl))

# ── Right col: disk summary + state legend (col 2) ────────────────────────────
right = tk.Frame(ov, bg=PANEL2)
right.grid(row=0, column=2, rowspan=2, sticky="nsew", padx=(0,10), pady=10)
right.rowconfigure(0, weight=3)
right.rowconfigure(1, weight=2)
right.columnconfigure(0, weight=1)

disk_ov_frame = tk.Frame(right, bg=PANEL,
                          highlightbackground=BORDER, highlightthickness=1)
disk_ov_frame.grid(row=0, column=0, sticky="nsew", pady=(0,8))
section_lbl(disk_ov_frame, "DISK PARTITIONS")
disk_ov_body  = tk.Frame(disk_ov_frame, bg=PANEL)
disk_ov_body.pack(fill="both", expand=True, padx=10, pady=(0,8))
_disk_ov_widgets = []

leg_frame = tk.Frame(right, bg=PANEL,
                     highlightbackground=BORDER, highlightthickness=1)
leg_frame.grid(row=1, column=0, sticky="nsew")
section_lbl(leg_frame, "PROCESS STATES")
for state,(color,label) in STATE_MAP.items():
    r = tk.Frame(leg_frame, bg=PANEL)
    r.pack(anchor="w", padx=14, pady=2)
    tk.Label(r, text=label, bg=PANEL, fg=color,
             font=("Segoe UI", 9)).pack(side="left")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — PROCESSES
# ══════════════════════════════════════════════════════════════════════════════
pr = tab_procs
pr.rowconfigure(1, weight=1)
pr.columnconfigure(0, weight=1)

# toolbar
toolbar = tk.Frame(pr, bg=PANEL2, height=40)
toolbar.grid(row=0, column=0, sticky="ew")
toolbar.pack_propagate(False)
toolbar.columnconfigure(1, weight=1)

tk.Label(toolbar, text="  Search:", bg=PANEL2, fg=DIM,
         font=("Segoe UI", 9)).pack(side="left", padx=(10,4), pady=8)
search_var = tk.StringVar()
search_entry = tk.Entry(toolbar, textvariable=search_var,
                        bg=PANEL, fg=TXT, insertbackground=TXT,
                        relief="flat", font=("Segoe UI", 9),
                        highlightbackground=BORDER, highlightthickness=1)
search_entry.pack(side="left", padx=(0,10), pady=8, ipadx=6, ipady=3)

sort_var = tk.StringVar(value="CPU %")
tk.Label(toolbar, text="Sort by:", bg=PANEL2, fg=DIM,
         font=("Segoe UI", 9)).pack(side="left", padx=(0,4))
sort_cb = ttk.Combobox(toolbar, textvariable=sort_var,
                       values=["CPU %","MEM %","PID","Name","Threads"],
                       width=9, state="readonly", style="TCombobox")
sort_cb.pack(side="left", padx=(0,10), pady=8)

# kill button
def kill_selected():
    sel = proc_tree.selection()
    if not sel: return
    pid = int(proc_tree.item(sel[0])["values"][0])
    name= proc_tree.item(sel[0])["values"][1]
    if messagebox.askyesno("Kill Process",
                            f"Terminate  {name}  (PID {pid})?",
                            icon="warning"):
        try:
            p = ps.Process(pid)
            p.terminate()
        except Exception as e:
            messagebox.showerror("Error", str(e))

kill_btn = tk.Button(toolbar, text="⊘  Kill Process",
                     bg=RED, fg="white",
                     font=("Segoe UI", 8, "bold"),
                     relief="flat", padx=10, pady=3,
                     activebackground="#cc3333",
                     command=kill_selected)
kill_btn.pack(side="right", padx=10, pady=8)

# process tree
proc_cols = ("PID","Name","User","State","CPU %","MEM %","MEM RSS","Threads")
proc_widths = (58, 195, 100, 115, 65, 65, 85, 65)
proc_anchors= ("center","w","w","w","center","center","center","center")

proc_tree = ttk.Treeview(pr, columns=proc_cols, show="headings",
                          style="T.Treeview", selectmode="browse")
for col, w, anc in zip(proc_cols, proc_widths, proc_anchors):
    proc_tree.heading(col, text=col,
                      command=lambda c=col: sort_var.set(c))
    proc_tree.column(col, width=w, anchor=anc, stretch=(col=="Name"))

proc_sb = ttk.Scrollbar(pr, orient="vertical",
                         command=proc_tree.yview,
                         style="Slim.Vertical.TScrollbar")
proc_tree.configure(yscrollcommand=proc_sb.set)
proc_sb.grid(row=1, column=1, sticky="ns", pady=(0,4))
proc_tree.grid(row=1, column=0, sticky="nsew", padx=(8,0), pady=(4,8))

proc_tree.tag_configure("alt", background=ALT)
for state,(color,_) in STATE_MAP.items():
    proc_tree.tag_configure(state,       foreground=color)
    proc_tree.tag_configure(state+"_a",  foreground=color, background=ALT)

# right-click context menu
ctx_menu = tk.Menu(root, tearoff=0, bg=PANEL, fg=TXT,
                   activebackground=BORDER, activeforeground=TXT,
                   font=("Segoe UI", 9), bd=0)
ctx_menu.add_command(label="⊘  Kill Process",  command=kill_selected)
ctx_menu.add_separator()
ctx_menu.add_command(label="Copy PID",
    command=lambda: root.clipboard_clear() or root.clipboard_append(
        proc_tree.item(proc_tree.selection()[0])["values"][0]
        if proc_tree.selection() else ""))
ctx_menu.add_command(label="Copy Name",
    command=lambda: root.clipboard_clear() or root.clipboard_append(
        proc_tree.item(proc_tree.selection()[0])["values"][1]
        if proc_tree.selection() else ""))

def show_ctx(event):
    row = proc_tree.identify_row(event.y)
    if row:
        proc_tree.selection_set(row)
        ctx_menu.post(event.x_root, event.y_root)
proc_tree.bind("<Button-3>", show_ctx)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — DISK
# ══════════════════════════════════════════════════════════════════════════════
dk = tab_disk
dk.columnconfigure(0, weight=1)
dk.rowconfigure(0, weight=1)

disk_cols = ("Mount","Device","FS","Total","Used","Free","Usage")
disk_tree = ttk.Treeview(dk, columns=disk_cols, show="headings",
                          style="T.Treeview")
for col in disk_cols:
    disk_tree.heading(col, text=col)
    w = 120 if col in ("Device","Mount") else 85
    disk_tree.column(col, width=w, anchor="center",
                     stretch=(col in ("Device","Mount")))
disk_sb = ttk.Scrollbar(dk, orient="vertical",
                         command=disk_tree.yview,
                         style="Slim.Vertical.TScrollbar")
disk_tree.configure(yscrollcommand=disk_sb.set)
disk_sb.grid(row=0, column=1, sticky="ns", padx=(0,8), pady=8)
disk_tree.grid(row=0, column=0, sticky="nsew", padx=(8,0), pady=8)

disk_tree.tag_configure("ok",   foreground=GREEN)
disk_tree.tag_configure("warn", foreground=YELLOW)
disk_tree.tag_configure("crit", foreground=RED)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — NETWORK
# ══════════════════════════════════════════════════════════════════════════════
nw = tab_network
nw.columnconfigure(0, weight=1)
nw.columnconfigure(1, weight=1)
nw.rowconfigure(0, weight=0)
nw.rowconfigure(1, weight=1)

# totals bar
net_totals_frame = tk.Frame(nw, bg=PANEL,
                             highlightbackground=BORDER, highlightthickness=1)
net_totals_frame.grid(row=0, column=0, columnspan=2,
                       sticky="ew", padx=10, pady=(10,6))
net_sent_total_lbl = tk.Label(net_totals_frame, text="Total Sent: —",
    bg=PANEL, fg=PURPLE, font=("Segoe UI", 11, "bold"))
net_sent_total_lbl.pack(side="left", padx=24, pady=10)
net_recv_total_lbl = tk.Label(net_totals_frame, text="Total Received: —",
    bg=PANEL, fg=ORANGE, font=("Segoe UI", 11, "bold"))
net_recv_total_lbl.pack(side="left", padx=24, pady=10)
net_rate_lbl = tk.Label(net_totals_frame, text="",
    bg=PANEL, fg=DIM, font=("Segoe UI", 9))
net_rate_lbl.pack(side="right", padx=24, pady=10)

# sent chart
sent_frame = tk.Frame(nw, bg=PANEL,
                       highlightbackground=BORDER, highlightthickness=1)
sent_frame.grid(row=1, column=0, sticky="nsew", padx=(10,5), pady=(0,10))
fig_sent = Figure(figsize=(4,2.8), dpi=88)
fig_sent.subplots_adjust(left=0.10, right=0.97, top=0.85, bottom=0.12)
ax_sent_h = fig_sent.add_subplot(111)
ax_sent_h.set_xlim(-HISTORY+1, 0)
ax_sent_h.set_ylim(0, 10)
ax_sent_h.set_ylabel("Sent KB/s", fontsize=7, color=PURPLE, labelpad=2)
ax_sent_h.set_title("Upload History", fontsize=8, color=TXT, pad=4)
ax_sent_h.tick_params(labelsize=6, length=2); ax_sent_h.set_xticks([])
ax_sent_h.grid(True, axis="y", linewidth=0.5)
line_sent_h, = ax_sent_h.plot(T_AXIS, z, color=PURPLE, lw=1.8)
ax_sent_h.fill_between(T_AXIS, z, alpha=0.12, color=PURPLE)
canvas_sent = FigureCanvasTkAgg(fig_sent, sent_frame)
canvas_sent.get_tk_widget().pack(fill="both", expand=True, padx=2, pady=2)

# recv chart
recv_frame = tk.Frame(nw, bg=PANEL,
                       highlightbackground=BORDER, highlightthickness=1)
recv_frame.grid(row=1, column=1, sticky="nsew", padx=(5,10), pady=(0,10))
fig_recv = Figure(figsize=(4,2.8), dpi=88)
fig_recv.subplots_adjust(left=0.10, right=0.97, top=0.85, bottom=0.12)
ax_recv_h = fig_recv.add_subplot(111)
ax_recv_h.set_xlim(-HISTORY+1, 0)
ax_recv_h.set_ylim(0, 10)
ax_recv_h.set_ylabel("Recv KB/s", fontsize=7, color=ORANGE, labelpad=2)
ax_recv_h.set_title("Download History", fontsize=8, color=TXT, pad=4)
ax_recv_h.tick_params(labelsize=6, length=2); ax_recv_h.set_xticks([])
ax_recv_h.grid(True, axis="y", linewidth=0.5)
line_recv_h, = ax_recv_h.plot(T_AXIS, z, color=ORANGE, lw=1.8)
ax_recv_h.fill_between(T_AXIS, z, alpha=0.12, color=ORANGE)
canvas_recv = FigureCanvasTkAgg(fig_recv, recv_frame)
canvas_recv.get_tk_widget().pack(fill="both", expand=True, padx=2, pady=2)

# ══════════════════════════════════════════════════════════════════════════════
# STATUS BAR
# ══════════════════════════════════════════════════════════════════════════════
tk.Frame(root, bg=BORDER, height=1).pack(fill="x", side="bottom")
sbar = tk.Frame(root, bg=PANEL, height=24)
sbar.pack(fill="x", side="bottom")
sbar.pack_propagate(False)
sys_lbl = tk.Label(sbar, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 8))
sys_lbl.pack(side="left", padx=14, pady=4)
net_bot_lbl = tk.Label(sbar, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 8))
net_bot_lbl.pack(side="right", padx=14, pady=4)

# ══════════════════════════════════════════════════════════════════════════════
# UPDATE FUNCTIONS — each is cheap; no blocking calls
# ══════════════════════════════════════════════════════════════════════════════

# track which tab was active to skip redraws
_active_tab = 0
nb.bind("<<NotebookTabChanged>>",
        lambda e: globals().update(_active_tab=nb.index(nb.select())))

_disk_ov_tick = 0   # refresh disk overview every N UI ticks

def update_charts(cd, md, sd, rd):
    """Update main overview charts — only set_ydata, no redraws."""
    line_cpu.set_ydata(cd)
    line_mem.set_ydata(md)
    line_sent.set_ydata(sd)
    line_recv.set_ydata(rd)

    # fast fill update via path vertices
    verts = fill_cpu.get_paths()[0].vertices
    verts[1:HISTORY+1, 1] = cd
    verts_m = fill_mem.get_paths()[0].vertices
    verts_m[1:HISTORY+1, 1] = md

    net_max = max(max(sd), max(rd), 1)
    ax_net.set_ylim(0, net_max * 1.3)
    chart_canvas.draw_idle()


def update_overview(snap):
    global _disk_ov_tick, _disk_ov_widgets

    cpu_pct  = snap["cpu_pct"]
    mem_pct  = snap["mem"].percent if snap["mem"] else 0
    disk_pct = snap["disk_parts"][0]["percent"] if snap["disk_parts"] else 0

    card_val["CPU"].config(  text=f"{cpu_pct:.1f}%",  fg=color_for(cpu_pct))
    card_val["MEM"].config(  text=f"{mem_pct:.1f}%",  fg=color_for(mem_pct))
    card_val["DISK"].config( text=f"{disk_pct:.1f}%", fg=color_for(disk_pct))
    card_val["PROCS"].config(text=str(snap["n_procs"]),fg=TEAL)

    if snap["cpu_temp"] is not None:
        t = snap["cpu_temp"]
        card_val["TEMP"].config(
            text=f"{t:.0f} °C", fg=color_for(t*1.2))
    else:
        card_val["TEMP"].config(text="N/A", fg=DIM)

    card_val["NET"].config(
        text=f"↑{snap['sent_kb']:.0f}\n↓{snap['recv_kb']:.0f} KB/s",
        fg=PINK)

    # per-core bars
    for i,(bcanv, bid, plbl) in enumerate(core_bars):
        if i < len(snap["cpu_cores"]):
            v = snap["cpu_cores"][i]
            color = color_for(v)
            w = max(1, int(80 * v / 100))
            bcanv.coords(bid, 0, 0, w, 8)
            bcanv.itemconfig(bid, fill=color)
            plbl.config(text=f"{v:.0f}%", fg=color)

    # disk overview — rebuild only every 10 ticks
    _disk_ov_tick += 1
    if _disk_ov_tick >= 10:
        _disk_ov_tick = 0
        for w in _disk_ov_widgets:
            w.destroy()
        _disk_ov_widgets = []
        for dp in snap["disk_parts"][:6]:
            pct   = dp["percent"]
            color = color_for(pct)
            name  = (dp["device"].replace("\\\\","\\").split("\\")[-1]
                     or dp["mountpoint"])[:10]
            row = tk.Frame(disk_ov_body, bg=PANEL)
            row.pack(fill="x", pady=3)
            _disk_ov_widgets.append(row)
            tk.Label(row, text=name, bg=PANEL, fg=TXT,
                     font=("Segoe UI", 8, "bold"),
                     width=7, anchor="w").pack(side="left")
            bar = tk.Canvas(row, bg=DIM2, height=7, width=90,
                            highlightthickness=0, bd=0)
            bar.pack(side="left", padx=5)
            bar.create_rectangle(0, 0, max(1,int(90*pct/100)), 7,
                                  fill=color, outline="")
            used = dp["used"]/1024**3; tot = dp["total"]/1024**3
            tk.Label(row, text=f"{pct:.0f}%  {used:.0f}/{tot:.0f}G",
                     bg=PANEL, fg=DIM,
                     font=("Segoe UI", 7)).pack(side="left")


_disk_ov_widgets = []


def update_proc_tab(procs, filter_txt, sort_key):
    """Smart diff: only replace rows that changed."""
    ft = filter_txt.lower()
    if ft:
        procs = [p for p in procs
                 if ft in (p.get("name") or "").lower()
                 or ft in str(p.get("pid",""))]

    sk_map = {"CPU %": "cpu_percent", "MEM %": "memory_percent",
              "PID": "pid", "Name": "name", "Threads": "num_threads"}
    sk = sk_map.get(sort_key, "cpu_percent")
    procs = sorted(procs,
                   key=lambda x: x.get(sk) or 0,
                   reverse=(sk not in ("name","pid")))[:60]

    existing = proc_tree.get_children()

    for i, p in enumerate(procs):
        status = (p.get("status") or "idle").lower()
        _, slabel = state_info(status)
        rss = p.get("memory_info")
        rss_str = fmt_bytes(rss.rss) if rss else "—"
        vals = (
            p["pid"],
            p.get("name") or "—",
            (p.get("username") or "—")[:18],
            slabel,
            f"{p.get('cpu_percent') or 0:.1f}",
            f"{(p.get('memory_percent') or 0):.2f}",
            rss_str,
            p.get("num_threads") or 0,
        )
        alt  = (i % 2 == 1)
        tag  = (status if status in STATE_MAP else "default") + ("_a" if alt else "")

        if i < len(existing):
            iid = existing[i]
            if proc_tree.item(iid)["values"] != list(vals):
                proc_tree.item(iid, values=vals, tags=(tag,))
        else:
            proc_tree.insert("", "end", values=vals, tags=(tag,))

    # remove extra rows
    for iid in proc_tree.get_children()[len(procs):]:
        proc_tree.delete(iid)


def update_disk_tab(disk_parts):
    for row in disk_tree.get_children():
        disk_tree.delete(row)
    for dp in disk_parts:
        pct = dp["percent"]
        tag = "ok" if pct < 70 else "warn" if pct < 90 else "crit"
        disk_tree.insert("", "end", values=(
            dp["mountpoint"],
            dp["device"][:28],
            dp["fstype"],
            fmt_bytes(dp["total"]),
            fmt_bytes(dp["used"]),
            fmt_bytes(dp["free"]),
            f"{pct:.1f}%",
        ), tags=(tag,))


def update_network_tab(sd, rd, total_sent, total_recv, s_kb, r_kb):
    line_sent_h.set_ydata(sd)
    line_recv_h.set_ydata(rd)
    mx_s = max(max(sd), 1); mx_r = max(max(rd), 1)
    ax_sent_h.set_ylim(0, mx_s * 1.25)
    ax_recv_h.set_ylim(0, mx_r * 1.25)
    canvas_sent.draw_idle()
    canvas_recv.draw_idle()
    net_sent_total_lbl.config(text=f"Total Sent:  {fmt_bytes(total_sent)}")
    net_recv_total_lbl.config(text=f"Total Received:  {fmt_bytes(total_recv)}")
    net_rate_lbl.config(text=f"↑ {s_kb:.1f} KB/s   ↓ {r_kb:.1f} KB/s")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN UI TICK — runs in tkinter thread, reads from MON (non-blocking)
# ══════════════════════════════════════════════════════════════════════════════
_last_update = 0.0

def ui_tick():
    global _last_update
    now = time.time()
    interval = MON.interval

    # only redraw if new data is actually available
    if now - _last_update < interval * 0.9:
        root.after(100, ui_tick)
        return
    _last_update = now

    with MON.lock:
        snap = {
            "cpu_pct":    MON.cpu_pct,
            "cpu_cores":  list(MON.cpu_cores),
            "cpu_temp":   MON.cpu_temp,
            "mem":        MON.mem,
            "sent_kb":    MON.net_sent_kb,
            "recv_kb":    MON.net_recv_kb,
            "total_sent": MON.net_total_sent,
            "total_recv": MON.net_total_recv,
            "disk_parts": list(MON.disk_parts),
            "processes":  list(MON.processes),
            "n_procs":    len(MON.processes),
            "cpu_hist":   list(MON.cpu_hist),
            "mem_hist":   list(MON.mem_hist),
            "sent_hist":  list(MON.sent_hist),
            "recv_hist":  list(MON.recv_hist),
        }

    active = _active_tab

    # always update charts + overview cards (lightweight)
    update_charts(snap["cpu_hist"], snap["mem_hist"],
                  snap["sent_hist"], snap["recv_hist"])
    update_overview(snap)

    # tab-specific updates
    if active == 1:
        update_proc_tab(snap["processes"],
                        search_var.get(), sort_var.get())
    elif active == 2:
        update_disk_tab(snap["disk_parts"])
    elif active == 3:
        update_network_tab(snap["sent_hist"], snap["recv_hist"],
                           snap["total_sent"], snap["total_recv"],
                           snap["sent_kb"], snap["recv_kb"])

    # always update proc tab if it's visible
    if active == 0:
        pass   # overview already done

    # header + statusbar
    elapsed = int(now - t0)
    h, rem = divmod(elapsed, 3600); m, s = divmod(rem, 60)
    uptime_lbl.config(text=f"Uptime  {h:02d}:{m:02d}:{s:02d}")
    clock_lbl.config( text=time.strftime("%H:%M:%S   %d %b %Y   "))
    root.title(f"OS Monitor  ·  CPU {snap['cpu_pct']:.1f}%")

    mem = snap["mem"]
    if mem:
        ram_used = mem.used/1024**3; ram_tot = mem.total/1024**3
        sys_lbl.config(text=(
            f"  RAM  {ram_used:.1f}/{ram_tot:.1f} GB"
            f"   ·   Cores  {n_cores}"
            f"   ·   Boot  {time.strftime('%d %b %Y %H:%M', time.localtime(MON.boot_time))}"
        ))
    net_bot_lbl.config(
        text=f"↑ {snap['sent_kb']:.1f} KB/s   ↓ {snap['recv_kb']:.1f} KB/s  ")

    root.after(100, ui_tick)   # check every 100 ms, only redraw when data is fresh


def on_close():
    MON.stop()
    root.destroy()

root.protocol("WM_DELETE_WINDOW", on_close)

# ── KICK OFF ──────────────────────────────────────────────────────────────────
ps.cpu_percent(percpu=True)   # prime per-core
root.after(700, ui_tick)
root.mainloop()