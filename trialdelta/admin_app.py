"""Launch with ``python -m trialdelta.admin_app`` from the repository directory."""

import json
import queue
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .worker_health import read_health


WORKER_HEALTH_POLL_MS = 5000
TAD_COLUMNS = (
    ("ticker", "Ticker", 80), ("tad", "Reported date", 135),
    ("precision", "Precision", 90), ("status", "Recognition", 125),
    ("form_type", "Source", 110), ("accession_no", "Accession number", 195),
    ("sources", "Sources", 90),
)


def _date(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%b %d, %Y %I:%M %p")
    except (ValueError, TypeError):
        return str(value)


def _bulk_date_range(row):
    since, through = row.get("updated_since"), row.get("updated_through")
    if not since and not through:
        return "All dates"
    return f"{since or 'Any date'} – {through or 'Present'}"


def _bulk_summary(row):
    from .bulk_download import safe_error_messages
    from .registry import safe_coverage_warnings

    status = row["status"]
    if status == "busy":
        return "Another registry collection is running. Try again when it finishes."
    summary = (
        f"Bulk run #{row.get('job_id', row.get('id', '—'))} {status}: "
        f"{row.get('pages') or 0:,} pages, {row.get('scanned') or 0:,} scanned, "
        f"{row.get('studies') or 0:,} trials processed, {row.get('skipped') or 0:,} skipped, "
        f"{row.get('changes') or 0:,} differences."
    )
    if status == "partial" and row.get("pages") == 0:
        summary += " Stopped before saving any pages."
    reasons = safe_error_messages(row.get("error_messages", row.get("errors", [])))
    if reasons:
        summary += " " + " ".join(reasons)
    warnings = safe_coverage_warnings(row.get("warning_messages", row.get("warnings", [])))
    if warnings:
        summary += " " + " ".join(warnings)
    if status in ("partial", "cancelled", "interrupted"):
        summary += " Select this run to resume from its saved checkpoint."
    elif status == "complete":
        summary += " Registry differences are visible in the change log."
    return summary


class AdminApp:
    def __init__(self, root, operations, initialize=True):
        self.root, self.ops = root, operations
        self.events = queue.Queue()
        self.busy = False
        self.identity = None
        self.buttons = []
        self.inbox_rows = {}
        self.tad_rows = {}
        self._tad_sort_column = None
        self._tad_sort_descending = False
        self.job_rows = {}
        self.bulk_rows = {}
        self._bulk_result_id = None
        self._preserve_bulk_status_on_refresh = False
        self._refreshing_bulk_selection = False
        self.bulk_cancel = None
        self.review_key = None
        self.review_urls = []
        self._login_window = None
        self._tad_entry_window = None
        self._tad_edit_selection = None
        self.tad_entry_fields = {}
        self.tad_entry_controls = []
        self._worker_health_after = None
        self._worker_health_state = None
        self._closed = False
        root.title("TrialDelta · Administrator")
        width = min(1240, max(760, root.winfo_screenwidth() - 64))
        height = min(880, max(580, root.winfo_screenheight() - 100))
        root.geometry(f"{width}x{height}")
        root.minsize(min(960, width), min(700, height))
        root.configure(bg="#f4f6f8")
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._style()
        self._build()
        self._poll_worker_health()
        root.after(100, self._poll)
        self._set_enabled(False)
        if initialize:
            self._submit(self.ops.initialize_desktop, self._initialized, "Connecting to configured database")

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#f4f6f8", foreground="#172c3c")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("Card.TFrame", background="white", relief="solid", borderwidth=1)
        style.configure("Card.TLabel", background="white")
        style.configure("Metric.TLabel", background="white", foreground="#0d756c", font=("Segoe UI", 26, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Muted.TLabel", foreground="#607380")
        style.configure("TButton", padding=(12, 8))
        style.configure("Accent.TButton", background="#0d756c", foreground="white")
        style.map("Accent.TButton", background=[("active", "#095c56"), ("disabled", "#afc6c3")])
        style.configure("TNotebook", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 11))
        style.map("TNotebook.Tab", background=[("selected", "white")])
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=30, borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), padding=(5, 9))
        style.map("Treeview", background=[("selected", "#d7eae6")], foreground=[("selected", "#163d36")])

    def _button(self, parent, text, command, accent=False):
        button = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "TButton")
        self.buttons.append(button)
        return button

    def _tree(self, parent, columns, selectmode="browse"):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode=selectmode)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=55, stretch=True)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _build(self):
        self._build_worker_health()
        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 14))
        title = ttk.Frame(header)
        title.pack(side="left")
        ttk.Label(title, text="TrialDelta", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title, text="Administrator workspace  ·  Collection, database health & publication", style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
        self._button(header, "Refresh database view", self.refresh).pack(side="right")
        self.connection = tk.StringVar(value="Connecting…")
        ttk.Label(outer, textvariable=self.connection, style="Muted.TLabel").pack(anchor="w", pady=(0, 14))

        cards = ttk.Frame(outer)
        cards.pack(fill="x", pady=(0, 14))
        self.metrics = {}
        for index, (key, label) in enumerate((
            ("companies", "COMPANIES"), ("studies", "TRIALS"), ("snapshots", "SNAPSHOTS"),
            ("approved", "CHANGE LOG ENTRIES"), ("pending", "AWAITING REVIEW"), ("inbox", "SOURCE CANDIDATES"),
        )):
            cards.columnconfigure(index, weight=1)
            card = ttk.Frame(cards, style="Card.TFrame", padding=(14, 11))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 7, 0))
            ttk.Label(card, text=label, style="Card.TLabel", font=("Segoe UI", 8, "bold")).pack(anchor="w")
            self.metrics[key] = tk.StringVar(value="—")
            ttk.Label(card, textvariable=self.metrics[key], style="Metric.TLabel").pack(anchor="w")
        self.last_job = tk.StringVar(value="Latest registry run: not loaded")
        ttk.Label(outer, textvariable=self.last_job, style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        collection = ttk.Frame(notebook, padding=16)
        bulk = ttk.Frame(notebook, padding=16)
        tads = ttk.Frame(notebook, padding=16)
        review = ttk.Frame(notebook, padding=16)
        inbox = ttk.Frame(notebook, padding=16)
        history = ttk.Frame(notebook, padding=16)
        for frame, name in ((collection, "Collection"), (bulk, "Bulk download"), (tads, "Target action dates"), (review, "Review & publish"), (inbox, "Source inbox"), (history, "Run history")):
            notebook.add(frame, text=name)
        self._build_collection(collection)
        self._build_bulk(bulk)
        self._build_tads(tads)
        self._build_review(review)
        self._build_inbox(inbox)
        self._build_history(history)
        notebook.bind("<<NotebookTabChanged>>", lambda event: self._refresh_tads()
                      if notebook.select() == str(tads) and self.identity and not self.busy else None)

        log_header = ttk.Frame(outer)
        log_header.pack(fill="x", pady=(12, 4))
        ttk.Label(log_header, text="SESSION LOG", style="Muted.TLabel", font=("Segoe UI", 9, "bold")).pack(side="left")
        self.status = tk.StringVar(value="Ready")
        ttk.Label(log_header, textvariable=self.status, style="Muted.TLabel").pack(side="right")
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 5))
        self.log = ScrolledText(outer, height=5, wrap="word", font=("Consolas", 9), bg="#172c3c", fg="#dceae8", relief="flat", padx=10, pady=8, state="disabled")
        self.log.pack(fill="x")
        self._append_log("Desktop opened. Collection runs only when you start it.")

    def _build_worker_health(self):
        self.worker_health_banner = tk.Frame(self.root, padx=24, pady=8)
        self.worker_health_banner.pack(fill="x")
        self.worker_health_title = tk.StringVar(value="Checking daily worker…")
        self.worker_health_detail = tk.StringVar()
        self.worker_health_times = tk.StringVar()
        self._worker_health_title_label = tk.Label(
            self.worker_health_banner, textvariable=self.worker_health_title,
            anchor="w", justify="left", font=("Segoe UI", 10, "bold"),
        )
        self._worker_health_title_label.pack(fill="x")
        self._worker_health_detail_label = tk.Label(
            self.worker_health_banner, textvariable=self.worker_health_detail,
            anchor="w", justify="left", font=("Segoe UI", 10),
        )
        self._worker_health_times_label = tk.Label(
            self.worker_health_banner, textvariable=self.worker_health_times,
            anchor="w", justify="left", font=("Segoe UI", 9),
        )
        self._worker_health_times_label.pack(fill="x")
        self.worker_health_banner.bind("<Configure>", self._resize_worker_health)

    def _resize_worker_health(self, event):
        for label in (self._worker_health_title_label, self._worker_health_detail_label,
                      self._worker_health_times_label):
            label.configure(wraplength=max(200, event.width - 48))

    def _poll_worker_health(self):
        self._worker_health_after = None
        if self._closed:
            return
        try:
            health = read_health()
        except Exception:
            # Monitoring remains available even when the database or health file
            # is unavailable. Do not display paths or exception contents.
            health = {"state": "unknown", "detail": "Worker health could not be read."}
        titles = {
            "not_started": "Daily worker: not started",
            "starting": "Daily worker: starting",
            "collecting": "Daily worker: collecting",
            "waiting": "Daily worker: waiting for the next collection",
            "stopped": "Daily worker: stopped normally",
            "failed": "DAILY WORKER STOPPED UNEXPECTEDLY",
            "unresponsive": "DAILY WORKER IS NOT RESPONDING",
            "unknown": "DAILY WORKER STATUS NEEDS ATTENTION",
        }
        state = health.get("state", "unknown")
        if state not in titles:
            state = "unknown"
        alert = state in {"failed", "unresponsive", "unknown"}
        neutral = state in {"not_started", "stopped"}
        background = "#a61b1b" if alert else ("#e8edf1" if neutral else "#e1f1eb")
        foreground = "#ffffff" if alert else "#172c3c"
        self.worker_health_banner.configure(bg=background, pady=14 if alert else 6)
        for label in (self._worker_health_title_label, self._worker_health_detail_label,
                      self._worker_health_times_label):
            label.configure(bg=background, fg=foreground)
        self._worker_health_title_label.configure(font=("Segoe UI", 16 if alert else 10, "bold"))
        self.worker_health_title.set(titles[state])
        self.worker_health_detail.set(health.get("detail") or "Check the worker logs and restart it when ready.")
        if alert:
            self._worker_health_detail_label.pack(fill="x", pady=(4, 4), before=self._worker_health_times_label)
        else:
            self._worker_health_detail_label.pack_forget()
        self.worker_health_times.set(
            f"Last heartbeat (local): {_date(health.get('heartbeat_at'))}  ·  "
            f"Next collection (local): {_date(health.get('next_run_at'))}"
        )
        if state != self._worker_health_state:
            self._append_log(titles[state] + (f". {self.worker_health_detail.get()}" if alert else "."))
            self._worker_health_state = state
        self._worker_health_after = self.root.after(WORKER_HEALTH_POLL_MS, self._poll_worker_health)

    def _build_collection(self, parent):
        ttk.Label(parent, text="ClinicalTrials.gov collection", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Exact lead-sponsor matching · All conditions and study types · Baselines create no alerts", style="Muted.TLabel").pack(anchor="w", pady=(3, 10))
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(0, 10))
        self._button(toolbar, "Collect selected companies", self._collect_selected, True).pack(side="left")
        self._button(toolbar, "Collect all active companies", lambda: self._collect(None)).pack(side="left", padx=8)
        self._button(toolbar, "Import news candidates", self._import_news).pack(side="right")
        ttk.Label(parent, text="Use Ctrl / Shift to select multiple companies. Collection uses active coverage; inactive companies retain their saved history.", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.companies = self._tree(parent, [
            ("ticker", "Ticker", 75), ("name", "Company", 180), ("coverage", "Coverage", 125), ("studies", "Trials", 60),
            ("freshness", "Freshness", 95), ("last_checked", "Last successful check (local)", 185), ("error", "Collection status", 285),
        ], "extended")
        self.companies.tag_configure("error", foreground="#a34133")
        self.companies.tag_configure("inactive", foreground="#607380")

    def _build_tads(self, parent):
        ttk.Label(parent, text="Target action dates", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Select Unresolved, then Edit selected to fill in a date. If no record was recognized, use Add date. Month-only dates retain their precision.", style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(3, 10))
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(0, 8))
        self._button(toolbar, "Import stored filings", self._import_tads, True).pack(side="left")
        self._button(toolbar, "Add date", self._add_tad).pack(side="left", padx=(8, 0))
        self._button(toolbar, "Refresh saved dates", self._refresh_tads).pack(side="left", padx=8)
        self._button(toolbar, "Open source", self._open_tad_source).pack(side="right")
        self.tad_delete_button = self._button(toolbar, "Delete selected", self._delete_tad)
        self.tad_delete_button.pack(side="right", padx=8)
        self.tad_edit_button = self._button(toolbar, "Edit selected", self._edit_tad)
        self.tad_edit_button.pack(side="right")
        filters = ttk.Frame(parent)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="Show:").pack(side="left", padx=(0, 8))
        self.tad_view = tk.StringVar(value="All saved")
        self.tad_view_selector = ttk.Combobox(
            filters, textvariable=self.tad_view, values=("All saved", "Unresolved"),
            state="readonly", width=18,
        )
        self.tad_view_selector.pack(side="left")
        self.tad_view_selector.bind("<<ComboboxSelected>>", lambda event: self._refresh_tads())
        ttk.Label(filters, text="Click a column heading to sort; click again to reverse.", style="Muted.TLabel").pack(side="left", padx=16)
        self.tad_notice = tk.StringVar(value="Import stored filings or select Add date. Saved dates appear in the website's date list and calendar.")
        ttk.Label(parent, textvariable=self.tad_notice, style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(0, 8))
        self.tad_count = tk.StringVar(value="Saved results have not been loaded.")
        ttk.Label(parent, textvariable=self.tad_count, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        self.tads = self._tree(parent, TAD_COLUMNS)
        for key, _label, _width in TAD_COLUMNS:
            self.tads.heading(key, command=lambda column=key: self._sort_tads(column))
        self.tads.bind("<<TreeviewSelect>>", self._show_tad)
        self.tad_text = ScrolledText(parent, height=5, wrap="word", state="disabled", font=("Segoe UI", 10))
        self.tad_text.pack(fill="x", pady=(10, 0))

    def _add_tad(self):
        self._open_tad_entry()

    def _edit_tad(self):
        if not self.identity or self.busy:
            return
        selected = self.tads.selection()
        row = self.tad_rows.get(selected[0]) if selected else None
        if not row:
            messagebox.showinfo("Edit target action date", "Select a saved date or unresolved record first.", parent=self.root)
            return
        self._open_tad_entry(row)

    def _open_tad_entry(self, row=None):
        if not self.identity or self.busy:
            return
        if self._tad_entry_window is not None:
            self._tad_entry_window.lift()
            return
        self._tad_edit_selection = dict(row) if row else None
        editing = row is not None
        window = self._tad_entry_window = tk.Toplevel(self.root)
        title = "Edit target action date" if editing else "Add target action date"
        window.title(title)
        window.geometry("600x620" if editing else "600x600")
        window.minsize(500, 580 if editing else 560)
        window.transient(self.root)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW", self._close_tad_entry)
        window.bind("<Escape>", lambda event: self._close_tad_entry())
        panel = ttk.Frame(window, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text=title, style="Section.TLabel").pack(anchor="w")
        explanation = "Enter an exact date or a month and year. Saved dates appear in the calendar."
        if editing:
            count = row.get("occurrence_count", 1) if row["status"] == "identified" else 1
            explanation = (f"Changes apply to the {count:,} saved record{'s' if count != 1 else ''} in this row. "
                           "Original records and prior edits are retained. The updated date appears in the calendar.")
        ttk.Label(panel, text=explanation,
                  style="Muted.TLabel", wraplength=530).pack(anchor="w", pady=(4, 14))
        self.tad_entry_fields = {}
        for key, label in (("ticker", "Ticker *"), ("tad", "Date *  (YYYY-MM-DD or YYYY-MM)"),
                           ("source_url", "Source link (optional)")):
            ttk.Label(panel, text=label).pack(anchor="w")
            entry = ttk.Entry(panel)
            entry.pack(fill="x", pady=(3, 10))
            self.tad_entry_fields[key] = entry
        ttk.Label(panel, text="Notes or source excerpt (optional)").pack(anchor="w")
        notes = ScrolledText(panel, height=4, wrap="word", font=("Segoe UI", 10))
        notes.pack(fill="both", expand=True, pady=(3, 12))
        self.tad_entry_fields["notes"] = notes
        if editing:
            for key, value in (("ticker", row["ticker"]), ("tad", row["tad"] or ""),
                               ("source_url", row.get("filing_link") or "")):
                self.tad_entry_fields[key].insert(0, value)
            notes.insert("1.0", row.get("context") or "")
        actions = ttk.Frame(panel)
        actions.pack(fill="x")
        save = ttk.Button(actions, text="Save changes" if editing else "Save date", style="Accent.TButton", command=self._save_tad_entry)
        save.pack(side="right")
        cancel = ttk.Button(actions, text="Cancel", command=self._close_tad_entry)
        cancel.pack(side="right", padx=(0, 8))
        self.tad_entry_controls = [save, cancel]
        self.tad_entry_fields["ticker"].focus_set()

    def _close_tad_entry(self):
        if self.busy or self._tad_entry_window is None:
            return
        self._tad_entry_window.grab_release()
        self._tad_entry_window.destroy()
        self._tad_entry_window = None
        self._tad_edit_selection = None
        self.tad_entry_fields = {}
        self.tad_entry_controls = []

    def _save_tad_entry(self):
        if not self.identity or self.busy or self._tad_entry_window is None:
            return
        values = {key: entry.get() for key, entry in self.tad_entry_fields.items() if key != "notes"}
        values["notes"] = self.tad_entry_fields["notes"].get("1.0", "end-1c")
        user_id = self.identity["id"]
        selection = self._tad_edit_selection

        def done(result):
            self._close_tad_entry()
            if selection:
                action = "unchanged" if result["status"] == "unchanged" else "updated"
                message = f"Date {action}: {result['ticker']} · {result['tad'] or 'Unidentified'}."
            else:
                action = "already saved" if result["status"] == "existing" else "saved"
                message = f"Manual date {action}: {result['ticker']} · {result['tad']}."
                if result.get("is_edited"):
                    message += " Its saved correction is shown."
            self.tad_notice.set(message)
            self._append_log(message)
            self._refresh_tads()

        if selection:
            self._submit(lambda: self.ops.edit_tad(
                user_id, selection["id"], expected_identity=selection.get("selection_identity"), **values,
            ), done, "Updating selected target action date")
        else:
            self._submit(lambda: self.ops.add_tad(user_id, **values), done, "Saving manual target action date")

    def _refresh_tads(self):
        if not self.identity or self.busy:
            return
        user_id = self.identity["id"]
        filters = {"status": "unresolved"} if self.tad_view.get() == "Unresolved" else {}
        self._submit(lambda: self.ops.tad_items(user_id, **filters), self._show_tads, "Reading saved target action dates")

    def _show_tads(self, result):
        self.tad_rows = {str(row["id"]): row for row in result["items"]}
        self.tads.delete(*self.tads.get_children())
        for key, row in self.tad_rows.items():
            method = row.get("entry_method", "filing")
            source = "Edited entry" if row.get("is_edited") else "Manual entry" if method == "manual" else "Filing + manual" if method == "mixed" else row["form_type"]
            self.tads.insert("", "end", iid=key, values=(row["ticker"], row["tad"] or "Unidentified", row["precision"], row["status"], source, row.get("accession_no") or "—", row["source_count"] + row.get("manual_count", 0)))
        self._apply_tad_sort()
        self.tad_count.set(f"{self.tad_view.get()}: {result['total']:,} saved results · {result['identified']:,} identified ({result['month']:,} month-only) · {result['unresolved']:,} unresolved · Showing {len(self.tad_rows):,}")
        empty_message = ("No unresolved target action dates. Select All saved to see identified dates, or Add date to enter a missing result."
                         if self.tad_view.get() == "Unresolved" else "No saved target action dates. Import stored filings or add a date manually.")
        self._replace_text(self.tad_text, "Select a date to inspect its evidence." if self.tad_rows else empty_message)
        self._update_tad_selection_state()

    def _sort_tads(self, column):
        self._tad_sort_descending = not self._tad_sort_descending if column == self._tad_sort_column else False
        self._tad_sort_column = column
        self._apply_tad_sort()

    def _apply_tad_sort(self):
        column = self._tad_sort_column
        if column is None:
            return

        def sort_value(key):
            if column == "sources":
                return int(self.tads.set(key, column))
            if column in {"tad", "accession_no"}:
                # ISO dates retain month precision; accession numbers retain leading zeros.
                return self.tad_rows[key].get(column)
            return self.tads.set(key, column).casefold()

        values, missing = [], []
        for key in self.tads.get_children():
            value = sort_value(key)
            if value is None or value == "":
                missing.append(key)
            else:
                values.append((key, value))
        values.sort(key=lambda item: item[1], reverse=self._tad_sort_descending)
        # Keep unavailable dates/accessions last in either direction, and move existing
        # items so selection, focus, and the evidence/actions still refer to the same row.
        for index, key in enumerate([key for key, _value in values] + missing):
            self.tads.move(key, "", index)
        for key, label, _width in TAD_COLUMNS:
            arrow = (" ▼" if self._tad_sort_descending else " ▲") if key == column else ""
            self.tads.heading(key, text=label + arrow)

    def _update_tad_selection_state(self, *, allowed=True):
        selected = self.tads.selection()
        enabled = bool(allowed and self.identity and not self.busy and selected and selected[0] in self.tad_rows)
        self.tad_delete_button.configure(state="normal" if enabled else "disabled")
        self.tad_edit_button.configure(state="normal" if enabled else "disabled")

    def _show_tad(self, event=None):
        self._update_tad_selection_state()
        selected = self.tads.selection()
        row = self.tad_rows.get(selected[0]) if selected else None
        if not row:
            return
        if row.get("is_edited"):
            self._replace_text(self.tad_text, f"{row['ticker']} · Edited entry · {row['tad']} ({row['precision']})\n{row['reason']}\n\n{row['context'] or 'No notes supplied.'}\n\nSource: {row.get('filing_link') or 'Not supplied'}\nEdited: {row.get('edited_at') or 'Not supplied'}\n\nOriginal saved date: {row.get('original_tad') or 'Unidentified'}\nOriginal evidence: {row.get('original_context') or 'No excerpt supplied.'}\nOriginal source: {row.get('original_filing_link') or 'Not supplied'}")
            return
        if row.get("evidence_method", row.get("entry_method")) == "manual":
            self._replace_text(self.tad_text, f"{row['ticker']} · Manual entry · {row['tad']} ({row['precision']})\n{row['reason']}\n\n{row['context'] or 'No notes supplied.'}\n\nSource: {row.get('filing_link') or 'Not supplied'}\nSaved: {row['imported_at']}")
            return
        self._replace_text(self.tad_text, f"{row['ticker']} · {row['form_type']} · {row['tad'] or 'Unidentified'} ({row['precision']})\n{row['reason']}\n\n{row['context']}\n\nAccession: {row.get('accession_no') or 'Not supplied'}\nFiled: {row.get('filed_at') or 'Not supplied'}\nSource: {row.get('filing_link') or 'Not supplied'}\nImported: {row['imported_at']}\nFiling SHA-256: {row['filing_sha256']}")

    def _open_tad_source(self):
        selected = self.tads.selection()
        row = self.tad_rows.get(selected[0]) if selected else None
        if not row or not row.get("filing_link"):
            messagebox.showinfo("Open source", "Select a result with a source link.", parent=self.root)
            return
        self._open_url(row["filing_link"])

    def _delete_tad(self):
        if not self.identity or self.busy:
            return
        selected = self.tads.selection()
        row = self.tad_rows.get(selected[0]) if selected else None
        if not row:
            messagebox.showinfo("Delete target action date", "Select a saved date or unresolved record first.", parent=self.root)
            return
        item_id, user_id = row["id"], self.identity["id"]
        expected_identity = row.get("selection_identity")
        label = f"{row['ticker']} · {row['tad'] or 'Unidentified'}"
        if row["status"] == "identified":
            count = row.get("occurrence_count", 1)
            scope = (f"This row represents {count:,} saved record{'s' if count != 1 else ''}. "
                     "All manual and filing records grouped under this ticker and date will be removed.")
        else:
            scope = "Only this unresolved saved record will be removed."
        message = (f"Delete {label}?\n\n{scope}\n\n"
                   "Original filings will stay unchanged. Later imports can recreate filing-derived records.")
        if not messagebox.askyesno("Delete target action date", message, parent=self.root,
                                   icon="warning", default="no"):
            return

        def done(result):
            count = result["deleted"]
            notice = f"Deleted {result['ticker']} · {result['tad'] or 'Unidentified'}: {count:,} saved record{'s' if count != 1 else ''} removed."
            self.tad_notice.set(notice)
            self._append_log(notice)
            self._refresh_tads()

        self._submit(lambda: self.ops.delete_tad(user_id, item_id, expected_identity=expected_identity),
                     done, "Deleting selected target action date")

    def _import_tads(self):
        if not self.identity or self.busy:
            return

        def done(result):
            if result["status"] == "complete":
                message = (f"Import complete: {result['scanned']:,} filings scanned; "
                           f"{result['imported']:,} new results, {result['existing']:,} already saved; "
                           f"{result['unresolved']:,} unresolved occurrences.")
                if not result["scanned"]:
                    message += " The source table is empty."
            else:
                message = result.get("detail", "The filing import could not complete. Saved dates are retained.")
            self.tad_notice.set(message)
            self._append_log(message)
            self._refresh_tads()

        self._submit(lambda: self.ops.run_tad_import(self.identity["id"]), done, "Importing target action dates from stored filings")

    def _build_bulk(self, parent):
        ttk.Label(parent, text="ClinicalTrials.gov bulk download", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Refreshes Healthcare coverage from the market database before each new download. Downloads matching trials, saving each page; baselines create no alerts.", style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(3, 10))
        filters = ttk.Frame(parent)
        filters.pack(fill="x", pady=(0, 8))
        self.bulk_scope = tk.StringVar(value="healthcare")
        self.bulk_healthcare = ttk.Radiobutton(filters, text="Healthcare companies (market coverage)", variable=self.bulk_scope, value="healthcare")
        self.bulk_healthcare.pack(side="left", padx=(0, 12))
        self.bulk_all = ttk.Radiobutton(filters, text="All active companies", variable=self.bulk_scope, value="all")
        self.bulk_all.pack(side="left")
        self.bulk_selected = ttk.Radiobutton(filters, text="Selected in Collection tab", variable=self.bulk_scope, value="selected")
        self.bulk_selected.pack(side="left", padx=(12, 18))
        dates = ttk.Frame(parent)
        dates.pack(fill="x", pady=(0, 8))
        ttk.Label(dates, text="Updated since (optional):").pack(side="left")
        self.bulk_since = ttk.Entry(dates, width=13)
        self.bulk_since.pack(side="left", padx=(8, 0))
        ttk.Label(dates, text="Updated through (optional):").pack(side="left", padx=(18, 0))
        self.bulk_through = ttk.Entry(dates, width=13)
        self.bulk_through.pack(side="left", padx=(8, 0))
        ttk.Label(dates, text="YYYY-MM-DD", style="Muted.TLabel").pack(side="left", padx=8)
        ttk.Label(parent, text="Filter by the registry's latest update date; both dates are included. Blank start = any date; blank end = present. Downloads current records.", style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(0, 8))
        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        self._button(controls, "Start bulk download", self._start_bulk, True).pack(side="left")
        self.bulk_cancel_button = ttk.Button(controls, text="Cancel", command=self._cancel_bulk, state="disabled")
        self.bulk_cancel_button.pack(side="left", padx=8)
        self._button(controls, "Refresh healthcare company list", self._discover_bulk_companies).pack(side="left")
        self._button(controls, "Resume selected run", self._resume_bulk).pack(side="right")
        self.bulk_status = tk.StringVar(value="Ready to download. Progress and page counts appear in the session log.")
        ttk.Label(parent, textvariable=self.bulk_status, style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(0, 6))
        ttk.Label(parent, text="Resume uses saved companies, filters and the page checkpoint. Current inputs apply only to a new download.", style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(0, 8))
        self.bulk_jobs = self._tree(parent, [
            ("id", "Run", 45), ("status", "Status", 85), ("started", "Started (local)", 175),
            ("range", "Updated range (inclusive)", 215), ("pages", "Pages", 60), ("scanned", "Scanned", 70),
            ("studies", "Trials processed", 100), ("skipped", "Skipped", 65), ("changes", "Differences", 85),
        ])
        self.bulk_jobs.tag_configure("error", foreground="#a34133")
        self.bulk_jobs.bind("<<TreeviewSelect>>", self._show_bulk_run)

    def _build_review(self, parent):
        ttk.Label(parent, text="Inspect evidence before publishing", style="Section.TLabel").pack(anchor="w")
        self.review_count = tk.StringVar(value="Pending registry differences and submitted company evidence")
        ttk.Label(parent, textvariable=self.review_count, style="Muted.TLabel").pack(anchor="w", pady=(3, 8))
        split = ttk.Panedwindow(parent, orient="vertical")
        split.pack(fill="both", expand=True)
        upper, lower = ttk.Frame(split), ttk.Frame(split)
        split.add(upper, weight=1)
        split.add(lower, weight=2)
        self.reviews = self._tree(upper, [("kind", "Type", 95), ("ticker", "Ticker", 65), ("study", "Trial", 105), ("title", "Pending observation", 400), ("date", "Source / detected date", 170)])
        self.reviews.bind("<<TreeviewSelect>>", self._load_review)
        self.review_text = ScrolledText(lower, wrap="word", height=8, font=("Consolas", 9), bg="white", relief="flat", padx=10, pady=10, state="disabled")
        self.review_text.pack(fill="both", expand=True, pady=(8, 6))
        self._replace_text(self.review_text, "Select an item to inspect its preserved source records.")
        note_row = ttk.Frame(parent)
        note_row.pack(fill="x", pady=(8, 6))
        ttk.Label(note_row, text="Review note (registry changes):").pack(side="left")
        self.review_note = ttk.Entry(note_row)
        self.review_note.pack(side="left", fill="x", expand=True, padx=(8, 0))
        controls = ttk.Frame(parent)
        controls.pack(fill="x")
        self._button(controls, "Approve & publish", lambda: self._review("approve"), True).pack(side="left")
        self._button(controls, "Reject", lambda: self._review("reject")).pack(side="left", padx=8)
        self._button(controls, "Open source", self._open_review_source).pack(side="right")

    def _build_inbox(self, parent):
        ttk.Label(parent, text="Unverified source candidates", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Latest 500 matched headlines within 30 days. Open the primary document and submit quoted evidence in the research app.", style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(3, 10))
        self.inbox = self._tree(parent, [("ticker", "Ticker", 65), ("title", "Headline", 570), ("source", "Source", 130), ("date", "Published", 150)])
        self.inbox.bind("<<TreeviewSelect>>", self._show_inbox)
        self.inbox_text = ScrolledText(parent, height=4, wrap="word", font=("Segoe UI", 10), bg="white", relief="flat", padx=10, pady=8, state="disabled")
        self.inbox_text.pack(fill="x", pady=8)
        controls = ttk.Frame(parent)
        controls.pack(fill="x")
        self._button(controls, "Open selected source", self._open_inbox, True).pack(side="left")
        self._button(controls, "Dismiss candidate", self._dismiss_inbox).pack(side="left", padx=8)
        self._button(controls, "Import news candidates", self._import_news).pack(side="right")

    def _build_history(self, parent):
        ttk.Label(parent, text="Registry collection runs", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Latest 100 runs from the database. Partial runs preserve each company's last good snapshots.", style="Muted.TLabel").pack(anchor="w", pady=(3, 10))
        self.history_status = tk.StringVar(value="Select a run to view its saved result and any failure reason.")
        ttk.Label(parent, textvariable=self.history_status, style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(0, 8))
        self.jobs = self._tree(parent, [
            ("id", "Run", 50), ("status", "Status", 90), ("started", "Started (local)", 180),
            ("finished", "Finished (local)", 180), ("companies", "Companies", 80), ("studies", "Trials", 70),
            ("changes", "Differences", 85), ("errors", "Errors", 65),
        ])
        self.jobs.tag_configure("error", foreground="#a34133")
        self.jobs.bind("<<TreeviewSelect>>", self._show_history_run)

    def _set_enabled(self, enabled):
        for button in self.buttons:
            if button.winfo_exists():
                button.configure(state="normal" if enabled else "disabled")
        for widget in (self.bulk_healthcare, self.bulk_all, self.bulk_selected, self.bulk_since, self.bulk_through):
            widget.configure(state="normal" if enabled else "disabled")
        self.bulk_cancel_button.configure(state="normal" if self.bulk_cancel is not None and not self.bulk_cancel.is_set() else "disabled")
        for widget in [*self.tad_entry_fields.values(), *self.tad_entry_controls]:
            widget.configure(state="normal" if enabled else "disabled")
        self.tad_view_selector.configure(state="readonly" if enabled and self.identity else "disabled")
        self._update_tad_selection_state(allowed=enabled)

    def _append_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 1500:
            self.log.delete("1.0", "251.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    @staticmethod
    def _replace_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _submit(self, task, success, label):
        if self.busy:
            return
        self.busy = True
        self._set_enabled(False)
        self.status.set(label + "…")
        self.progress.start(12)
        self._append_log(label + ".")

        def work():
            try:
                self.events.put(("done", task(), success, label))
            except Exception as exc:
                self.events.put(("error", self.ops.safe_error(exc), None, label))

        threading.Thread(target=work, name="trialdelta-admin-operation", daemon=False).start()

    def _poll(self):
        try:
            while True:
                kind, payload, callback, label = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                    continue
                if kind == "bulk_log":
                    self._append_log(payload)
                    self.bulk_status.set(payload)
                    continue
                self.busy = False
                was_bulk = self.bulk_cancel is not None
                self.bulk_cancel = None
                self.progress.stop()
                self._set_enabled(self.identity is not None or self._login_window is not None)
                if kind == "error":
                    self.status.set("Operation needs attention")
                    if was_bulk:
                        self.bulk_status.set("Download needs attention. Refresh the database view to inspect the saved run.")
                    self._append_log(payload)
                    messagebox.showerror(label, payload, parent=self._login_window or self._tad_entry_window or self.root)
                else:
                    self.status.set("Ready")
                    self._append_log(label + " — finished.")
                    if callback:
                        callback(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _initialized(self, info):
        self.connection.set(f"Database: {info['database'].upper()}  ·  No source collection on startup")
        if info["mode"] == "local":
            self._submit(self.ops.authenticate, self._signed_in, "Opening local administrator session")
        else:
            self._login()

    def _login(self):
        window = self._login_window = tk.Toplevel(self.root)
        window.title("Administrator sign-in")
        window.geometry("420x285")
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW", self._close)
        panel = ttk.Frame(window, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="Administrator sign-in", style="Section.TLabel").pack(anchor="w")
        ttk.Label(panel, text="Use an existing hosted administrator account.", style="Muted.TLabel").pack(anchor="w", pady=(4, 12))
        ttk.Label(panel, text="Email").pack(anchor="w")
        email = ttk.Entry(panel)
        email.pack(fill="x", pady=(0, 8))
        ttk.Label(panel, text="Password").pack(anchor="w")
        password = ttk.Entry(panel, show="•")
        password.pack(fill="x", pady=(0, 12))

        def login(event=None):
            if self.busy:
                return
            address, secret = email.get(), password.get()
            password.delete(0, "end")
            self._submit(lambda: self.ops.authenticate(address, secret), self._signed_in, "Verifying administrator")

        self._button(panel, "Sign in", login, True).pack(anchor="e")
        password.bind("<Return>", login)
        email.focus_set()

    def _signed_in(self, identity):
        self.identity = identity
        if self._login_window:
            self._login_window.destroy()
            self._login_window = None
        self.connection.set(f"Administrator: {identity['email']}  ·  {identity['mode'].title()} mode  ·  Direct database connection")
        self._set_enabled(True)
        self.refresh()

    def refresh(self):
        if not self.identity:
            return
        user_id = self.identity["id"]

        def load():
            return self.ops.dashboard_snapshot(user_id), self.ops.review_items(user_id), self.ops.inbox_items(user_id)

        self._submit(load, self._show_data, "Reading database metrics and queues")

    def _show_data(self, payload):
        snapshot, reviews, inbox = payload
        for key, variable in self.metrics.items():
            variable.set(f"{snapshot['counts'][key]:,}")
        self.review_count.set(f"{snapshot['counts']['pending_changes']} registry differences · {snapshot['counts']['pending_evidence']} submitted evidence records")
        latest = snapshot["jobs"][0] if snapshot["jobs"] else None
        self.last_job.set(
            f"Latest registry run: #{latest['id']} · {latest['status']} · {_date(latest['started_at'])}  |  Database: {snapshot['database'].upper()}"
            if latest else f"No registry collection runs yet.  |  Database: {snapshot['database'].upper()}"
        )
        selected = set(self.companies.selection())
        selected_jobs = set(self.jobs.selection())
        selected_bulk = set(self.bulk_jobs.selection())
        if self._preserve_bulk_status_on_refresh:
            self._preserve_bulk_status_on_refresh = False
            self._refreshing_bulk_selection = True
        if self._bulk_result_id is not None:
            selected_bulk = {str(self._bulk_result_id)}
            self._bulk_result_id = None
        for tree in (self.companies, self.reviews, self.inbox, self.jobs, self.bulk_jobs):
            tree.delete(*tree.get_children())
        for row in snapshot["companies"]:
            inactive = row.get("coverage_status", "active") != "active"
            self.companies.insert("", "end", iid=str(row["id"]), values=(row["ticker"], row["name"], row.get("coverage_label", "Active"), row["studies"], row["freshness"], _date(row["last_checked"]), row["error"] or ("History retained · Collection inactive" if inactive else "Ready")), tags=("error",) if row["error"] else ("inactive",) if inactive else ())
        self.companies.selection_set([key for key in selected if self.companies.exists(key)])
        self.review_key = None
        self.review_urls = []
        self.review_note.delete(0, "end")
        self._replace_text(self.review_text, "Select an item to inspect its preserved source records." if reviews else "The review queue is clear. New differences and submitted evidence will appear here.")
        for row in reviews:
            self.reviews.insert("", "end", iid=f"{row['kind']}:{row['id']}", values=(row["kind"].title(), row["ticker"], row["study_id"], row["title"], _date(row["date"])))
        self.inbox_rows = {str(row["id"]): row for row in inbox}
        for key, row in self.inbox_rows.items():
            self.inbox.insert("", "end", iid=key, values=(row["ticker"], row["title"], row["source"], _date(row["published_at"])))
        self._replace_text(self.inbox_text, "Select a headline to inspect its source URL." if inbox else "No unreviewed candidates. Use Import news candidates to read the optional news database.")
        self.job_rows = {str(row["id"]): row for row in snapshot["jobs"]}
        for row in self.job_rows.values():
            self.jobs.insert("", "end", iid=str(row["id"]), values=(row["id"], row["status"], _date(row["started_at"]), _date(row["finished_at"]), *[row[k] if row[k] is not None else "—" for k in ("companies", "studies", "changes")], row["errors"]), tags=("error",) if row["status"] in ("partial", "interrupted") else ())
        self.jobs.selection_set([key for key in selected_jobs if self.jobs.exists(key)])
        self._show_history_run()
        self.bulk_rows = {str(row["id"]): row for row in snapshot.get("bulk_jobs", [])}
        for key, row in self.bulk_rows.items():
            self.bulk_jobs.insert("", "end", iid=key, values=(row["id"], row["status"], _date(row["started_at"]), _bulk_date_range(row), *[row.get(k, 0) for k in ("pages", "scanned", "studies", "skipped", "changes")]), tags=("error",) if row["status"] in ("partial", "cancelled", "interrupted") else ())
        self.bulk_jobs.selection_set([key for key in selected_bulk if self.bulk_jobs.exists(key)])
        self._show_bulk_run()
        if self._refreshing_bulk_selection:
            # Rebuilding the table also queues TreeviewSelect events. Keep the
            # current operation's message until those automatic events finish.
            self.root.after_idle(self._finish_bulk_selection_refresh)

    def _finish_bulk_selection_refresh(self):
        self._refreshing_bulk_selection = False

    def _show_bulk_run(self, event=None):
        if self.bulk_cancel is not None or self._refreshing_bulk_selection:
            return
        selected = self.bulk_jobs.selection()
        if selected and (row := self.bulk_rows.get(selected[0])):
            self.bulk_status.set(_bulk_summary(row))

    def _show_history_run(self, event=None):
        selected = self.jobs.selection()
        row = self.job_rows.get(selected[0]) if selected else None
        if not row:
            self.history_status.set("Select a run to view its saved result and any failure reason.")
        elif row.get("kind") == "bulk":
            self.history_status.set(_bulk_summary(row))
        else:
            from .registry import safe_coverage_warnings

            summary = f"Registry run #{row['id']} {row['status']}."
            errors = row.get("errors", 0)
            if errors:
                summary += f" {errors} company refresh{'es' if errors != 1 else ''} failed; last good snapshots are retained."
            warnings = safe_coverage_warnings(row.get("warning_messages", []))
            if warnings:
                summary += " " + " ".join(warnings)
            self.history_status.set(summary)

    def _start_bulk(self):
        if self.busy or not self.identity:
            return
        ids = None
        if self.bulk_scope.get() == "selected":
            ids = [int(key) for key in self.companies.selection()]
            if not ids:
                messagebox.showinfo("Select companies", "Select one or more companies in the Collection tab first.", parent=self.root)
                return
        since = self.bulk_since.get().strip() or None
        through = self.bulk_through.get().strip() or None
        for label, value in (("updated-since", since), ("updated-through", through)):
            if not value:
                continue
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
                if parsed.strftime("%Y-%m-%d") != value:
                    raise ValueError("Date format")
            except ValueError:
                messagebox.showinfo(f"Check {label} date", "Enter a valid date in YYYY-MM-DD format, or leave this date blank for no limit.", parent=self.root)
                return
            if parsed > datetime.now(timezone.utc).date():
                messagebox.showinfo(f"Check {label} date", "Enter today or an earlier date, or leave this date blank for no limit.", parent=self.root)
                return
        if since and through and since > through:
            messagebox.showinfo("Check date range", "Updated since must be on or before Updated through. Use the same date in both fields to download one day.", parent=self.root)
            return
        self._run_bulk(company_ids=ids, updated_since=since, updated_through=through, discover_companies=self.bulk_scope.get() == "healthcare", oncology_only=False)

    def _discover_bulk_companies(self):
        if self.busy or not self.identity:
            return
        user_id = self.identity["id"]
        emit = lambda message: self.events.put(("bulk_log", message, None, ""))

        def done(result):
            summary = f"Healthcare company list refreshed: {result.get('discovered', 0):,} discovered, {result.get('created', 0):,} added, {result.get('updated', 0):,} updated, {result.get('skipped', 0):,} skipped."
            if any(key in result for key in ("reactivated", "delisted", "out_of_sector")):
                summary += (f" {result.get('reactivated', 0):,} reactivated, "
                            f"{result.get('delisted', 0):,} tagged delisted, "
                            f"{result.get('out_of_sector', 0):,} moved outside Healthcare. History retained.")
            self.bulk_status.set(summary)
            self._append_log(summary)
            self._preserve_bulk_status_on_refresh = True
            self.refresh()

        self._submit(lambda: self.ops.discover_bulk_companies(user_id, emit=emit), done, "Refreshing healthcare company list")

    def _resume_bulk(self):
        if self.busy or not self.identity:
            return
        selected = self.bulk_jobs.selection()
        row = self.bulk_rows.get(selected[0]) if selected else None
        if not row:
            messagebox.showinfo("Select a bulk run", "Select a saved bulk download to resume.", parent=self.root)
            return
        if row["status"] not in ("partial", "cancelled", "interrupted", "running"):
            messagebox.showinfo("Run is complete", "Choose an interrupted, partial or cancelled run. Start a new download to collect completed runs again.", parent=self.root)
            return
        if row.get("oncology_only"):
            self._append_log("This saved run retains a legacy specialty filter. Start a new download for all conditions and study types.")
        self._run_bulk(resume_job_id=row["id"])

    def _run_bulk(self, **options):
        user_id = self.identity["id"]
        self.bulk_cancel = cancel = threading.Event()
        resume_id = options.get("resume_job_id")
        label = f"Resuming bulk download #{resume_id}" if resume_id is not None else "Starting bulk download"
        self.bulk_status.set(label + "…")
        emit = lambda message: self.events.put(("bulk_log", message, None, ""))

        def done(result):
            summary = _bulk_summary(result)
            self._bulk_result_id = result.get("job_id")
            self._preserve_bulk_status_on_refresh = result["status"] == "busy"
            self.bulk_status.set(summary)
            self._append_log(summary)
            self.refresh()

        self._submit(lambda: self.ops.run_bulk_download(user_id, emit=emit, cancel=cancel, **options), done, label)

    def _cancel_bulk(self):
        if self.bulk_cancel is not None:
            self.bulk_cancel.set()
            self.bulk_cancel_button.configure(state="disabled")
            self.bulk_status.set("Cancellation requested. Waiting for the current page to finish saving.")
            self._append_log("Bulk cancellation requested. Saved progress can be resumed after this operation finishes.")

    def _collect_selected(self):
        ids = [int(key) for key in self.companies.selection()]
        if not ids:
            messagebox.showinfo("Select companies", "Select one or more companies in the table first.", parent=self.root)
            return
        self._collect(ids)

    def _collect(self, ids):
        user_id = self.identity["id"]
        label = "Collecting all active companies" if ids is None else f"Collecting {len(ids)} selected companies"
        emit = lambda message: self.events.put(("log", message, None, ""))

        def done(result):
            from .registry import safe_coverage_warnings

            self._append_log(f"Registry collection {result['status']}. Registry differences are visible in the change log.")
            for warning in safe_coverage_warnings(result.get("warnings", [])):
                self._append_log(warning)
            self.refresh()

        self._submit(lambda: self.ops.run_registry_sync(user_id, ids, emit), done, label)

    def _import_news(self):
        def done(result):
            status = result.get("status")
            if status == "unconfigured":
                self._append_log("News bridge is not configured. Set SOURCE_DATABASE_URL or use the local MySQL configuration.")
            else:
                self._append_log(f"News import {status}: {result.get('imported', 0)} new candidates. These are unverified discovery leads.")
            self.refresh()

        self._submit(lambda: self.ops.run_news_import(self.identity["id"]), done, "Importing news candidates")

    def _load_review(self, event=None):
        selected = self.reviews.selection()
        if self.busy or not selected:
            return
        key = selected[0]
        kind, item_id = key.split(":")

        def done(detail):
            if self.reviews.selection() != (key,):
                self._load_review()
                return
            self.review_key = key
            self.review_urls = []
            record = detail["record"]
            if detail["kind"] == "evidence":
                self.review_urls.append(record.get("url"))
            else:
                self.review_urls.extend(e.get("url") for e in detail.get("evidence", []))
                if record.get("study_id"):
                    self.review_urls.append(f"https://clinicaltrials.gov/study/{record['study_id']}")
            header = f"{detail['ticker']} · {record['title']}\n"
            if detail["kind"] == "change":
                header += f"\nPREVIOUS: {record.get('before_value')}\nCURRENT: {record.get('after_value')}\n"
            else:
                header += f"\nCLAIM: {record.get('value')}\nSOURCE DATE: {record.get('published_at')}\nQUOTE: {record.get('quote')}\n"
            self._replace_text(self.review_text, header + "\nPRESERVED SOURCE RECORDS\n" + json.dumps(detail, ensure_ascii=False, indent=2))

        self._submit(lambda: self.ops.review_detail(self.identity["id"], kind, int(item_id)), done, "Loading preserved evidence")

    def _review(self, action):
        selected = self.reviews.selection()
        if not selected or selected[0] != self.review_key:
            messagebox.showinfo("Inspect evidence", "Select an item and inspect its preserved evidence before reviewing it.", parent=self.root)
            return
        kind, item_id = selected[0].split(":")
        note = self.review_note.get()

        def done(result):
            self._append_log(f"{kind.title()} #{item_id}: {result['status']}.")
            self.refresh()

        self._submit(lambda: self.ops.review(self.identity["id"], kind, int(item_id), action, note), done, f"Reviewing {kind} #{item_id}")

    def _open_url(self, url):
        try:
            webbrowser.open(self.ops.public_source_url(url))
        except Exception as exc:
            messagebox.showerror("Open source", self.ops.safe_error(exc), parent=self.root)

    def _open_review_source(self):
        if self.review_urls and self.reviews.selection() == (self.review_key,):
            self._open_url(self.review_urls[0])
        else:
            messagebox.showinfo("Open source", "Select and inspect a record with a source URL first.", parent=self.root)

    def _show_inbox(self, event=None):
        selected = self.inbox.selection()
        if selected and (row := self.inbox_rows.get(selected[0])):
            self._replace_text(self.inbox_text, f"{row['title']}\n{row['url']}\nPublished {_date(row['published_at'])} · Unverified discovery candidate")

    def _open_inbox(self):
        selected = self.inbox.selection()
        if selected:
            self._open_url(self.inbox_rows[selected[0]]["url"])
        else:
            messagebox.showinfo("Select a candidate", "Select a source candidate first.", parent=self.root)

    def _dismiss_inbox(self):
        selected = self.inbox.selection()
        if not selected:
            messagebox.showinfo("Select a candidate", "Select a source candidate first.", parent=self.root)
            return
        item_id = int(selected[0])
        self._submit(lambda: self.ops.dismiss_inbox(self.identity["id"], item_id), lambda result: self.refresh(), "Dismissing source candidate")

    def _close(self):
        if self.busy:
            messagebox.showinfo("Operation in progress", "Wait for the current operation to finish before closing. Collection preserves the last good snapshots if a source fails.", parent=self._login_window or self.root)
            return
        self._closed = True
        if self._worker_health_after is not None:
            self.root.after_cancel(self._worker_health_after)
            self._worker_health_after = None
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        from . import admin_ops
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("TrialDelta startup", f"Cannot load database configuration ({type(exc).__name__}). Check the configured database settings. Technical details are hidden.", parent=root)
        root.destroy()
        return
    AdminApp(root, admin_ops)
    root.mainloop()


if __name__ == "__main__":
    main()
