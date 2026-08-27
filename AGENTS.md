# AGENTS.md — local_wiki

Offline Wikipedia (ZIM) as an MCP server. Two tools: `get` (exact title →
article text; long articles return lead + section list by default) and
`search` (phrase → ranked candidates).

## Install

A Python with these packages: `libzim`, `mcp`, `html2text`, `uvicorn`
(e.g. `pip --user --break-system-packages install libzim mcp html2text uvicorn`).
If the shell's `python3` is a different interpreter (e.g. Linuxbrew),
invoke the system one explicitly (`/usr/bin/python3`).

## Download a ZIM

Full English Wikipedia, no images: the newest
`wikipedia_en_all_nopic_YYYY-MM.zim` (~49 GB) from
<https://download.kiwix.org/zim/wikipedia/>.

The bundled updater does it end-to-end (resolves the newest build, checks
free space, resumable download, libzim verification, atomic swap, keeps
one `.bak` of the previous file):

    /usr/bin/python3 refresh_zim.py            # newest build
    /usr/bin/python3 refresh_zim.py --dry-run  # resolve + size check only
    /usr/bin/python3 refresh_zim.py --url URL  # explicit file/URL

It prints a restart hint when done — the running server keeps the old
snapshot in mmap until you restart it. Point the `wikipedia.zim` symlink
at the file the script installed (see `DATA_DIR` in `refresh_zim.py`).

## Operation

    LOCAL_WIKI_HTTP_PORT=3211 LOCAL_WIKI_ZIM=$PWD/wikipedia.zim \
        /usr/bin/python3 server.py

Stateless streamable-HTTP MCP at `http://127.0.0.1:3211/mcp`. Without
`LOCAL_WIKI_HTTP_PORT` the server runs in stdio mode (for MCP clients
that spawn the process).

Check it's up:

    curl -s 'http://127.0.0.1:3211/mcp' >/dev/null && echo up
