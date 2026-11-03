"""Cashed Risk Review - Windows desktop program.

Type an account ID or username and press Review (needs the account link from cashed.fun, set in Settings),
or paste the Trades table copied from the admin panel and press Review pasted trades.
The report opens in your browser and is saved in Documents/Cashed Risk Reviews.
"""
import json
import os
import re
import sys
import threading
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import messagebox, ttk

from risk_review import from_api, parse_export
from risk_report import CSS, build_html
from risk_scan import build_scan_html, load_csv, scan

APP = "Cashed Risk Review"
CONF_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), "CashedRiskReview")
CONF = os.path.join(CONF_DIR, "settings.json")
OUT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Cashed Risk Reviews")

BG, PANEL, FG, MUTED, ACCENT, LINE = "#0f141c", "#161d28", "#e6ebf3", "#93a0b4", "#7fb0ff", "#263142"


def load_settings() -> dict:
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"api_url": "https://www.cashed.fun/api/integrations/risk/account",
                "scan_url": "https://www.cashed.fun/api/integrations/risk/accounts", "api_key": ""}


def save_settings(s: dict):
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(CONF, "w", encoding="utf-8") as f:
        json.dump(s, f)


def fetch_account(query: str, s: dict) -> tuple[dict, list]:
    if not s.get("api_url") or not s.get("api_key"):
        raise RuntimeError("Add the account link and key in Settings first (Sam provides them).")
    q = query.strip().lstrip("@")
    param = "account" if re.match(r"(?i)^CSH-", q) else "username"
    url = f"{s['api_url']}?{urllib.parse.urlencode({param: q})}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {s['api_key']}", "User-Agent": "CashedRiskReview/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("The key was refused. Check it in Settings.")
        if e.code == 404:
            raise RuntimeError(f"No account found for '{q}'.")
        raise RuntimeError(f"cashed.fun answered with error {e.code}.")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach cashed.fun ({e.reason}).")
    accounts = data.get("accounts")
    if accounts:  # a username can have several accounts: review the one with the most trades
        best = max(accounts, key=lambda a: len(a.get("trades") or []))
        return best.get("account") or {}, from_api(best.get("trades") or [])
    return data.get("account") or {}, from_api(data.get("trades") or [])


def fetch_all(s: dict, progress) -> list:
    """Every account + trades, page by page: {accounts:[{account, trades}], next_page}."""
    if not s.get("scan_url") or not s.get("api_key"):
        raise RuntimeError("Add the 'all accounts' link and key in Settings first (Sam provides them).")
    out, page = [], 1
    while page:
        url = f"{s['scan_url']}?{urllib.parse.urlencode({'page': page})}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {s['api_key']}", "User-Agent": "CashedRiskReview/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError("The key was refused. Check it in Settings." if e.code == 401 else f"cashed.fun answered with error {e.code}.")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Couldn't reach cashed.fun ({e.reason}).")
        out += data.get("accounts") or []
        progress(f"Downloaded {len(out):,} accounts…")
        page = data.get("next_page")
    return out


def write_scan(entries, short_secs, progress) -> str:
    if not entries:
        raise RuntimeError("No accounts to scan.")
    progress(f"Scanning {len(entries):,} accounts…")
    res = scan(entries, short_secs)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"ALL-USERS-scan_{datetime.now():%Y-%m-%d_%H%M}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_scan_html(res, CSS))
    return path


def write_report(trades, account) -> str:
    if not trades:
        raise RuntimeError("No trades found. Copy the whole Trades table from the admin panel, including the market lines.")
    os.makedirs(OUT_DIR, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", (account.get("username") or account.get("id") or "account"))
    path = os.path.join(OUT_DIR, f"{name}_{datetime.now():%Y-%m-%d_%H%M}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_html(trades, account))
    return path


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.geometry("780x720")
        self.minsize(560, 480)
        self.configure(bg=BG)
        self.settings = load_settings()
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, font=("Segoe UI", 10))
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("H.TLabel", font=("Segoe UI Semibold", 16))
        st.configure("TButton", background=PANEL, foreground=FG, bordercolor=LINE, padding=(14, 7))
        st.map("TButton", background=[("active", LINE)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0b1220")
        st.map("Accent.TButton", background=[("active", "#a9c9ff")])
        st.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG, bordercolor=LINE, padding=6)

        wrap = ttk.Frame(self, padding=20)
        wrap.pack(fill="both", expand=True)
        top = ttk.Frame(wrap)
        top.pack(fill="x")
        ttk.Label(top, text="Cashed Risk Review", style="H.TLabel").pack(side="left")
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="Open reports folder", command=self.open_folder).pack(side="right", padx=8)

        ttk.Label(wrap, text="Look up an account", style="Muted.TLabel").pack(anchor="w", pady=(18, 4))
        row = ttk.Frame(wrap)
        row.pack(fill="x")
        self.query = ttk.Entry(row)
        self.query.pack(side="left", fill="x", expand=True)
        self.query.insert(0, "")
        self.query.bind("<Return>", lambda e: self.review_account())
        ttk.Button(row, text="Review", style="Accent.TButton", command=self.review_account).pack(side="left", padx=(8, 0))
        ttk.Label(wrap, text="Account ID (CSH-…) or @username", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        ttk.Label(wrap, text="…or paste the Trades table from the admin panel", style="Muted.TLabel").pack(anchor="w", pady=(18, 4))
        self.paste = tk.Text(wrap, height=12, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                             highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT,
                             font=("Consolas", 9), wrap="none")
        self.paste.pack(fill="both", expand=True)
        row2 = ttk.Frame(wrap)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="Username (optional, for the report title)", style="Muted.TLabel").pack(side="left")
        self.paste_name = ttk.Entry(row2, width=22)
        self.paste_name.pack(side="left", padx=8)
        ttk.Button(row2, text="Clear", command=lambda: self.paste.delete("1.0", "end")).pack(side="right")
        ttk.Button(row2, text="Review pasted trades", style="Accent.TButton",
                   command=self.review_paste).pack(side="right", padx=8)

        ttk.Label(wrap, text="Scan every user for hedging and short trades", style="Muted.TLabel").pack(anchor="w", pady=(18, 4))
        row3 = ttk.Frame(wrap)
        row3.pack(fill="x")
        ttk.Label(row3, text="Short trade = under").pack(side="left")
        self.short_secs = tk.Spinbox(row3, from_=5, to=300, width=5, bg=PANEL, fg=FG, buttonbackground=PANEL,
                                     insertbackground=FG, relief="flat")
        self.short_secs.delete(0, "end")
        self.short_secs.insert(0, "30")
        self.short_secs.pack(side="left", padx=6)
        ttk.Label(row3, text="seconds").pack(side="left")
        ttk.Button(row3, text="Scan all users", style="Accent.TButton", command=self.scan_all).pack(side="right")
        ttk.Button(row3, text="Scan a CSV file…", command=self.scan_csv).pack(side="right", padx=8)

        self.status = ttk.Label(wrap, text="Reports open in your browser and are saved in Documents › Cashed Risk Reviews.",
                                style="Muted.TLabel")
        self.status.pack(anchor="w", pady=(14, 0))

    def set_status(self, text):
        self.status.configure(text=text)

    def _done(self, path):
        self.set_status(f"Saved: {path}")
        webbrowser.open("file:///" + path.replace("\\", "/"))

    def _fail(self, msg):
        self.set_status(msg)
        messagebox.showerror(APP, msg)

    def review_account(self):
        q = self.query.get().strip()
        if not q:
            self._fail("Type an account ID or username first.")
            return
        self.set_status(f"Looking up {q}…")

        def work():
            try:
                account, trades = fetch_account(q, self.settings)
                path = write_report(trades, account)
                self.after(0, self._done, path)
            except Exception as e:
                self.after(0, self._fail, str(e))
        threading.Thread(target=work, daemon=True).start()

    def review_paste(self):
        try:
            trades = parse_export(self.paste.get("1.0", "end"))
            name = self.paste_name.get().strip().lstrip("@")
            path = write_report(trades, {"username": name} if name else {})
            self._done(path)
        except Exception as e:
            self._fail(str(e))

    def _secs(self):
        try:
            return max(1, int(self.short_secs.get()))
        except ValueError:
            return 30

    def scan_all(self):
        secs = self._secs()
        prog = lambda m: self.after(0, self.set_status, m)

        def work():
            try:
                path = write_scan(fetch_all(self.settings, prog), secs, prog)
                self.after(0, self._done, path)
            except Exception as e:
                self.after(0, self._fail, str(e))
        self.set_status("Downloading all accounts…")
        threading.Thread(target=work, daemon=True).start()

    def scan_csv(self):
        from tkinter import filedialog
        fn = filedialog.askopenfilename(title="Trades CSV", filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not fn:
            return
        secs = self._secs()
        prog = lambda m: self.after(0, self.set_status, m)

        def work():
            try:
                with open(fn, encoding="utf-8-sig") as f:
                    entries = load_csv(f.read())
                path = write_scan(entries, secs, prog)
                self.after(0, self._done, path)
            except Exception as e:
                self.after(0, self._fail, f"Couldn't scan that file: {e}")
        threading.Thread(target=work, daemon=True).start()

    def open_folder(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(OUT_DIR)  # noqa: S606
        else:
            webbrowser.open("file://" + OUT_DIR)

    def open_settings(self):
        w = tk.Toplevel(self)
        w.title("Settings")
        w.configure(bg=BG)
        w.geometry("560x290")
        f = ttk.Frame(w, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Account link (from Sam)", style="Muted.TLabel").pack(anchor="w")
        url = ttk.Entry(f)
        url.insert(0, self.settings.get("api_url", ""))
        url.pack(fill="x", pady=(2, 10))
        ttk.Label(f, text="All-accounts link (from Sam)", style="Muted.TLabel").pack(anchor="w")
        scan_url = ttk.Entry(f)
        scan_url.insert(0, self.settings.get("scan_url", ""))
        scan_url.pack(fill="x", pady=(2, 10))
        ttk.Label(f, text="Key (from Sam)", style="Muted.TLabel").pack(anchor="w")
        key = ttk.Entry(f, show="•")
        key.insert(0, self.settings.get("api_key", ""))
        key.pack(fill="x", pady=(2, 12))

        def save():
            self.settings = {"api_url": url.get().strip(), "scan_url": scan_url.get().strip(), "api_key": key.get().strip()}
            save_settings(self.settings)
            self.set_status("Settings saved.")
            w.destroy()
        ttk.Button(f, text="Save", style="Accent.TButton", command=save).pack(anchor="e")


if __name__ == "__main__":
    App().mainloop()
