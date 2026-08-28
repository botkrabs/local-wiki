# local_wiki
This is a side project while testing the local model Qwen3.8 27B.
Early comments from redditors claimed that its world knowledge was sacrificed to
reinforce coding and tool-call abilities.

Therefore I added an offline Wikipedia database to compensate for that gap.
Since Wikipedia's structure is already a retrieval index, RAG is avoided —
no vector index to build or maintain, and no embedding-similarity risk of
burying the right article.

This project keeps the full English Wikipedia (52 GB, no images) on local disk
as a ZIM file and exposes it through two MCP tools
— `get` and `search` — so an AI coding agent can read articles without any
network round-trip. The snapshot is from June 2026; it only gets newer when
I run the refresh script by hand.

Since 0.1.0 (2026-08-28) it also serves the **full Chinese Wikipedia**
(13.7 GB, `wikipedia_zh_all_nopic_2026-07.zim`) behind the same two tools via
`lang="zh"` — auto-detected from `/mnt/shared/wiki_zim/`, no config needed.

Initial word search concept was inspired by [gbkorr/ratsearch](https://github.com/gbkorr/ratsearch).

### A word on size

The full no-images build is the big one — ~49 GB to download, ~52 GB on
disk — and that's what `refresh_zim.py` installs by default. If that's too
much for your machine, kiwix hosts smaller English ZIMs that this server
takes just as well: download any of them and point `LOCAL_WIKI_ZIM` at it
(or feed the URL to `refresh_zim.py --url`). Options as of the August 2026
listing (check <https://download.kiwix.org/zim/wikipedia/> for current):

| ZIM | Size | What it is |
|---|---|---|
| `wikipedia_en_100` | ~318 MB | tiny test subset — what I used to wire this up |
| `wikipedia_en_top_nopic` | ~2.1 GB | the "top" subset, no images |
| `wikipedia_en_all_mini` | ~12 GB | every article, minimal content |
| `wikipedia_en_top1m_nopic` | ~16 GB | the "top 1M" subset, no images |
| topic builds (astronomy, computer, history, medicine, movies, physics, …) | ~0.1–2.4 GB | one field of Wikipedia |

Trade-off: a smaller ZIM simply has fewer articles — `get` on a title that
isn't in it comes back as "not found" (with related titles), and `search`
can only rank what's inside. The trade this project makes is: pay the 52 GB
once, and every article exists.

## Tools

- `get(title, section="", full=False, lang="en")` — exact title → article text. Long
  articles (text > `LOCAL_WIKI_LEAD_MAX`, default 8000) return **lead mode** by
  default: the intro up to the first h2 + a section list + a pointer (~1.5 KB
  vs ~9–25 KB). `section="Reception"` fetches one h2/h3 section
  (case/space-insensitive); `full=True` returns the whole article (oversized
  ones get a `[truncated: …]` marker); unknown title → related titles, unknown
  section → section list. Short articles (< 8 KB) always come back whole.
- `search(query, limit=8, lang="en")` — phrase → ranked titles + ~220-char snippets
  (libzim fulltext, FTS-first with suggestion fallback). Titles containing the whole
  query are hoisted to the top. Empty result → "No articles matched …".
- `lang="zh"` (either tool) — the Chinese Wikipedia archive. Separate index,
  no cross-lingual: Chinese queries work best; English queries hit only when a
  zh article contains that English string (original titles/aliases). On a zh
  title miss, related candidates come from a per-gram merge (libzim 9.x cannot
  express OR — see `server.py:_cjk_loose`); the list can be noisy, pick the
  semantically right one.

## Clients & configs

Any MCP client works. Two transports, both standard:

- **stdio** — the client spawns the process; no port involved:
  `python3 server.py` (with `LOCAL_WIKI_ZIM` set), e.g. Claude Code:
  `claude mcp add local-wiki -- /usr/bin/python3 /path/to/server.py`
- **stateless streamable HTTP** — one long-running server, any client can
  connect: start it with `LOCAL_WIKI_HTTP_PORT=3211`, then point the client
  at `http://127.0.0.1:3211/mcp` (or the machine's address, for other hosts).
  The server sends CORS headers (0.1.1), so **browser-based MCP clients work
  too** — e.g. the llama.cpp web UI's MCP server config (`http://127.0.0.1:3211/mcp`,
  transport `streamable_http`).

`LOCAL_WIKI_HTTP_PORT` set = HTTP mode (listens on the port); unset = stdio.

This deployment uses: **OpenClaw** via stdio (`~/.openclaw/openclaw.json`) and
**pi** via HTTP (`~/.config/mcp/mcp.json`).

For pi users, the companion package
[pi-local-wiki](https://github.com/botkrabs/pi-local-wiki)
(`pi install git:github.com/botkrabs/pi-local-wiki`) bundles the pi-side
pieces — footer extensions + the `local-search` routing skill — that pair
with this server.

## Protocol (wire format)

MCP spec **2025-03-26** (what the installed `mcp` Python SDK speaks), on the
**stateless streamable-HTTP** transport, JSON-RPC 2.0 over `POST /mcp`:

- **No session required.** `stateless_http=True` — any request can arrive on
  its own: `tools/call` works with no prior `initialize` (verified over the
  wire); `Mcp-Session-Id` is never minted.
- **Request headers**: `Content-Type: application/json` plus
  `Accept: application/json, text/event-stream` — both accept-types are
  mandatory, a JSON-only Accept gets a `-32600 Not Acceptable` error.
- **Responses**: `text/event-stream` (SSE frames: `event: message`,
  `data: {jsonrpc ...}`). `GET /mcp` opens an SSE channel (200).
- **CORS**: `Access-Control-Allow-Origin: *` since 0.1.1, so browser-based MCP
  clients can connect cross-origin.
- **Spec note**: MCP **2026-07-28** (released 2026-07-28) made stateless the
  core of the protocol — `initialize`/`Mcp-Session-Id` retired, requests
  self-describing via `_meta`, `Mcp-Method`/`Mcp-Name` routing headers. This
  server already behaves stateless, so upgrading is a `mcp` SDK bump, not an
  architectural change.

Minimal client call (curl, no handshake):
```bash
curl -s -X POST http://127.0.0.1:3211/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"search","arguments":{"query":"mangonel"}}}'
```

Typical client config shape (client-specific, URL + streamable-HTTP transport):
```json
{ "url": "http://127.0.0.1:3211/mcp", "transport": "streamableHttp" }
```

## Files

| Path | Purpose |
|---|---|
| `server.py` | the MCP server (needs system python 3.12: libzim, mcp, html2text, uvicorn) |
| `wikipedia.zim` | symlink → `/mnt/shared/wiki_zim/wikipedia_nopic.zim` (52.7 GB) |
| `refresh_zim.py` | manual ZIM refresh (see Operations) |
| `run_eval.py` + `eval_set.jsonl` | 31-case regression suite (~5 s) |
| `bench_http.py` | e2e latency check over the live HTTP endpoint |
| `run.pid` | pid of the HTTP server (see gotcha #2) |
| `~/.bashrc` (bottom block) | autostart: if `:3211` not listening, start the server |
| `/tmp/local-wiki-mcp.log` | server log (HTTP instance) |
| `~/.pi/agent/AGENTS.md` + `~/.pi/agent/skills/local-search/SKILL.md` | how the pi agent routes queries here |

## Env vars (server.py)

`LOCAL_WIKI_ZIM` (path; default `./wikipedia.zim`) · `LOCAL_WIKI_ZIM_ZH`
(optional; default = auto-detect newest `wikipedia_zh_all_nopic_*.zim` in
`/mnt/shared/wiki_zim/`) · `LOCAL_WIKI_MAX_CHARS` (65535) ·
`LOCAL_WIKI_MAX_TITLES` (20) · `LOCAL_WIKI_LEAD_MAX` (8000 — lead-mode
threshold) · `LOCAL_WIKI_HTTP_PORT` (HTTP mode).

## Operations

**Status:** `ss -tlnp | grep 3211`

**Restart (HTTP instance):**
```bash
kill $(cat ~/.openclaw/workspace/local_wiki/run.pid) 2>/dev/null; sleep 2
cd ~/.openclaw/workspace/local_wiki && \
  LOCAL_WIKI_HTTP_PORT=3211 LOCAL_WIKI_ZIM=$PWD/wikipedia.zim \
  setsid nohup /usr/bin/python3 server.py >> /tmp/local-wiki-mcp.log 2>&1 < /dev/null &
sleep 5
ss -tlnp | grep ':3211 '   # record the pid= shown into run.pid
```
**After any code change, verify the live server over the wire** (the eval runs
in-process and will NOT catch a stale daemon):
```bash
curl -s -X POST http://127.0.0.1:3211/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**Regression suite:**
```bash
cd ~/.openclaw/workspace/local_wiki && \
  LOCAL_WIKI_ZIM=$PWD/wikipedia.zim /usr/bin/python3 run_eval.py
```
Expect `28 passed, 0 failed, 3 informational`. Non-zero exit = regression. The
3 `note`-marked cases are known libzim ranking limits — informational by design.

**ZIM refresh (manual, no schedule):**
```bash
/usr/bin/python3 refresh_zim.py --dry-run   # resolve newest kiwix build, check space
/usr/bin/python3 refresh_zim.py             # download → libzim verify → atomic swap
```
Keeps one `.bak` of the previous archive, updates the snapshot date in
`~/.pi/agent/AGENTS.md`, and prints a restart reminder. Newest build known at
time of writing: `wikipedia_en_all_nopic_2026-06.zim` (49.1 GB listed / 52.7 GB
on disk). First read after a swap is slow (lazy index build).

## Gotchas

1. **Two pythons.** Shell `python3` is Linuxbrew 3.14 — NO libzim. Always run
   with **`/usr/bin/python3`** (system 3.12, has libzim).
2. **`setsid … &` makes `$!` the wrapper PID, not the server's.** `run.pid`
   goes stale easily; record the real pid from `ss -tlnp` and verify over the wire.
3. **html2text state poisoning** (fixed — don't reintroduce): a shared
   `HTML2Text` instance returns empty forever after converting HTML truncated
   mid-tag. All conversion goes through `server.py:_convert()` (fresh instance
   per call).
4. **ZIM titles keep their spaces** — `get_entry_by_title("Dying Light")` works,
   `"Dying_Light"` doesn't. Lookup code tries both forms.
5. **pi tool cache**: pi snapshots the MCP tool list at session start; new or
   changed tools need a session restart or `/mcp` reconnect. OpenClaw stdio is
   unaffected (fresh spawn per session).
6. **Known search weakness**: libzim fulltext ranks by word *occurrence count*.
   Short 2–3 word queries ("United States tree", "Chernobyl reactor accident
   1986") can rank long/comparison articles above the main topic (main articles
   still land in top-5). Proper-noun queries are covered by the title-contains
   hoist. These cases are `note`-marked in `eval_set.jsonl` — don't "fix" them
   into failing the eval.
7. **Snapshot staleness**: the ZIM is frozen at download date (2026-08-27).
   Anything newer needs `web_search` (local SearXNG).
8. The ZIM lives on `/mnt/shared` (Windows-side share, root-owned dir) — 50 GB
   downloads take a while; `refresh_zim.py` is resumable.

## History

- 2026-08-27: built: `get` (lead/section/full), `search` with normalized
  title hoist, truncation footers, the `_convert()` poisoning fix,
  `refresh_zim.py`, and the 31-case eval suite.
- 2026-08-28 (0.1.0): FTS-first related-articles path; the FTS "outage" fixed
  (`libzim.Query(str)` silently builds an empty query — `_query()` helper);
  Chinese Wikipedia (`lang="zh"`, auto-detected); CJK miss-path per-gram merge
  (libzim 9.x drops `FLAG_DEFAULT` — OR/AND/phrase syntax is dead); first git
  tag. Full story: `INCIDENT_2026-08-28.md`.
- 2026-08-28 (0.1.1): CORS middleware so browser-based MCP clients can connect.
