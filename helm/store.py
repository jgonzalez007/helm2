"""One SQLite file. No credentials live here.

Search is an FTS5 index over the item, thread_msg and chat tables, written
alongside them rather than by triggers, so one code path owns what gets indexed.
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
  group_chat INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS sender_profile (
  addr TEXT PRIMARY KEY,
  is_automated INTEGER NOT NULL DEFAULT 0,
  expects_reply INTEGER NOT NULL DEFAULT 1,
  importance_hint TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  decided_by TEXT NOT NULL DEFAULT 'rules',
  confidence REAL,
  decided_at TEXT NOT NULL DEFAULT '',
  pinned INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS setting (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# Kept out of SCHEMA because _schema_columns() parses CREATE TABLE and a virtual
# table has no column declarations to compare against.
SEARCH_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  item_id UNINDEXED,
  field UNINDEXED,
  text,
  tokenize="unicode61 remove_diacritics 2"
);
"""

_JSON_COLS = ("attendees", "conflicts_with")
_BOOL_COLS = ("sender_external", "to_me_direct", "cc_me", "group_chat", "organizer_is_me", "presenter_is_me",
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


# Columns the user owns, not the source. A sweep rebuilds an Item from scratch, so
# without this a Done or Snoozed click is undone by the next poll.
_USER_OWNED = ("read_at", "replied_at", "snooze_until")


def _upsert_set(col: str) -> str:
    if col == "state":
        # A source may still move an item between needs_me and waiting as the thread
        # changes. It may not reopen something you closed.
        return ("state=CASE WHEN item.state IN ('done','snoozed') "
                "THEN item.state ELSE excluded.state END")
    if col in _USER_OWNED:
        return f"{col}=CASE WHEN item.{col}!='' THEN item.{col} ELSE excluded.{col} END"
    return f"{col}=excluded.{col}"


# FTS5 treats what the user types as an expression language: `AND`, `"`, `(`, `*`
# and `NEAR(` are all syntax errors, and a bare `*` is "unknown special query".
# Search-as-you-type sends a new query on every keystroke, so a raw string reaches
# MATCH mid-word constantly. Tokens are extracted and quoted instead of escaped —
# an allowlist, so there is no operator left to get wrong.
_TOKEN_RE = re.compile(r"[0-9A-Za-z_@.\-]+")


def fts_query(text: str) -> str:
    """A MATCH expression that cannot be a syntax error, from anything typed."""
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return ""
    # Quoting makes each token a phrase, so an address like health@aws.com matches
    # as its parts in order. The last token gets a prefix match so results appear
    # while the word is still being typed.
    quoted = [f'"{t}"' for t in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    return " ".join(quoted)


def _item_chunks(row) -> list[str]:
    """What is searchable about an item itself: subject, preview and who sent it."""
    return [f"{row['subject']} {row['sender']} {row['sender_addr']}".strip(),
            row["snippet"] or ""]


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
        self.search_available = True
        with self._conn() as c:
            c.executescript(SCHEMA)
            try:
                c.executescript(SEARCH_SCHEMA)
            except sqlite3.OperationalError as e:
                # A Python built without FTS5 loses search, not the app. Settings says so.
                self.search_available = False
                print(f"[helm] search index unavailable ({e}); the Search tab will say so")
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
        updates = ",".join(_upsert_set(c) for c in cols if c not in ("id", "source"))
        self._conn().execute(
            f"INSERT INTO item({','.join(cols)}, first_seen_at) "
            f"VALUES({placeholders}, :last_seen_at) "
            f"ON CONFLICT(source,id) DO UPDATE SET {updates}",
            d,
        )
        self.index_item(item.id, "item", [
            f"{item.subject} {item.sender} {item.sender_addr}".strip(),
            item.snippet,
        ])

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

    def wake_snoozed(self, now: str | None = None) -> list[str]:
        """Return snoozed items whose time has come, and put them back in the queue.

        Without this a snooze is a delete: nothing else ever clears the state.
        """
        at = now or now_iso()
        rows = self._conn().execute(
            "SELECT id FROM item WHERE state='snoozed' AND snooze_until!='' "
            "AND snooze_until<=?", (at,)).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            self._conn().execute(
                f"UPDATE item SET state='needs_me', snooze_until='' "
                f"WHERE id IN ({','.join('?' * len(ids))})", ids)
        return ids

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
        self.index_item(item_id, "thread",
                        [f"{m.get('author', '')} {m.get('body', '')}".strip()
                         for m in messages])

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
        # Chat accumulates, so this appends rather than replacing the field.
        if self.search_available:
            self._conn().execute(
                "INSERT INTO search_fts(item_id,field,text) VALUES(?,?,?)",
                (item_id, "chat", content))

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

    # ── sender profiles ─────────────────────────────────────────────────────
    #
    # What HELM has learned about a sender, so the same question is never asked
    # twice. A `pinned` row came from the user clicking an override and is never
    # overwritten by a classifier.

    def sender_profile(self, addr: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM sender_profile WHERE addr=?", ((addr or "").lower(),)).fetchone()
        return dict(row) if row else None

    def sender_profiles(self) -> dict[str, dict]:
        return {r["addr"]: dict(r)
                for r in self._conn().execute("SELECT * FROM sender_profile")}

    def set_sender_profile(self, addr: str, *, is_automated: bool | None = None,
                           expects_reply: bool | None = None,
                           importance_hint: str | None = None, reason: str = "",
                           decided_by: str = "rules", confidence: float | None = None,
                           pinned: bool = False) -> None:
        addr = (addr or "").lower()
        if not addr:
            return
        existing = self.sender_profile(addr) or {}
        # A pinned row is the user's own decision. Only another click may change it.
        if existing.get("pinned") and not pinned:
            return
        self._conn().execute(
            "INSERT INTO sender_profile(addr,is_automated,expects_reply,importance_hint,"
            "reason,decided_by,confidence,decided_at,pinned) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(addr) DO UPDATE SET is_automated=excluded.is_automated,"
            "expects_reply=excluded.expects_reply,importance_hint=excluded.importance_hint,"
            "reason=excluded.reason,decided_by=excluded.decided_by,"
            "confidence=excluded.confidence,decided_at=excluded.decided_at,"
            "pinned=excluded.pinned",
            (addr,
             int(existing.get("is_automated", 0) if is_automated is None else is_automated),
             int(existing.get("expects_reply", 1) if expects_reply is None else expects_reply),
             importance_hint if importance_hint is not None
             else existing.get("importance_hint", ""),
             reason or existing.get("reason", ""),
             decided_by, confidence, now_iso(), int(pinned or existing.get("pinned", 0))))

    def forget_sender(self, addr: str) -> None:
        self._conn().execute("DELETE FROM sender_profile WHERE addr=?",
                             ((addr or "").lower(),))

    # ── commitments ─────────────────────────────────────────────────────────

    def replace_commitments(self, rows: list[dict]) -> int:
        """Rewrite the open set, keeping anything you have already closed.

        Extraction re-runs over the same messages, so appending would multiply one
        promise by the number of sweeps.
        """
        c = self._conn()
        done = {r["text"] for r in c.execute(
            "SELECT text FROM commitment WHERE state!='open'")}
        c.execute("DELETE FROM commitment WHERE state='open'")
        kept = [r for r in rows if r["text"] not in done]
        c.executemany(
            "INSERT INTO commitment(item_id,text,owed_to,due_at,source_quote,"
            "state,detected_at) VALUES(?,?,?,?,?,'open',?)",
            [(r.get("item_id", ""), r["text"], r.get("owed_to", ""), r.get("due_at", ""),
              r.get("source_quote", ""), now_iso()) for r in kept])
        return len(kept)

    def commitments(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT id,item_id,text,owed_to,due_at,source_quote,state,detected_at "
            "FROM commitment ORDER BY (due_at='') , due_at, id")
        return [dict(r) for r in rows]

    def set_commitment_state(self, commitment_id: int, state: str) -> None:
        self._conn().execute("UPDATE commitment SET state=? WHERE id=?",
                             (state, commitment_id))

    def my_messages(self, limit: int = 400) -> list[dict]:
        """Everything in the store that you wrote. Nothing anyone else wrote.

        Commitment extraction must never read someone else's text, so the filter is
        the connector's own is_me rather than a name comparison.
        """
        rows = self._conn().execute(
            "SELECT t.item_id, t.body, t.sent_at, i.subject, i.sender_addr "
            "FROM thread_msg t LEFT JOIN item i ON i.id = t.item_id "
            "WHERE t.is_me = 1 ORDER BY t.id DESC LIMIT ?", (limit,))
        return [{"item_id": r["item_id"], "body": r["body"], "sent_at": r["sent_at"],
                 "to": r["sender_addr"] or ""} for r in rows]

    # ── search ──────────────────────────────────────────────────────────────

    def index_item(self, item_id: str, field: str, chunks: list[str]) -> None:
        """Replace everything indexed for one item and field.

        Called from upsert_item, set_thread and add_chat rather than from triggers:
        one code path decides what is searchable, and it is readable.
        """
        if not self.search_available:
            return
        c = self._conn()
        c.execute("DELETE FROM search_fts WHERE item_id=? AND field=?", (item_id, field))
        rows = [(item_id, field, t) for t in chunks if t and t.strip()]
        if rows:
            c.executemany("INSERT INTO search_fts(item_id,field,text) VALUES(?,?,?)", rows)

    def search(self, query: str, limit: int = 40) -> list[dict]:
        """Ranked hits, one per item, with the matching excerpt.

        Local, instant and offline: no connector call and no model call. The index is
        the only thing consulted.
        """
        if not self.search_available:
            return []
        match = fts_query(query)
        if not match:
            return []
        try:
            rows = self._conn().execute(
                "SELECT item_id, field, "
                "snippet(search_fts, 2, '‹', '›', '…', 12) AS excerpt, "
                "rank AS score "
                "FROM search_fts WHERE search_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit * 4)).fetchall()
        except sqlite3.OperationalError as e:
            # fts_query is meant to make this impossible; if it ever happens the
            # search box must stay usable rather than throw into the bridge.
            print(f"[helm] search query rejected: {e}")
            return []

        best: dict[str, dict] = {}
        for r in rows:
            if r["item_id"] in best:
                continue
            best[r["item_id"]] = {"item_id": r["item_id"], "field": r["field"],
                                  "excerpt": r["excerpt"], "score": r["score"]}
            if len(best) >= limit:
                break
        return list(best.values())

    def reindex(self) -> int:
        """Rebuild the whole index from the tables it mirrors.

        Needed once on a database written before phase 7, and after any change to
        what counts as searchable.
        """
        if not self.search_available:
            return 0
        c = self._conn()
        c.execute("DELETE FROM search_fts")
        n = 0
        for row in c.execute("SELECT id,subject,snippet,sender,sender_addr FROM item"):
            self.index_item(row["id"], "item", _item_chunks(row))
            n += 1
        threads: dict[str, list[str]] = {}
        for row in c.execute("SELECT item_id,author,body FROM thread_msg ORDER BY id"):
            threads.setdefault(row["item_id"], []).append(
                f"{row['author']} {row['body']}".strip())
        for item_id, chunks in threads.items():
            self.index_item(item_id, "thread", chunks)
        chats: dict[str, list[str]] = {}
        for row in c.execute("SELECT item_id,content FROM chat ORDER BY id"):
            chats.setdefault(row["item_id"], []).append(row["content"])
        for item_id, chunks in chats.items():
            self.index_item(item_id, "chat", chunks)
        return n

    def search_size(self) -> int:
        if not self.search_available:
            return 0
        return self._conn().execute("SELECT COUNT(*) AS n FROM search_fts").fetchone()["n"]

    def reset_demo(self) -> None:
        """Wipe everything a demo run created, keeping user settings and shortcuts."""
        c = self._conn()
        for t in ("item", "flag", "thread_msg", "chat", "draft", "action_log",
                  "commitment", "waiting_on", "source_state"):
            c.execute(f"DELETE FROM {t}")
        if self.search_available:
            c.execute("DELETE FROM search_fts")
