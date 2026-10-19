"""One SQLite file. No credentials live here.

FTS5 search arrives in phase 7; the tables it indexes already exist.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from dataclasses import fields as dc_fields
from pathlib import Path

from .models import Draft, Flag, Item, SourceStatus, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS item (
  id TEXT NOT NULL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  sender_addr TEXT NOT NULL DEFAULT '',
  sender_external INTEGER NOT NULL DEFAULT 0,
  snippet TEXT NOT NULL DEFAULT '',
  thread_id TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL DEFAULT '',
  starts_at TEXT NOT NULL DEFAULT '',
  ends_at TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'needs_me',
  to_me_direct INTEGER NOT NULL DEFAULT 1,
  cc_me INTEGER NOT NULL DEFAULT 0,
  organizer_is_me INTEGER NOT NULL DEFAULT 0,
  presenter_is_me INTEGER NOT NULL DEFAULT 0,
  rsvp_pending INTEGER NOT NULL DEFAULT 0,
  has_agenda INTEGER NOT NULL DEFAULT 1,
  all_day INTEGER NOT NULL DEFAULT 0,
  attendees TEXT NOT NULL DEFAULT '[]',
  external_attendees INTEGER NOT NULL DEFAULT 0,
  join_url TEXT NOT NULL DEFAULT '',
  conflicts_with TEXT NOT NULL DEFAULT '[]',
  deadline_at TEXT NOT NULL DEFAULT '',
  sent_by_me_at TEXT NOT NULL DEFAULT '',
  last_reply_at TEXT NOT NULL DEFAULT '',
  read_at TEXT NOT NULL DEFAULT '',
  replied_at TEXT NOT NULL DEFAULT '',
  snooze_until TEXT NOT NULL DEFAULT '',
  first_seen_at TEXT NOT NULL DEFAULT '',
  last_seen_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS flag (
  item_id TEXT PRIMARY KEY,
  importance TEXT NOT NULL DEFAULT 'normal',
  urgency TEXT NOT NULL DEFAULT 'none',
  reasons TEXT NOT NULL DEFAULT '[]',
  decided_by TEXT NOT NULL DEFAULT 'rules',
  confidence REAL,
  pinned INTEGER NOT NULL DEFAULT 0,
  posture_warning TEXT NOT NULL DEFAULT '',
  evaluated_at TEXT NOT NULL DEFAULT '',
  recheck_after TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS thread_msg (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  sent_at TEXT NOT NULL DEFAULT '',
  is_me INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_thread_msg_item ON thread_msg(item_id);

CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_item ON chat(item_id);

CREATE TABLE IF NOT EXISTS draft (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  to_addr TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  approved_at TEXT NOT NULL DEFAULT '',
  sent_at TEXT NOT NULL DEFAULT '',
  undo_until TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'drafted'
);

CREATE TABLE IF NOT EXISTS action_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  verb TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  approved_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS commitment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  owed_to TEXT NOT NULL DEFAULT '',
  due_at TEXT NOT NULL DEFAULT '',
  source_quote TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open',
  detected_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS waiting_on (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  sent_at TEXT NOT NULL DEFAULT '',
  recipient TEXT NOT NULL DEFAULT '',
  nudge_after TEXT NOT NULL DEFAULT '',
  resurfaced_at TEXT NOT NULL DEFAULT '',
  followup_draft_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shortcut (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_name TEXT NOT NULL DEFAULT 'Work',
  label TEXT NOT NULL,
  url TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  hotkey TEXT NOT NULL DEFAULT '',
  favicon_blob BLOB,
  browser_profile TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_state (
  source TEXT PRIMARY KEY,
  health TEXT NOT NULL DEFAULT 'stale',
  last_ok_at TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  backoff_s INTEGER NOT NULL DEFAULT 0,
  measured_latency_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS setting (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_JSON_COLS = ("attendees", "conflicts_with")
_BOOL_COLS = ("sender_external", "to_me_direct", "cc_me", "organizer_is_me", "presenter_is_me",
              "rsvp_pending", "has_agenda", "all_day", "external_attendees")

DEFAULT_SETTINGS = {
    "theme": "system",
    "density": "comfortable",
    "professional_mode": False,
    "sounds": False,
    "reduced_motion": False,
    "first_run_done": False,
    "quiet_hours": {"start": "19:00", "end": "07:00"},
    "summary_times": {"brief": "08:00", "recap": "17:30"},
    "vips": [],
    "managers": [],
    "muted_senders": [],
    "muted_channels": [],
    "internal_domains": ["sailpoint.com"],
    "keyword_weights": {},
    "island_screen": "cursor",
}


_CREATE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", re.S)


def _schema_columns() -> dict[str, dict[str, str]]:
    """{table: {column: declaration}} read straight from SCHEMA, so the two cannot drift."""
    tables: dict[str, dict[str, str]] = {}
    for table, body in _CREATE_RE.findall(SCHEMA):
        cols: dict[str, str] = {}
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE")):
                continue
            name, _, decl = line.partition(" ")
            if name and decl:
                cols[name] = decl.strip()
        tables[table] = cols
    return tables


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "HELM"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else data_dir() / "helm.db"
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` never adds a column to an existing database.

        Rather than carry a migration framework, every column declared in SCHEMA with
        a literal default is added to a table that is missing it. A later phase that
        adds a field gets the upgrade for free; anything needing a backfill or a type
        change still needs real thought.
        """
        conn = self._conn()
        for table, columns in _schema_columns().items():
            try:
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.OperationalError:
                continue
            if not have:
                continue
            for name, decl in columns.items():
                if name in have:
                    continue
                if "DEFAULT" not in decl.upper() and "NOT NULL" in decl.upper():
                    # No safe value for existing rows: a human has to decide.
                    raise RuntimeError(
                        f"{table}.{name} is NOT NULL with no default; "
                        "add a migration rather than letting this pass silently")
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                print(f"[helm] migrated: added {table}.{name}")

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    # ── settings ────────────────────────────────────────────────────────────

    def get_setting(self, key: str, default=None):
        row = self._conn().execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
        if row is None:
            return DEFAULT_SETTINGS.get(key, default)
        return json.loads(row["value"])

    def set_setting(self, key: str, value) -> None:
        self._conn().execute(
            "INSERT INTO setting(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def all_settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for row in self._conn().execute("SELECT key,value FROM setting"):
            out[row["key"]] = json.loads(row["value"])
        return out

    # ── items ───────────────────────────────────────────────────────────────

    def upsert_item(self, item: Item) -> None:
        d = item.to_dict()
        for col in _JSON_COLS:
            d[col] = json.dumps(d[col])
        for col in _BOOL_COLS:
            d[col] = int(bool(d[col]))
        d["last_seen_at"] = now_iso()
        cols = list(d.keys())
        placeholders = ",".join(f":{c}" for c in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "source"))
        self._conn().execute(
            f"INSERT INTO item({','.join(cols)}, first_seen_at) "
            f"VALUES({placeholders}, :last_seen_at) "
            f"ON CONFLICT(source,id) DO UPDATE SET {updates}",
            d,
        )

    def _row_to_item(self, row: sqlite3.Row) -> Item:
        names = {f.name for f in dc_fields(Item)}
        d = {k: row[k] for k in row.keys() if k in names}
        for col in _JSON_COLS:
            d[col] = json.loads(d.get(col) or "[]")
        for col in _BOOL_COLS:
            d[col] = bool(d.get(col))
        return Item(**d)

    def items(self, source: str | None = None, states: tuple[str, ...] = ()) -> list[Item]:
        sql = "SELECT * FROM item WHERE 1=1"
        args: list = []
        if source:
            sql += " AND source=?"
            args.append(source)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args.extend(states)
        sql += " ORDER BY COALESCE(NULLIF(starts_at,''), received_at) ASC"
        return [self._row_to_item(r) for r in self._conn().execute(sql, args)]

    def item(self, item_id: str) -> Item | None:
        row = self._conn().execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def set_item_state(self, item_id: str, state: str, **extra) -> None:
        sets = ["state=?"]
        args: list = [state]
        for k, v in extra.items():
            sets.append(f"{k}=?")
            args.append(v)
        args.append(item_id)
        self._conn().execute(f"UPDATE item SET {','.join(sets)} WHERE id=?", args)

    # ── flags ───────────────────────────────────────────────────────────────

    def set_flag(self, flag: Flag) -> None:
        self._conn().execute(
            "INSERT INTO flag(item_id,importance,urgency,reasons,decided_by,confidence,"
            "pinned,posture_warning,evaluated_at,recheck_after) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET importance=excluded.importance,"
            "urgency=excluded.urgency,reasons=excluded.reasons,decided_by=excluded.decided_by,"
            "confidence=excluded.confidence,pinned=excluded.pinned,"
            "posture_warning=excluded.posture_warning,evaluated_at=excluded.evaluated_at,"
            "recheck_after=excluded.recheck_after",
            (flag.item_id, flag.importance, flag.urgency, json.dumps(flag.reasons),
             flag.decided_by, flag.confidence, int(flag.pinned), flag.posture_warning,
             flag.evaluated_at, flag.recheck_after),
        )

    def flag(self, item_id: str) -> Flag | None:
        row = self._conn().execute("SELECT * FROM flag WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            return None
        return Flag(
            item_id=row["item_id"], importance=row["importance"], urgency=row["urgency"],
            reasons=json.loads(row["reasons"]), decided_by=row["decided_by"],
            confidence=row["confidence"], pinned=bool(row["pinned"]),
            posture_warning=row["posture_warning"], evaluated_at=row["evaluated_at"],
            recheck_after=row["recheck_after"],
        )

    def flags(self) -> dict[str, Flag]:
        return {r["item_id"]: self.flag(r["item_id"])
                for r in self._conn().execute("SELECT item_id FROM flag")}

    # ── threads & chat ──────────────────────────────────────────────────────

    def set_thread(self, item_id: str, messages: list[dict]) -> None:
        c = self._conn()
        c.execute("DELETE FROM thread_msg WHERE item_id=?", (item_id,))
        c.executemany(
            "INSERT INTO thread_msg(item_id,author,body,sent_at,is_me) VALUES(?,?,?,?,?)",
            [(item_id, m.get("author", ""), m.get("body", ""), m.get("sent_at", ""),
              int(bool(m.get("is_me")))) for m in messages],
        )

    def thread(self, item_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT author,body,sent_at,is_me FROM thread_msg WHERE item_id=? ORDER BY id",
            (item_id,))
        return [{"author": r["author"], "body": r["body"], "sent_at": r["sent_at"],
                 "is_me": bool(r["is_me"])} for r in rows]

    def add_chat(self, item_id: str, role: str, content: str) -> None:
        self._conn().execute(
            "INSERT INTO chat(item_id,role,content,created_at) VALUES(?,?,?,?)",
            (item_id, role, content, now_iso()))

    def chat(self, item_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT role,content,created_at FROM chat WHERE item_id=? ORDER BY id", (item_id,))
        return [dict(r) for r in rows]

    # ── drafts & action log ─────────────────────────────────────────────────

    def add_draft(self, draft: Draft) -> None:
        self._conn().execute(
            "INSERT INTO draft(id,item_id,channel,to_addr,subject,body,created_at,status) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,"
            "subject=excluded.subject,status=excluded.status",
            (draft.id, draft.item_id, draft.channel, draft.to_addr, draft.subject,
             draft.body, draft.created_at or now_iso(), draft.status))

    def draft(self, draft_id: str) -> Draft | None:
        row = self._conn().execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
        if not row:
            return None
        return Draft(id=row["id"], item_id=row["item_id"], channel=row["channel"],
                     to_addr=row["to_addr"], subject=row["subject"], body=row["body"],
                     created_at=row["created_at"], status=row["status"])

    def log_action(self, verb: str, target: str, source: str, payload: str,
                   status: str, approved_by: str = "") -> None:
        # approved_by has exactly one legal non-empty value. Anything else is a bug,
        # and a silent write would hide it.
        if approved_by not in ("", "click"):
            raise ValueError(f"approved_by must be 'click' or empty, got {approved_by!r}")
        self._conn().execute(
            "INSERT INTO action_log(at,verb,target,source,payload,status,approved_by) "
            "VALUES(?,?,?,?,?,?,?)",
            (now_iso(), verb, target, source, payload, status, approved_by))

    def action_log(self, limit: int = 200) -> list[dict]:
        rows = self._conn().execute(
            "SELECT at,verb,target,source,payload,status,approved_by FROM action_log "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ── shortcuts ───────────────────────────────────────────────────────────

    def shortcuts(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT id,group_name,label,url,position,hotkey,browser_profile FROM shortcut "
            "ORDER BY position, id")
        return [dict(r) for r in rows]

    def add_shortcut(self, label: str, url: str, group_name: str = "Work",
                     hotkey: str = "", browser_profile: str = "") -> int:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("a shortcut must be an http or https URL")
        pos = self._conn().execute(
            "SELECT COALESCE(MAX(position),0)+1 AS p FROM shortcut").fetchone()["p"]
        cur = self._conn().execute(
            "INSERT INTO shortcut(group_name,label,url,position,hotkey,browser_profile) "
            "VALUES(?,?,?,?,?,?)", (group_name, label, url, pos, hotkey, browser_profile))
        return cur.lastrowid

    def update_shortcut(self, shortcut_id: int, **fields) -> None:
        if "url" in fields and not str(fields["url"]).lower().startswith(("http://", "https://")):
            raise ValueError("a shortcut must be an http or https URL")
        allowed = {"group_name", "label", "url", "position", "hotkey", "browser_profile"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        clause = ",".join(f"{k}=?" for k in sets)
        self._conn().execute(f"UPDATE shortcut SET {clause} WHERE id=?",
                             [*sets.values(), shortcut_id])

    def delete_shortcut(self, shortcut_id: int) -> None:
        self._conn().execute("DELETE FROM shortcut WHERE id=?", (shortcut_id,))

    # ── waiting-on ──────────────────────────────────────────────────────────

    def add_waiting(self, item_id: str, sent_at: str, recipient: str, nudge_after: str) -> None:
        self._conn().execute(
            "INSERT INTO waiting_on(item_id,sent_at,recipient,nudge_after) VALUES(?,?,?,?)",
            (item_id, sent_at, recipient, nudge_after))

    def waiting(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT item_id,sent_at,recipient,nudge_after,resurfaced_at FROM waiting_on "
            "ORDER BY sent_at")
        return [dict(r) for r in rows]

    # ── source status ───────────────────────────────────────────────────────

    def set_source_status(self, st: SourceStatus) -> None:
        self._conn().execute(
            "INSERT INTO source_state(source,health,last_ok_at,last_error,measured_latency_ms) "
            "VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET health=excluded.health,"
            "last_ok_at=excluded.last_ok_at,last_error=excluded.last_error,"
            "measured_latency_ms=excluded.measured_latency_ms",
            (st.source, st.health, st.last_ok_at, st.last_error, st.measured_latency_ms))

    def source_states(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT source,health,last_ok_at,last_error,measured_latency_ms FROM source_state")
        return [dict(r) for r in rows]

    def reset_demo(self) -> None:
        """Wipe everything a demo run created, keeping user settings and shortcuts."""
        c = self._conn()
        for t in ("item", "flag", "thread_msg", "chat", "draft", "action_log",
                  "commitment", "waiting_on", "source_state"):
            c.execute(f"DELETE FROM {t}")
