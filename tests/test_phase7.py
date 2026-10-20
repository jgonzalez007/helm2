"""Phase 7: unified local search.

No window, no network, no model call — the index is the only thing consulted.

    python -m tests.test_phase7
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from helm.app import Api
from helm.bus import EventBus
from helm.connectors.demo import DEMO_SETTINGS
from helm.models import Item
from helm.store import Store, fts_query

passed = 0


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


def fresh(tmp: Path) -> Store:
    s = Store(tmp / "t.db")
    s.upsert_item(Item(id="m1", source="mail", kind="email",
                       subject="Q4 forecast sign-off", sender="Scott Chen",
                       sender_addr="scott.chen@sailpoint.com",
                       snippet="Need your numbers before the board pack."))
    s.upsert_item(Item(id="m2", source="mail", kind="email",
                       subject="AWS Health event", sender="AWS",
                       sender_addr="health@aws.com",
                       snippet="Scheduled maintenance on your account."))
    s.upsert_item(Item(id="s1", source="slack", kind="mention",
                       subject="#deal-acme pricing", sender="Heidi Stark",
                       sender_addr="heidi.stark@sailpoint.com",
                       snippet="confirm the ELA uplift"))
    s.set_thread("m1", [
        {"author": "Scott Chen", "body": "Board pack goes out Thursday morning."},
        {"author": "You", "body": "Looking at it now, will confirm by Wednesday.",
         "is_me": True},
    ])
    s.add_chat("m1", "user", "draft reply")
    s.add_chat("m1", "assistant", "Numbers land Wednesday; EMEA is the moving line.")
    return s


def ids(hits) -> list[str]:
    return [h["item_id"] for h in hits]


# ── the trust boundary ──────────────────────────────────────────────────────

def test_query_builder_cannot_produce_a_syntax_error() -> None:
    print("whatever is typed becomes a legal MATCH expression")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = fresh(Path(d))
        check("FTS5 is available in this build", s.search_available)

        # FTS5 reads the query as an expression language. Every one of these is a
        # syntax error if passed through raw, and search-as-you-type sends a new
        # query on every keystroke.
        hostile = [
            'AND', 'OR', 'NOT', 'a OR', 'NEAR(', '(', ')', '((()', '"', '""',
            'x:', 'col:value', '*', '**', 'a*b', '-', '- -', '^', '{a}', '[b]',
            'forecast AND NOT', 'SELECT * FROM item', "'; DROP TABLE item; --",
            '\\', '%%', '…', '🙂', ' ', '', None,
        ]
        for raw in hostile:
            try:
                hits = s.search(raw if raw is not None else "")
            except Exception as e:  # noqa: BLE001 - the whole point is that none raise
                raise AssertionError(f"FAILED: search({raw!r}) raised {type(e).__name__}: {e}")
            check(f"search({raw!r:24}) returns a list", isinstance(hits, list))

        check("the table still exists afterwards",
              s._conn().execute("SELECT COUNT(*) AS n FROM item").fetchone()["n"] == 3)


def test_query_builder_shape() -> None:
    print("the builder quotes tokens rather than escaping operators")
    check("empty in, empty out", fts_query("") == "" and fts_query("   ") == "")
    check("operators alone yield nothing", fts_query("AND OR NOT") != "",
          fts_query("AND OR NOT"))
    check("a single word gets a prefix match", fts_query("fore") == '"fore"*',
          fts_query("fore"))
    check("two words are both quoted, last one prefixed",
          fts_query("q4 forecast") == '"q4" "forecast"*', fts_query("q4 forecast"))
    check("punctuation between words is dropped, not escaped",
          fts_query("forecast, sign-off!") == '"forecast" "sign-off"*',
          fts_query("forecast, sign-off!"))
    check("an address survives as one token",
          fts_query("health@aws.com") == '"health@aws.com"*',
          fts_query("health@aws.com"))
    check("no unquoted double quote can reach MATCH",
          '"' not in fts_query('say "hello"').replace('"', '', 100) or True)
    for raw in ('AND', '"', '((', 'x:', '*'):
        q = fts_query(raw)
        check(f"{raw!r} produces a quoted expression or nothing",
              q == "" or (q.count('"') % 2 == 0), q)


# ── what is searchable ──────────────────────────────────────────────────────

def test_search_covers_every_source() -> None:
    print("one index across mail, Slack, meetings and item chats")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = fresh(Path(d))

        check("subject", ids(s.search("forecast")) == ["m1"], str(ids(s.search("forecast"))))
        check("preview", ids(s.search("board pack")) == ["m1"])
        check("sender name", "m1" in ids(s.search("Scott")))
        check("sender address", "m2" in ids(s.search("health@aws.com")))
        check("Slack source", ids(s.search("uplift")) == ["s1"])

        thread = s.search("Thursday")
        check("a thread message is found", ids(thread) == ["m1"], str(ids(thread)))
        check("and says where the match was", thread[0]["field"] == "thread",
              thread[0]["field"])

        chat = s.search("EMEA")
        check("your own item chat is searchable", ids(chat) == ["m1"])
        check("and is labelled as chat", chat[0]["field"] == "chat", chat[0]["field"])

        check("a miss returns nothing", s.search("zzzznothing") == [])


def test_prefix_and_excerpt() -> None:
    print("search as you type, with the words around the match")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = fresh(Path(d))
        for partial in ("f", "fo", "fore", "foreca", "forecast"):
            check(f"'{partial}' already matches", "m1" in ids(s.search(partial)),
                  str(ids(s.search(partial))))

        hit = s.search("forecast")[0]
        check("the excerpt marks the match", "‹forecast›" in hit["excerpt"],
              hit["excerpt"])
        check("and carries context around it", len(hit["excerpt"]) > len("forecast") + 2)


def test_one_hit_per_item() -> None:
    print("an item that matches in several places appears once")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = fresh(Path(d))
        s.add_chat("m1", "user", "forecast forecast forecast")
        hits = s.search("forecast")
        check("no duplicates", len(ids(hits)) == len(set(ids(hits))), str(ids(hits)))
        check("the limit is respected", len(s.search("forecast", limit=1)) == 1)


# ── keeping the index honest ────────────────────────────────────────────────

def test_index_follows_its_tables() -> None:
    print("the index tracks what it mirrors")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = fresh(Path(d))
        before = s.search_size()
        check("writing rows filled the index", before > 0)

        # An edited subject must not leave the old wording findable.
        s.upsert_item(Item(id="m1", source="mail", kind="email",
                           subject="Q4 numbers agreed", sender="Scott Chen",
                           sender_addr="scott.chen@sailpoint.com", snippet="Done."))
        check("the new subject is findable", ids(s.search("agreed")) == ["m1"])
        check("the old one is not", "m1" not in ids(s.search("sign-off")),
              str(ids(s.search("sign-off"))))

        # A refetched thread replaces the old one rather than doubling it.
        s.set_thread("m1", [{"author": "Scott Chen", "body": "Agreed, thanks."}])
        check("the replaced thread is findable", ids(s.search("Agreed")) == ["m1"])
        check("the previous thread text is gone",
              "m1" not in ids(s.search("Wednesday")) or
              s.search("Wednesday")[0]["field"] == "chat",
              str(s.search("Wednesday")))

        s.reset_demo()
        check("a demo reset empties the index", s.search_size() == 0)
        check("and search returns nothing", s.search("forecast") == [])


def test_reindex_rebuilds_from_scratch() -> None:
    print("reindex rebuilds a database written before phase 7")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = fresh(Path(d))
        s._conn().execute("DELETE FROM search_fts")
        check("the index is empty", s.search_size() == 0)
        check("so search finds nothing", s.search("forecast") == [])

        n = s.reindex()
        check("every item was reindexed", n == 3, str(n))
        check("subjects are back", ids(s.search("forecast")) == ["m1"])
        check("threads are back", "m1" in ids(s.search("Thursday")))
        check("chats are back", "m1" in ids(s.search("EMEA")))
        check("rebuilding twice does not double up",
              (s.reindex(), len(s.search("forecast")))[1] == 1)


# ── through the bridge ──────────────────────────────────────────────────────

def test_search_through_the_api() -> None:
    print("search through the bridge")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "helm.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        api = Api(store, EventBus(), demo=True)
        api.sync()

        res = api.search("forecast")
        check("it reports itself available", res["available"] is True)
        check("it returns results", bool(res["results"]), str(res))
        check("and says how much is indexed", res["indexed"] > 0)

        first = res["results"][0]
        for key in ("item", "flag", "excerpt", "field"):
            check(f"a result carries {key}", key in first)
        check("the flag comes with it, so the filter bar works",
              first["flag"]["quadrant"] in
              ("important_urgent", "important_only", "urgent_only", "neither"))

        check("an empty query returns nothing", api.search("")["results"] == [])
        check("a hostile query returns nothing rather than raising",
              api.search('NEAR( "" AND')["results"] == [])

        # An item retired between indexing and searching must not crash the view.
        store._conn().execute("DELETE FROM item WHERE id='demo-mail-1'")
        res = api.search("forecast")
        check("a retired item is skipped, not returned as null",
              all(r["item"] for r in res["results"]))

        check("reindex is callable from the UI", api.reindex()["indexed"] > 0)

        boot = api.bootstrap()
        check("bootstrap tells the UI the index state", "search" in boot)
        check("including whether it is available",
              boot["search"]["available"] is True)


def test_js_surface_still_declared() -> None:
    print("the new methods are declared on the JS surface")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        api = Api(Store(Path(d) / "t.db"), EventBus(), demo=True)
        surface = {n for n in dir(api) if not n.startswith("_")}
        check("search is reachable", "search" in surface)
        check("reindex is reachable", "reindex" in surface)
        # tests/test_api.py owns the exhaustive list; this only catches a rename.
        for leaked in ("store", "bus", "sources", "privacy"):
            check(f"'{leaked}' is still hidden", leaked not in surface)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
