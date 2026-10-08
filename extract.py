#!/usr/bin/env python3
"""Extrai a memoria local do Cursor IDE para um indice SQLite FTS5 compacto.

Fontes lidas (todas somente-leitura):
  - ~/.config/Cursor/User/globalStorage/state.vscdb  (chats formato antigo: composerHeaders + cursorDiskKV)
  - ~/.cursor/chats/*/*/store.db + meta.json + prompt_history.json  (chats formato novo)
  - ~/.cursor/plans/*.md
  - ~/.cursor/rules/*.mdc

Saida: data/index.db ao lado deste script.

Uso:
  python extract.py            # incremental (pula sessoes inalteradas)
  python extract.py --full     # reindica tudo
  python extract.py --since-days 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IDX_DB = os.path.join(HERE, "data", "index.db")

CURSOR_HOME = os.path.expanduser("~/.cursor")
GLOBAL_DB = os.path.expanduser("~/.config/Cursor/User/globalStorage/state.vscdb")

ROLE_MAP = {1: "user", 2: "assistant"}
TEXT_CAP = 65535  # max chars por mensagem no indice
BATCH = 5000


def log(msg: str) -> None:
    print(f"[extract] {msg}", flush=True)


def connect_ro(path: str) -> sqlite3.Connection:
    uri = "file:{}?mode=ro".format(path.replace("?", "%3f").replace("#", "%23"))
    return sqlite3.connect(uri, uri=True)


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions(
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT,
            cwd TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            n_messages INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages(
            session_id TEXT NOT NULL,
            pos INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            PRIMARY KEY(session_id, pos)
        ) WITHOUT ROWID;
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content, session_id UNINDEXED, role UNINDEXED, tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS plans(
            name TEXT PRIMARY KEY,
            title TEXT,
            path TEXT,
            content TEXT,
            updated_at INTEGER
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS plans_fts USING fts5(
            content, name UNINDEXED, tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS rules(
            name TEXT PRIMARY KEY,
            description TEXT,
            content TEXT,
            updated_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return conn


# ---------------------------------------------------------------- old format
def extract_old_format(conn: sqlite3.Connection, since_ms: int, full: bool) -> None:
    if not os.path.exists(GLOBAL_DB):
        log("state.vscdb global nao encontrado; pulando formato antigo")
        return
    log(f"abrindo banco global (somente leitura): {GLOBAL_DB}")
    g = connect_ro(GLOBAL_DB)
    g.execute("PRAGMA cache_size=-500000")  # ~500MB de cache p/ leitura

    # 1) headers -> sessions
    rows = g.execute(
        "SELECT composerId, workspaceId, createdAt, lastUpdatedAt, value FROM composerHeaders "
        "WHERE COALESCE(isSubagent,0)=0 AND lastUpdatedAt >= ?", (since_ms,)
    ).fetchall()
    log(f"composerHeaders desde corte: {len(rows)}")

    existing = {
        r[0]: r[1]
        for r in conn.execute("SELECT id, updated_at FROM sessions WHERE source='old'")
    }
    todo: dict[str, tuple] = {}
    for cid, wsid, created, updated, val in rows:
        todo[cid] = (created or 0, updated or 0, wsid, val)

    skip = 0
    batch = []
    for cid, (created, updated, wsid, val) in todo.items():
        if not full and existing.get(cid) == updated:
            skip += 1
            continue
        cwd = ""
        try:
            j = json.loads(val) if val else {}
            ident = j.get("workspaceIdentifier") or {}
            cwd = ((ident.get("uri") or {}).get("fsPath")) or ""
        except Exception:
            pass
        batch.append((cid, "old", "", cwd, created, updated))
    conn.executemany(
        "INSERT OR REPLACE INTO sessions(id,source,title,cwd,created_at,updated_at,n_messages) VALUES(?,?,?,?,?,?,0)",
        batch,
    )
    conn.commit()
    log(f"sessoes formato antigo novas/atualizadas: {len(batch)} (puladas {skip})")
    wanted = {b[0] for b in batch}
    if not wanted:
        log("nada a atualizar no formato antigo")
        return

    # 2) composerData -> titulo + ordem dos bubbles
    bubble_map: dict[str, tuple[str, str, int]] = {}
    titles: dict[str, str] = {}
    cur = g.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key >= 'composerData:' AND key < 'composerData;'"
    )
    n_cd = 0
    for key, val in cur:
        cid = key.split(":", 1)[1]
        if cid not in wanted:
            continue
        n_cd += 1
        try:
            j = json.loads(val)
        except Exception:
            continue
        titles[cid] = (j.get("name") or "").strip()
        conv = j.get("fullConversationHeadersOnly") or []
        for pos, h in enumerate(conv):
            bid = h.get("bubbleId")
            if bid:
                bubble_map[f"{cid}:{bid}"] = (cid, ROLE_MAP.get(h.get("type"), "other"), pos)
    log(f"composerData casados: {n_cd}; bubbles esperados: {len(bubble_map)}")
    conn.executemany(
        "UPDATE sessions SET title=? WHERE id=?", (list((t, c) for c, t in titles.items()) or [])
    )
    conn.commit()
    conn.executemany("DELETE FROM messages WHERE session_id=?", [(c,) for c in titles])
    # limpa fts dessas sessoes
    conn.executemany("DELETE FROM messages_fts WHERE session_id=?", [(c,) for c in titles])
    conn.commit()

    # 3) stream dos bubbles
    log("varredura dos bubbles (pode demorar alguns minutos; banco de 46GB)...")
    cur = g.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key >= 'bubbleId:' AND key < 'bubbleId;'"
    )
    insert_batch: list[tuple] = []
    seen = hit = 0
    t0 = time.time()
    for key, val in cur:
        seen += 1
        if seen % 100000 == 0:
            log(f"  bubbles varridos: {seen} (hits {hit}) {time.time()-t0:.0f}s")
        m = bubble_map.get(key.split(":", 1)[1])
        if not m:
            continue
        cid, role, pos = m
        try:
            if isinstance(val, (bytes, memoryview)):
                continue
            j = json.loads(val)
            text = j.get("text") or ""
        except Exception:
            continue
        if not text.strip():
            continue
        hit += 1
        insert_batch.append((cid, pos, role, text[:TEXT_CAP]))
        if len(insert_batch) >= BATCH:
            _flush_messages(conn, insert_batch)
            insert_batch = []
    if insert_batch:
        _flush_messages(conn, insert_batch)
    log(f"bubbles indexados: {hit} de ~{seen} varridos")

    conn.execute(
        """UPDATE sessions SET n_messages=(SELECT COUNT(*) FROM messages WHERE session_id=sessions.id)
           WHERE source='old'"""
    )
    conn.execute(
        """UPDATE sessions SET title=(
                 SELECT substr(replace(replace(content,char(10),' '),char(13),' '),1,120)
                 FROM messages WHERE session_id=sessions.id AND role='user' ORDER BY pos LIMIT 1)
           WHERE source='old' AND (title IS NULL OR title='')"""
    )
    conn.commit()
    g.close()


def _flush_messages(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO messages(session_id,pos,role,content) VALUES(?,?,?,?)", rows
    )
    conn.executemany(
        "INSERT INTO messages_fts(content,session_id,role) VALUES(?,?,?)",
        [(r[3], r[0], r[2]) for r in rows],
    )
    conn.commit()


# ---------------------------------------------------------------- new format
_PRINTABLE = set(range(32, 127)) | {9}  # ascii + tab


def _strings_runs(data: bytes, min_len: int = 40) -> list[str]:
    out: list[str] = []
    cur = bytearray()

    def flush() -> None:
        if len(cur) >= min_len:
            try:
                out.append(cur.decode("utf-8", "replace"))
            except Exception:
                pass

    intab_ws = {10, 13}
    for b in data:
        if b in _PRINTABLE or b in intab_ws:
            cur.append(b if b not in intab_ws else 32)
        else:
            flush()
            cur = bytearray()
    flush()
    return out


def extract_new_format(conn: sqlite3.Connection, since_ms: int, full: bool) -> None:
    root = os.path.join(CURSOR_HOME, "chats")
    if not os.path.isdir(root):
        log("~/.cursor/chats nao encontrado; pulando formato novo")
        return
    store_dbs = []
    for hblob in sorted(os.listdir(root)):
        d1 = os.path.join(root, hblob)
        if not os.path.isdir(d1):
            continue
        for chat in sorted(os.listdir(d1)):
            p = os.path.join(d1, chat, "store.db")
            if os.path.exists(p):
                store_dbs.append((chat, os.path.dirname(p)))
    log(f"chats formato novo encontrados: {len(store_dbs)}")

    existing = {
        r[0]: r[1]
        for r in conn.execute("SELECT id, updated_at FROM sessions WHERE source='new'")
    }
    for chat_id, cdir in store_dbs:
        meta_file = os.path.join(cdir, "meta.json")
        title, cwd, created, updated = "", "", 0, 0
        if os.path.exists(meta_file):
            try:
                mj = json.load(open(meta_file, encoding="utf-8"))
                title = mj.get("title", "") or ""
                cwd = mj.get("cwd", "") or ""
                created = int(mj.get("createdAtMs", 0) or 0)
                updated = int(mj.get("updatedAtMs", 0) or 0)
            except Exception:
                pass
        if updated and updated < since_ms:
            continue
        if not full and existing.get(chat_id) == updated and updated:
            continue
        msgs: list[tuple[int, str, str]] = []  # (pos, role, text)
        hist_file = os.path.join(cdir, "prompt_history.json")
        prompts: list[str] = []
        if os.path.exists(hist_file):
            try:
                prompts = [str(p) for p in json.load(open(hist_file, encoding="utf-8"))]
            except Exception:
                pass
        # blobs
        try:
            sdb = connect_ro(os.path.join(cdir, "store.db"))
            for bid, data in sdb.execute("SELECT id, data FROM blobs"):
                if data is None:
                    continue
                if isinstance(data, memoryview):
                    data = bytes(data)
                try:
                    txt = data.decode("utf-8") if isinstance(data, bytes) else str(data)
                except Exception:
                    txt = ""
                if txt:
                    try:
                        o = json.loads(txt)
                        content = o.get("content")
                        role = str(o.get("role") or "other")
                        if isinstance(content, list):
                            content = "\n".join(
                                str(c.get("text", "")) if isinstance(c, dict) else str(c)
                                for c in content
                            )
                        if isinstance(content, str) and content.strip() and role in (
                            "user",
                            "assistant",
                        ):
                            msgs.append((0, role, content))
                            continue
                    except Exception:
                        pass
                if isinstance(data, bytes):
                    for run in _strings_runs(data):
                        msgs.append((0, "unknown", run))
            sdb.close()
        except Exception as e:
            log(f"  {chat_id}: falha ao ler store.db: {e}")

        # dedup por hash do conteudo
        dedup: dict[int, tuple[str, str]] = {}
        for _, role, text in msgs:
            dedup[hash((role, text))] = (role, text)
        ordered = sorted(dedup.items(), key=lambda kv: (kv[1][0] != "user", kv[0]))

        conn.execute("DELETE FROM messages WHERE session_id=?", (chat_id,))
        conn.execute("DELETE FROM messages_fts WHERE session_id=?", (chat_id,))
        rows = []
        pos = 0
        for _, (role, text) in ordered:
            rows.append((chat_id, pos, role, text[:TEXT_CAP]))
            pos += 1
        for ptxt in prompts:
            ptxt = ptxt.strip()
            if ptxt and ptxt not in {r[3] for r in rows}:
                rows.append((chat_id, pos, "user", ptxt[:TEXT_CAP]))
                pos += 1
        _flush_messages(conn, rows)
        if not title:
            title = next((r[3][:120].replace("\n", " ") for r in rows if r[2] == "user"), "")
        conn.execute(
            "INSERT OR REPLACE INTO sessions(id,source,title,cwd,created_at,updated_at,n_messages) VALUES(?,?,?,?,?,?,?)",
            (chat_id, "new", title, cwd, created, updated, len(rows)),
        )
        conn.commit()
        log(f"  {chat_id}: {len(rows)} mensagens indexadas ({title[:60]!r})")


# ---------------------------------------------------------------- plans / rules / kv
def _first_heading(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def extract_plans(conn: sqlite3.Connection) -> None:
    pdir = os.path.join(CURSOR_HOME, "plans")
    files = sorted(glob_md(pdir))
    log(f"planos encontrados: {len(files)}")
    for f in files:
        try:
            content = open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        name = os.path.basename(f)
        title = _first_heading(content) or os.path.splitext(name)[0]
        mtime = int(os.path.getmtime(f) * 1000)
        conn.execute(
            "INSERT OR REPLACE INTO plans(name,title,path,content,updated_at) VALUES(?,?,?,?,?)",
            (name, title, f, content, mtime),
        )
        conn.execute("DELETE FROM plans_fts WHERE name=?", (name,))
        conn.execute("INSERT INTO plans_fts(content,name) VALUES(?,?)", (content, name))
    conn.commit()


def extract_rules(conn: sqlite3.Connection) -> list[str]:
    rdir = os.path.join(CURSOR_HOME, "rules")
    files = glob_mdc(rdir)
    log(f"regras globais encontradas: {len(files)}")
    names = []
    for f in files:
        try:
            content = open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        desc = ""
        m = re.search(r"^description:\s*(.+)$", content, re.M)
        if m:
            desc = m.group(1).strip()
        name = os.path.basename(f)
        names.append(name)
        mtime = int(os.path.getmtime(f) * 1000)
        conn.execute(
            "INSERT OR REPLACE INTO rules(name,description,content,updated_at) VALUES(?,?,?,?)",
            (name, desc, content, mtime),
        )
    conn.commit()
    return names


def glob_md(pdir):
    import glob as g

    return g.glob(os.path.join(pdir, "*.md")) if os.path.isdir(pdir) else []


def glob_mdc(pdir):
    import glob as g

    return g.glob(os.path.join(pdir, "*.mdc")) if os.path.isdir(pdir) else []


def extract_kv(conn: sqlite3.Connection) -> None:
    if not os.path.exists(GLOBAL_DB):
        return
    g = connect_ro(GLOBAL_DB)
    for key in ("aicontext.personalContext", "cursor/memoriesEnabled", "cursorPendingMemories"):
        row = g.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
        if row:
            conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, str(row[0])))
    g.close()
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="reindexa tudo")
    ap.add_argument("--since-days", type=int, default=0, help="so sessoes atualizadas nos ultimos N dias")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(IDX_DB), exist_ok=True)
    conn = init_db(IDX_DB)

    if args.full:
        log("--full: limpando tabelas de mensagens/chat")
        conn.executescript(
            "DELETE FROM messages; DELETE FROM messages_fts; DELETE FROM sessions;"
        )
        conn.commit()

    since_ms = 0
    if args.since_days:
        since_ms = int((time.time() - args.since_days * 86400) * 1000)
        log(f"corte temporal: ultimos {args.since_days} dias")

    t0 = time.time()
    extract_old_format(conn, since_ms, args.full)
    extract_new_format(conn, since_ms, args.full)
    extract_plans(conn)
    rules_names = extract_rules(conn)
    extract_kv(conn)

    stats = {
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - t0, 1),
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "plans": conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0],
        "rules": conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0],
        "db_bytes": os.path.getsize(IDX_DB),
    }
    conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('last_build', ?)", (json.dumps(stats),)
    )
    conn.commit()
    conn.close()
    log("PRONTO: " + json.dumps(stats, indent=2))
    log(f"regras globais: {', '.join(rules_names)}")


if __name__ == "__main__":
    sys.exit(main())
