#!/usr/bin/env python3
"""MCP server que expoe a memoria local do Cursor IDE (chats, planos, regras).

Le data/index.db (gerado por extract.py) em modo somente-leitura e expoe
tools via MCP stdio: busca full-text, listagem/leitura de sessoes, planos,
regras globais e refresh do indice.

Rode com: python server.py
Registre no opencode/opencode.json:
  "mcp": {
    "cursor-memory": {
      "type": "local",
      "command": ["<venv>/bin/python", "<projeto>/server.py"],
      "enabled": true
    }
  }
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys

from mcp.server.fastmcp import FastMCP

HERE = os.path.dirname(os.path.abspath(__file__))
IDX_DB = os.path.join(HERE, "data", "index.db")
EXTRACT = os.path.join(HERE, "extract.py")

mcp = FastMCP(
    "cursor-memory",
    instructions=(
        "Memoria historica local do Cursor IDE deste computador: ~2.6k sessoes de chat "
        "(formato antigo + novo), planos (*.md) e regras globais (*.mdc) extraidos com "
        "extract.py. Use para responder 'o que ja discutimos/fizemos sobre X no Cursor', "
        "recuperar decisoes passadas e ler planos/regras antigas. "
        "Sempre comece com cursor_memory_search ou cursor_memory_list_sessions."
    ),
)


def db() -> sqlite3.Connection:
    uri = "file:{}?mode=ro".format(IDX_DB)
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_ms(ms: int | None) -> str:
    if not ms:
        return "?"
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


_FTS_OPS = {"AND", "OR", "NOT", "NEAR"}


def _fts_safe(q: str) -> str:
    tokens = re.findall(r'"[^"]*"|[\wÀ-ӿ]+', q, flags=re.UNICODE)
    out = []
    for t in tokens:
        if t.upper() in _FTS_OPS or t.startswith('"'):
            out.append(t)
        else:
            out.append('"' + t + '"')
    return " ".join(out)


@mcp.tool()
def cursor_memory_stats() -> str:
    """Estatisticas do indice local da memoria do Cursor (contagens e ultima indexacao)."""
    c = db()
    meta = c.execute("SELECT value FROM meta WHERE key='last_build'").fetchone()
    out = {
        "db": IDX_DB,
        "db_size_mb": round(os.path.getsize(IDX_DB) / 1e6, 1),
        "sessions_total": c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "sessions_old_format": c.execute(
            "SELECT COUNT(*) FROM sessions WHERE source='old'"
        ).fetchone()[0],
        "sessions_new_format": c.execute(
            "SELECT COUNT(*) FROM sessions WHERE source='new'"
        ).fetchone()[0],
        "messages": c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "plans": c.execute("SELECT COUNT(*) FROM plans").fetchone()[0],
        "rules": c.execute("SELECT COUNT(*) FROM rules").fetchone()[0],
        "last_build": json.loads(meta[0]) if meta else None,
    }
    c.close()
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool()
def cursor_memory_search(query: str, limit: int = 10, role: str = "") -> str:
    """Busca full-text nas mensagens dos chats do Cursor.

    query: texto livre (termos sao ANDados por padrao; suporta OR/NOT/NEAR e "frases exatas").
    limit: max de resultados (default 10, max 50).
    role: '' (ambos), 'user' (so o que voce perguntou) ou 'assistant' (so respostas).
    """
    limit = max(1, min(int(limit), 50))
    q = _fts_safe(query)
    if not q:
        return "query vazia"
    c = db()
    sql = (
        "SELECT f.session_id AS sid, f.role AS role, rank, "
        "       snippet(messages_fts, 0, '>>>', '<<<', ' … ', 24) AS snip "
        "FROM messages_fts f WHERE messages_fts MATCH ?"
    )
    params: list = [q]
    if role in ("user", "assistant"):
        sql += " AND f.role = ?"
        params.append(role)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit * 3)  # extra p/ dedup por sessao
    try:
        rows = c.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        return f"query FTS invalida: {e}"
    if not rows:
        return "sem resultados"
    info = {
        r[0]: r
        for r in c.execute(
            "SELECT id, title, cwd, updated_at FROM sessions WHERE id IN "
            "(SELECT DISTINCT session_id FROM messages_fts WHERE messages_fts MATCH ? LIMIT 200)",
            (q,),
        ).fetchall()
    }
    c.close()
    lines = []
    seen_sessions = set()
    for r in rows:
        if r["sid"] in seen_sessions:
            continue
        seen_sessions.add(r["sid"])
        s = info.get(r["sid"])
        title = (s[1] if s else "") or "(sem titulo)"
        when = _fmt_ms(s[3] if s else 0)
        proj = os.path.basename(s[2].rstrip("/")) if s and s[2] else "?"
        lines.append(
            f"[{len(lines)+1}] sessao {r['sid'][:8]}… \"{title[:70]}\" ({proj}, {when}, {r['role']})\n"
            f"    {r['snip']}"
        )
        if len(lines) >= limit:
            break
    return "\n\n".join(lines) + (
        "\n\n→ use cursor_memory_read_session(session_id=...) para ler a sessao inteira"
    )


@mcp.tool()
def cursor_memory_search_plans(query: str, limit: int = 10) -> str:
    """Busca full-text nos planos (*.md) gerados historicamente pelo Cursor."""
    limit = max(1, min(int(limit), 50))
    q = _fts_safe(query)
    if not q:
        return "query vazia"
    c = db()
    try:
        rows = c.execute(
            "SELECT name, snippet(plans_fts, 0, '>>>', '<<<', ' … ', 24) AS snip "
            "FROM plans_fts WHERE plans_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, limit),
        ).fetchall()
    except sqlite3.OperationalError as e:
        return f"query FTS invalida: {e}"
    c.close()
    if not rows:
        return "sem resultados"
    return "\n\n".join(f"[{i+1}] {r['name']}\n    {r['snip']}" for i, r in enumerate(rows)) + (
        "\n\n→ use cursor_memory_read_plan(name=...) para ler o plano inteiro"
    )


@mcp.tool()
def cursor_memory_list_sessions(
    limit: int = 25, offset: int = 0, project: str = "", since_days: int = 0
) -> str:
    """Lista sessoes de chat do Cursor, das mais recentes para as mais antigas.

    project: filtra por trecho do caminho do projeto (ex.: 'web-crm', 'platform').
    since_days: so sessoes atualizadas nos ultimos N dias (0 = todas).
    """
    limit = max(1, min(int(limit), 100))
    c = db()
    sql = "SELECT id, title, cwd, created_at, updated_at, n_messages, source FROM sessions WHERE 1=1"
    params: list = []
    if project:
        sql += " AND cwd LIKE ?"
        params.append(f"%{project}%")
    if since_days:
        cutoff = int((dt.datetime.now() - dt.timedelta(days=since_days)).timestamp() * 1000)
        sql += " AND updated_at >= ?"
        params.append(cutoff)
    sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = c.execute(sql, params).fetchall()
    total = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    c.close()
    if not rows:
        return f"nenhuma sessao (total no indice: {total})"
    lines = []
    for i, r in enumerate(rows):
        proj = os.path.basename((r["cwd"] or "").rstrip("/")) or "?"
        lines.append(
            f"{offset+i+1:>4}. {r['id'][:8]}… [{r['source']}] {_fmt_ms(r['updated_at'])} "
            f"({r['n_messages']} msg) {proj} — {(r['title'] or '(sem titulo)')[:70]}"
        )
    return f"total no indice: {total}\n" + "\n".join(lines)


@mcp.tool()
def cursor_memory_read_session(session_id: str, start: int = 0, limit: int = 40) -> str:
    """Le as mensagens de uma sessao (paginado). session_id aceita id completo ou prefixo.

    start: posicao da 1a mensagem (0 = comeco). limit: quantas mensagens (max 100).
    """
    limit = max(1, min(int(limit), 100))
    c = db()
    sess = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        sess = c.execute(
            "SELECT * FROM sessions WHERE id LIKE ?", (session_id + "%",)
        ).fetchone()
    if not sess:
        c.close()
        return f"sessao nao encontrada: {session_id}"
    sid = sess["id"]
    rows = c.execute(
        "SELECT pos, role, content FROM messages WHERE session_id=? AND pos>=? ORDER BY pos LIMIT ?",
        (sid, max(0, int(start)), limit),
    ).fetchall()
    n = sess["n_messages"]
    c.close()
    header = (
        f"# sessao {sid}\n"
        f"titulo: {sess['title'] or '(sem titulo)'}\n"
        f"projeto: {sess['cwd'] or '?'}  |  criada: {_fmt_ms(sess['created_at'])}  |  "
        f"atualizada: {_fmt_ms(sess['updated_at'])}  |  {n} mensagens\n"
        f"mensagens {start}..{start+len(rows)-1}\n"
    )
    body = []
    for r in rows:
        txt = r["content"]
        if len(txt) > 2000:
            txt = txt[:2000] + "\n…[truncado]"
        body.append(f"## [{r['pos']}] {r['role']}\n{txt}")
    return header + "\n" + "\n\n".join(body)


@mcp.tool()
def cursor_memory_list_plans() -> str:
    """Lista todos os planos (*.md) indexados do Cursor."""
    c = db()
    rows = c.execute(
        "SELECT name, title, updated_at, length(content) AS size FROM plans ORDER BY updated_at DESC"
    ).fetchall()
    c.close()
    if not rows:
        return "nenhum plano indexado"
    return "\n".join(
        f"- {r['name']} — {(r['title'] or '')[:70]} ({r['size']}B, {_fmt_ms(r['updated_at'])})"
        for r in rows
    )


@mcp.tool()
def cursor_memory_read_plan(name: str) -> str:
    """Le um plano inteiro pelo nome exato do arquivo (ex.: 'crm_mvp_portal_c18edd38.plan.md')."""
    c = db()
    row = c.execute("SELECT * FROM plans WHERE name=?", (name,)).fetchone()
    if not row:
        row = c.execute(
            "SELECT * FROM plans WHERE name LIKE ?", ("%" + name + "%",)
        ).fetchone()
    c.close()
    if not row:
        return f"plano nao encontrado: {name}"
    return f"# {row['title'] or row['name']}\n(caminho original: {row['path']})\n\n{row['content']}"


@mcp.tool()
def cursor_memory_list_rules() -> str:
    """Lista as regras globais do Cursor (~/.cursor/rules/*.mdc)."""
    c = db()
    rows = c.execute("SELECT name, description, length(content) AS size FROM rules").fetchall()
    c.close()
    if not rows:
        return "nenhuma regra indexada"
    return "\n".join(
        f"- {r['name']} — {(r['description'] or '')[:80]} ({r['size']}B)" for r in rows
    )


@mcp.tool()
def cursor_memory_read_rule(name: str) -> str:
    """Le uma regra global inteira pelo nome do arquivo (ex.: 'alfred-notebook.mdc')."""
    c = db()
    row = c.execute("SELECT * FROM rules WHERE name=?", (name,)).fetchone()
    if not row:
        row = c.execute(
            "SELECT * FROM rules WHERE name LIKE ?", ("%" + name + "%",)
        ).fetchone()
    c.close()
    if not row:
        return f"regra nao encontrada: {name}"
    return f"# {row['name']}\n{row['content']}"


@mcp.tool()
def cursor_memory_preferences() -> str:
    """Mostra o contexto pessoal salvo no Cursor (ex.: idioma preferido)."""
    c = db()
    rows = c.execute("SELECT key, value FROM kv").fetchall()
    c.close()
    return "\n".join(f"{r['key']} = {r['value']}" for r in rows) or "vazio"


@mcp.tool()
def cursor_memory_refresh(since_days: int = 30) -> str:
    """Re-roda a extracao incremental (chats atualizados nos ultimos N dias; padrao 30)."""
    try:
        proc = subprocess.run(
            [sys.executable, EXTRACT, "--since-days", str(since_days)],
            capture_output=True,
            text=True,
            timeout=1800,
            cwd=HERE,
        )
    except subprocess.TimeoutExpired:
        return "extracao excedeu 30min (provavelmente ainda rodando em segundo plano do subprocesso)"
    out = (proc.stdout or "")[-3000:]
    err = (proc.stderr or "")[-1500:]
    return f"exit={proc.returncode}\n{out}\n{err}"


if __name__ == "__main__":
    if not os.path.exists(IDX_DB):
        print("indice nao encontrado — rode: python extract.py", file=sys.stderr)
        sys.exit(1)
    mcp.run()
