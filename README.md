# cursor-memory-mcp

MCP server e UI local que expõem a **memória do Cursor IDE** deste computador
(chats, planos e regras globais) para qualquer cliente MCP (OpenCode, Cursor,
Claude Code, etc.).

## O que é indexado

| Fonte | Caminho lido (somente leitura) | Destino |
|---|---|---|
| Chats formato antigo | `~/.config/Cursor/User/globalStorage/state.vscdb` (`composerHeaders` + `cursorDiskKV`) | `sessions` / `messages` |
| Chats formato novo | `~/.cursor/chats/*/*/store.db` + `meta.json` + `prompt_history.json` | `sessions` / `messages` |
| Planos | `~/.cursor/plans/*.md` | `plans` |
| Regras globais | `~/.cursor/rules/*.mdc` | `rules` |
| Prefs do Cursor | chaves `aicontext.*` / `cursor*` do banco global | `kv` |

Nada é alterado no Cursor: as leituras usam SQLite `mode=ro`. O índice local
(`data/index.db`) **não** vai para o Git — chats são pessoais.

## Instalação rápida

```bash
git clone https://github.com/rodriguesfas/cursor-memory-mcp.git
cd cursor-memory-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python extract.py          # primeira indexação (pode demorar alguns minutos)
./install-systemd.sh                 # UI + reindex automático a cada 30 min
```

UI: [http://127.0.0.1:8777/](http://127.0.0.1:8777/)

## Uso manual (sem systemd)

```bash
.venv/bin/python extract.py                 # incremental
.venv/bin/python extract.py --since-days 7
.venv/bin/python extract.py --full          # reindex completo
.venv/bin/python server.py                  # MCP (stdio)
.venv/bin/python web.py --port 8777         # UI
```

## Automático (systemd --user)

`./install-systemd.sh` instala e activa:

| Unidade | Função |
|---|---|
| `cursor-memory-extract.timer` | a cada **30 min** (+ 3 min após boot) corre `extract.py` incremental |
| `cursor-memory-web.service` | mantém a UI em `http://127.0.0.1:8777/` |

```bash
systemctl --user status cursor-memory-extract.timer cursor-memory-web.service
journalctl --user -u cursor-memory-extract.service -n 50
```

Templates em `systemd/*.in`; o instalador substitui o caminho do clone.

## Ferramentas MCP

- `cursor_memory_stats()` — contagens e data da última indexação
- `cursor_memory_search(query, limit, role)` — FTS5 nas mensagens
- `cursor_memory_list_sessions(limit, offset, project, since_days)`
- `cursor_memory_read_session(session_id, start, limit)`
- `cursor_memory_search_plans(query)` / `cursor_memory_list_plans()` / `cursor_memory_read_plan(name)`
- `cursor_memory_list_rules()` / `cursor_memory_read_rule(name)`
- `cursor_memory_preferences()`
- `cursor_memory_refresh(since_days)` — re-roda o extrator incremental

## Registro em clientes MCP

OpenCode (`~/.config/opencode/opencode.json`):

```json
"mcp": {
  "cursor-memory": {
    "type": "local",
    "command": [
      "/caminho/para/cursor-memory-mcp/.venv/bin/python",
      "/caminho/para/cursor-memory-mcp/server.py"
    ],
    "enabled": true
  }
}
```

Claude Desktop / Cursor: o mesmo comando com transport `stdio`.

## Limitações conhecidas

- A feature nativa “Memories” do Cursor guarda dados **no servidor**; localmente
  só existem pendentes (quando vazias, não há nada a extrair).
- Chats do formato novo podem ter blobs criptografados; o extrator captura o que
  é legível (títulos, `prompt_history.json`, JSON e texto puro).
- Subagentes do Composer e bolhas só com tool-calls (sem texto) são ignorados.
