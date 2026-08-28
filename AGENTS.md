# AGENTS.md — local_wiki

Offline Wikipedia (ZIM) as an MCP server. Two tools: `get` (exact title →
article text; long articles return lead + section list by default) and
`search` (phrase → ranked candidates).

## Install

A Python with these packages: `libzim`, `mcp`, `html2text`, `uvicorn`
(e.g. `pip --user --break-system-packages install libzim mcp html2text uvicorn`).
If the shell's `python3` is a different interpreter (e.g. Linuxbrew),
invoke the system one explicitly (`/usr/bin/python3`).

## Download a ZIM (manual)

No updater script — download and replace by hand; any kiwix build works
(full English, no images = `wikipedia_en_all_nopic_YYYY-MM.zim`, ~49 GB;
listing: <https://download.kiwix.org/zim/wikipedia/>). The file belongs at
`wiki_zim/wikipedia_nopic.zim` (the committed `wikipedia.zim` symlink
resolves there).

    # 1. download (resumable)
    curl -C - -L -o wiki_zim/wikipedia_nopic.zim.new '<URL>'
    # 2. VERIFY before swapping — a truncated 50 GB file is otherwise
    #    indistinguishable until first use
    /usr/bin/python3 -c "import libzim; a=libzim.reader.Archive('wiki_zim/wikipedia_nopic.zim.new'); print('articles:', a.count_articles)"
    # 3. swap, keeping the previous archive as .bak (rollback)
    mv wiki_zim/wikipedia_nopic.zim wiki_zim/wikipedia_nopic.zim.bak 2>/dev/null || true
    mv wiki_zim/wikipedia_nopic.zim.new wiki_zim/wikipedia_nopic.zim
    # 4. restart the server — the running instance keeps the old snapshot
    #    in mmap until restart; first read after a swap is slow (lazy index)

Verify with the interpreter that has libzim, and check disk space first —
the full build is tens of GB.

## Operation

    LOCAL_WIKI_HTTP_PORT=3211 LOCAL_WIKI_ZIM=$PWD/wikipedia.zim \
        /usr/bin/python3 server.py

Stateless streamable-HTTP MCP at `http://127.0.0.1:3211/mcp`. Without
`LOCAL_WIKI_HTTP_PORT` the server runs in stdio mode (for MCP clients
that spawn the process).

Check it's up:

    curl -s 'http://127.0.0.1:3211/mcp' >/dev/null && echo up
