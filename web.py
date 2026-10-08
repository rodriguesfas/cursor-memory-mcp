#!/usr/bin/env python3
"""UI web local para o índice de memória do Cursor (somente leitura).

Serve em http://127.0.0.1:8777/ uma página de busca + navegação
(sessões, planos, regras) sobre data/index.db gerado por extract.py.

Uso:
  .venv/bin/python web.py            # porta 8777
  .venv/bin/python web.py --port 9000
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
IDX_DB = os.path.join(HERE, "data", "index.db")

FTS_OPS = {"AND", "OR", "NOT", "NEAR"}


def fts_safe(q: str) -> str:
    tokens = re.findall(r'"[^"]*"|[\wÀ-ӿ]+', q, flags=re.UNICODE)
    out = []
    for t in tokens:
        if t.upper() in FTS_OPS or t.startswith('"'):
            out.append(t)
        else:
            out.append('"' + t + '"')
    return " ".join(out)


def db():
    conn = sqlite3.connect("file:{}?mode=ro".format(IDX_DB), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_ms(ms):
    if not ms:
        return "?"
    import datetime as dt

    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cursor Memory</title>
<style>
:root{--bg:#111318;--fg:#e6e6e6;--dim:#9aa0a6;--acc:#7aa2f7;--card:#1a1d24;--line:#2a2e37}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 18px;z-index:5}
h1{font-size:16px;margin:0 0 8px;display:inline}
#stats{color:var(--dim);font-size:12px;margin-left:12px}
.row{display:flex;gap:8px;flex-wrap:wrap}
input,select,button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit}
input{flex:1;min-width:200px}
button{cursor:pointer;border-color:var(--acc);color:var(--acc)}
.tabs{display:flex;gap:6px;margin:14px 18px 0}
.tab{padding:6px 14px;border:1px solid var(--line);border-radius:8px 8px 0 0;cursor:pointer;color:var(--dim)}
.tab.on{background:var(--card);color:var(--fg)}
main{padding:14px 18px;max-width:1100px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px;cursor:pointer}
.card:hover{border-color:var(--acc)}
.card .t{font-weight:600}.card .m{color:var(--dim);font-size:12px;margin-top:2px}
.card .s{color:var(--dim);margin-top:6px;white-space:pre-wrap;word-break:break-word}
mark{background:#3b2f10;color:#ffd479;border-radius:3px;padding:0 2px}
.msg{border-left:3px solid var(--line);padding:8px 12px;margin:8px 0;background:var(--card);border-radius:0 8px 8px 0;white-space:pre-wrap;word-break:break-word}
.msg .r{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.msg.user{border-left-color:#7aa2f7}.msg.assistant{border-left-color:#9ece6a}
.pager{display:flex;gap:8px;margin:10px 0;color:var(--dim)}
.pager button{color:var(--dim)}
.empty{color:var(--dim);padding:30px;text-align:center}
a{color:var(--acc);text-decoration:none}
</style></head>
<body>
<header>
  <h1>🧠 Cursor Memory</h1><span id="stats">carregando…</span>
  <div class="row" style="margin-top:10px">
    <input id="q" placeholder="Buscar nas conversas… (AND por padrão; use OR, NOT, &quot;frase exata&quot;)" autofocus>
    <select id="role"><option value="">todos</option><option value="user">só suas perguntas</option><option value="assistant">só respostas</option></select>
    <button onclick="go()">Buscar</button>
  </div>
</header>
<div class="tabs">
  <div class="tab on" data-tab="search" onclick="tab('search',this)">Busca</div>
  <div class="tab" data-tab="sessions" onclick="tab('sessions',this)">Sessões</div>
  <div class="tab" data-tab="plans" onclick="tab('plans',this)">Planos</div>
  <div class="tab" data-tab="rules" onclick="tab('rules',this)">Regras</div>
</div>
<main id="main"></main>
<script>
const main=document.getElementById('main');
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const hl=s=>esc(s).replace(/&gt;&gt;&gt;(.+?)&lt;&lt;&lt;/g,'<mark>$1</mark>').replace(/ … /g,' … ');
function tab(name,el){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));el.classList.add('on');
 if(name==='sessions')listSessions(); if(name==='plans')listPlans(); if(name==='rules')listRules();}
function go(){const q=document.getElementById('q').value.trim();if(!q)return;search(q);}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
async function api(u){const r=await fetch(u);return r.json();}
function snipRow(r){const d=new Date(r.updated_at);return `<div class="card" onclick="openSession('${r.id}')">
 <div class="t">${esc(r.title||'(sem título)')}</div>
 <div class="m">${esc(r.project||'?')} · ${esc(r.when)} · ${r.n_messages} msgs · ${r.source}</div>
 ${r.snip?`<div class="s">${hl(r.snip)}</div>`:''}</div>`;}
async function search(q){const role=document.getElementById('role').value;
 const r=await api('/api/search?q='+encodeURIComponent(q)+'&role='+role+'&limit=30');
 if(!r.results||!r.results.length){main.innerHTML='<div class="empty">sem resultados</div>';return;}
 const info={};r.info.forEach(s=>info[s.id]=s);
 main.innerHTML=r.results.map(x=>{const s=info[x.sid]||{};
  return snipRow({id:s.id||x.sid,title:s.title,project:s.project,when:s.when,n_messages:s.n_messages,source:s.source,snip:x.snip});}).join('');}
async function listSessions(off=0){const r=await api('/api/sessions?limit=40&offset='+off);
 let h=`<div class="pager"><button onclick="listSessions(${Math.max(0,off-40)})">←</button><span>total ${r.total} · exibindo ${off+1}–${off+r.rows.length}</span><button onclick="listSessions(${off+40})">→</button></div>`;
 h+=r.rows.map(s=>snipRow({id:s.id,title:s.title,project:s.project,when:s.when,n_messages:s.n_messages,source:s.source})).join('');
 main.innerHTML=h||'<div class="empty">vazio</div>';}
async function openSession(id,start=0){const r=await api('/api/session/'+encodeURIComponent(id)+'?start='+start);
 let h=`<div class="pager"><button onclick="location.hash='s'">← voltar</button>
 <span>${esc(r.title||'(sem título)')} · ${esc(r.cwd||'')} · ${r.n_messages} msgs · ${r.source}</span></div>`;
 h+=r.messages.map(m=>`<div class="msg ${m.role}"><div class="r">${esc(m.role)} · #${m.pos}</div>${esc(m.content)}</div>`).join('');
 if(start+r.messages.length<r.n_messages)h+=`<div class="pager"><button onclick="openSession('${id}',${start+r.messages.length})">próximas ${Math.min(40,r.n_messages-start-r.messages.length)} →</button></div>`;
 main.innerHTML=h;}
async function listPlans(){const r=await api('/api/plans');
 main.innerHTML=r.rows?r.rows.map(p=>`<div class="card" onclick="openPlan('${encodeURIComponent(p.name)}')"><div class="t">${esc(p.title||p.name)}</div><div class="m">${esc(p.name)} · ${p.size}B · ${esc(p.when)}</div></div>`).join(''):'<div class="empty">nenhum plano</div>';}
async function openPlan(name){const r=await api('/api/plan/'+encodeURIComponent(name));
 main.innerHTML=`<div class="pager"><button onclick="listPlans()">← planos</button><span>${esc(r.title||r.name)}</span></div><pre style="white-space:pre-wrap;word-break:break-word">${esc(r.content)}</pre>`;}
async function listRules(){const r=await api('/api/rules');
 main.innerHTML=r.rows.map(p=>`<div class="card" onclick="openRule('${encodeURIComponent(p.name)}')"><div class="t">${esc(p.name)}</div><div class="m">${esc(p.description||'')}</div></div>`).join('');}
async function openRule(name){const r=await api('/api/rule/'+encodeURIComponent(name));
 main.innerHTML=`<div class="pager"><button onclick="listRules()">← regras</button><span>${esc(r.name)}</span></div><pre style="white-space:pre-wrap;word-break:break-word">${esc(r.content)}</pre>`;}
(async()=>{const s=await api('/api/stats');document.getElementById('stats').textContent=
 `${s.sessions_total} sessões · ${s.messages} mensagens · ${s.plans} planos · ${s.rules} regras · ${s.db_size_mb} MB`})();
</script></body></html>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype="application/json; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status=status)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        qs = urllib.parse.parse_qs(u.query)
        try:
            if p == "/":
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif p == "/api/stats":
                c = db()
                self._json(
                    {
                        "sessions_total": c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                        "messages": c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                        "plans": c.execute("SELECT COUNT(*) FROM plans").fetchone()[0],
                        "rules": c.execute("SELECT COUNT(*) FROM rules").fetchone()[0],
                        "db_size_mb": round(os.path.getsize(IDX_DB) / 1e6, 1),
                    }
                )
                c.close()
            elif p == "/api/search":
                q = fts_safe(qs.get("q", [""])[0])
                role = qs.get("role", [""])[0]
                limit = min(int(qs.get("limit", ["10"])[0] or 10), 50)
                if not q:
                    return self._json({"results": [], "info": []})
                c = db()
                sql = (
                    "SELECT f.session_id AS sid, f.role AS role, "
                    "snippet(messages_fts, 0, '>>>', '<<<', ' … ', 24) AS snip "
                    "FROM messages_fts f WHERE messages_fts MATCH ?"
                )
                params = [q]
                if role in ("user", "assistant"):
                    sql += " AND f.role = ?"
                    params.append(role)
                sql += " ORDER BY rank LIMIT ?"
                params.append(limit * 3)
                try:
                    rows = c.execute(sql, params).fetchall()
                except sqlite3.OperationalError as e:
                    return self._json({"error": str(e)}, 400)
                seen, results = set(), []
                for r in rows:
                    if r["sid"] in seen:
                        continue
                    seen.add(r["sid"])
                    results.append({"sid": r["sid"], "role": r["role"], "snip": r["snip"]})
                    if len(results) >= limit:
                        break
                info = [
                    dict(
                        zip(
                            ("id", "title", "project", "when", "n_messages", "source"),
                            row,
                        )
                    )
                    for row in c.execute(
                        "SELECT id, title, "
                        "CASE WHEN cwd IS NULL OR cwd='' THEN '?' ELSE replace(cwd,'/',char(47)) END, "
                        "updated_at, n_messages, source FROM sessions WHERE id IN "
                        "(SELECT DISTINCT session_id FROM messages_fts WHERE messages_fts MATCH ? LIMIT 200)",
                        (q,),
                    ).fetchall()
                ]
                for s in info:
                    s["project"] = os.path.basename((s["project"] or "").rstrip("/")) or "?"
                    s["when"] = fmt_ms(s["when"])
                c.close()
                self._json({"results": results, "info": info})
            elif p == "/api/sessions":
                limit = min(int(qs.get("limit", ["40"])[0] or 40), 100)
                off = max(int(qs.get("offset", ["0"])[0] or 0), 0)
                c = db()
                rows = c.execute(
                    "SELECT id,title,cwd,updated_at,n_messages,source FROM sessions "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, off),
                ).fetchall()
                total = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                c.close()
                self._json(
                    {
                        "total": total,
                        "rows": [
                            {
                                "id": r["id"],
                                "title": r["title"],
                                "project": os.path.basename((r["cwd"] or "").rstrip("/")) or "?",
                                "when": fmt_ms(r["updated_at"]),
                                "n_messages": r["n_messages"],
                                "source": r["source"],
                            }
                            for r in rows
                        ],
                    }
                )
            elif p.startswith("/api/session/"):
                sid = urllib.parse.unquote(p[len("/api/session/"):])
                start = max(int(qs.get("start", ["0"])[0] or 0), 0)
                c = db()
                sess = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
                if not sess:
                    sess = c.execute("SELECT * FROM sessions WHERE id LIKE ?", (sid + "%",)).fetchone()
                if not sess:
                    c.close()
                    return self._json({"error": "sessão não encontrada"}, 404)
                msgs = [
                    dict(zip(("pos", "role", "content"), r))
                    for r in c.execute(
                        "SELECT pos,role,content FROM messages WHERE session_id=? AND pos>=? ORDER BY pos LIMIT 40",
                        (sess["id"], start),
                    ).fetchall()
                ]
                n = sess["n_messages"]
                c.close()
                self._json(
                    {
                        "id": sess["id"],
                        "title": sess["title"],
                        "cwd": sess["cwd"],
                        "source": sess["source"],
                        "n_messages": n,
                        "messages": [
                            {
                                "pos": m["pos"],
                                "role": m["role"],
                                "content": m["content"][:20000],
                            }
                            for m in msgs
                        ],
                    }
                )
            elif p == "/api/plans":
                c = db()
                rows = c.execute(
                    "SELECT name,title,updated_at,length(content) AS size FROM plans ORDER BY updated_at DESC"
                ).fetchall()
                c.close()
                self._json(
                    {
                        "rows": [
                            {
                                "name": r["name"],
                                "title": r["title"],
                                "size": r["size"],
                                "when": fmt_ms(r["updated_at"]),
                            }
                            for r in rows
                        ]
                    }
                )
            elif p.startswith("/api/plan/"):
                name = urllib.parse.unquote(p[len("/api/plan/"):])
                c = db()
                row = c.execute("SELECT * FROM plans WHERE name=?", (name,)).fetchone()
                if not row:
                    row = c.execute("SELECT * FROM plans WHERE name LIKE ?", ("%" + name + "%",)).fetchone()
                c.close()
                if not row:
                    return self._json({"error": "plano não encontrado"}, 404)
                self._json({"name": row["name"], "title": row["title"], "content": row["content"]})
            elif p == "/api/rules":
                c = db()
                rows = c.execute("SELECT name,description FROM rules").fetchall()
                c.close()
                self._json({"rows": [dict(r) for r in rows]})
            elif p.startswith("/api/rule/"):
                name = urllib.parse.unquote(p[len("/api/rule/"):])
                c = db()
                row = c.execute("SELECT * FROM rules WHERE name=?", (name,)).fetchone()
                if not row:
                    row = c.execute("SELECT * FROM rules WHERE name LIKE ?", ("%" + name + "%",)).fetchone()
                c.close()
                if not row:
                    return self._json({"error": "regra não encontrada"}, 404)
                self._json({"name": row["name"], "content": row["content"]})
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._json({"error": repr(e)}, 500)
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    global IDX_DB
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if not os.path.exists(IDX_DB):
        print("índice não encontrado — rode: python extract.py", file=sys.stderr)
        return 1
    srv = ThreadingHTTPServer((args.host, args.port), H)
    print(f"cursor-memory web UI → http://{args.host}:{args.port}/", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
