#!/usr/bin/env python3
"""local_wiki-mcp: offline Wikipedia reader as an MCP server.

Design: ONE read tool, `get` — if the title exists it returns the article
text; if not, it returns related article titles so the model can recover on
its own.

Deps: libzim, mcp, html2text, uvicorn (pip --user --break-system-packages)
Env:
  LOCAL_WIKI_ZIM      path to the .zim archive (English, lang="en")
  LOCAL_WIKI_ZIM_ZH   optional: path to the Chinese .zim archive (lang="zh")
  LOCAL_WIKI_MAX_CHARS cap for article text (default 65535, like the original)
  LOCAL_WIKI_MAX_TITLES max related titles returned (default 20)
  LOCAL_WIKI_HTTP_PORT if set, run as stateless streamable-HTTP MCP on
                    0.0.0.0:<port> (/mcp) for pi and other LAN clients;
                    unset = stdio (default, used by OpenClaw)
"""
import os
import re

import glob
import html2text
import libzim
from mcp.server.mcpserver import MCPServer

ZIM_PATH = os.environ.get(
    "LOCAL_WIKI_ZIM", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wikipedia.zim")
)
# zh archive: env wins; otherwise auto-detect the newest full zh nopic zim
# (so stdio instances without the env var still get Chinese).
ZH_PATH = os.environ.get("LOCAL_WIKI_ZIM_ZH", "")
if not ZH_PATH:
    _here = os.path.dirname(os.path.abspath(__file__))
    _cands = sorted(glob.glob(os.path.join(_here, "wiki_zim", "wikipedia_zh_all_nopic_*.zim")))
    if _cands:
        ZH_PATH = _cands[-1]
_ZIM_PATHS = {"en": ZIM_PATH}
if ZH_PATH:
    _ZIM_PATHS["zh"] = ZH_PATH

# advertised in the MCP tool descriptions so the client sees what is actually
# available (and where) — a missing ZIM shows up as MISSING instead of failing
# on first call
_ZIM_NOTE = ("\n\nAvailable: " + ", ".join(
    (f"{c}"  # code+availability only — no paths/sizes in tool metadata
     if os.path.exists(p) else f"{c}=MISSING")
    for c, p in sorted(_ZIM_PATHS.items())))
MAX_CHARS = int(os.environ.get("LOCAL_WIKI_MAX_CHARS", "65535"))
MAX_TITLES = int(os.environ.get("LOCAL_WIKI_MAX_TITLES", "20"))
# articles longer than this return their lead (intro) by default; pass
# full=True for the whole article
LEAD_MAX = int(os.environ.get("LOCAL_WIKI_LEAD_MAX", "8000"))

mcp = MCPServer("local_wiki")

_ARCHS = {}  # lang -> {"archive":..., "sugg":..., "ft":...} (lazily opened)


def archive(lang="en"):
    """Open (once) and return the Archive for `lang` ('en' or 'zh')."""
    if lang not in _ZIM_PATHS:
        raise ValueError(f"unknown wiki lang: {lang!r} (available: {sorted(_ZIM_PATHS)})")
    if lang not in _ARCHS:
        a = libzim.reader.Archive(_ZIM_PATHS[lang])
        _ARCHS[lang] = {
            "archive": a,
            "sugg": libzim.suggestion.SuggestionSearcher(a),
            "ft": libzim.search.Searcher(a) if a.has_fulltext_index else None,
        }
    return _ARCHS[lang]["archive"]


_HEADING_RE = re.compile(r"<h([23])[^>]*>(.*?)</h\1>", re.S)


def _toc(html: str):
    """Sections as (level, title, html_start, html_end); a section ends at the
    next heading of the same or higher level."""
    heads = []
    for m in _HEADING_RE.finditer(html):
        t = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        heads.append((int(m.group(1)), t, m.start(), m.end()))
    sections = []
    for i, (lvl, title, s, _e) in enumerate(heads):
        end = len(html)
        for lvl2, _t2, s2, _e2 in heads[i + 1:]:
            if lvl2 <= lvl:
                end = s2
                break
        sections.append((lvl, title, s, end))
    return sections


def _norm(s: str):
    return re.sub(r"\s+", " ", s).strip().lower()


def _cap(text: str):
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + (f"\n\n[truncated: showing first {MAX_CHARS} of "
                                  f"{len(text)} chars — pass the section parameter "
                                  f"to read one section only]")
    return text


def _article_entry(title: str, lang: str = "en"):
    a = archive(lang)
    norm = title.strip().replace(" ", "_")
    e = None
    if a.has_entry_by_title(norm):
        e = a.get_entry_by_title(norm)
    elif a.has_entry_by_title(title.strip()):
        e = a.get_entry_by_title(title.strip())
    if e is None:
        return None
    while e.is_redirect:
        e = e.get_redirect_entry()
    html = bytes(e.get_item().content).decode("utf-8", errors="ignore")
    # drop style/script blocks so they can't leak into the text
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html, flags=re.S)
    return e, html


def _query(text: str):
    """Correct libzim Query.

    2026-08-28 incident: `libzim.Query(text)` does NOT raise — the binding's
    Query cdef class has no string ctor (only set_query()), yet Cython's
    generated tp_new silently accepts the arg and yields an EMPTY query
    (0 results, no error, ~6 ms). That masked as "FTS is dead" for a day.
    Always go through this helper.
    """
    return libzim.Query().set_query(text)


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _cjk_loose(query: str, st, limit=20):
    """Loose CJK match without OR: one ranked query per overlapping 2-gram
    (plus any non-CJK tokens), merged by co-occurrence. Needed because
    libzim 9.x compiles queries with parse_query(str, FLAG_CJK_NGRAM) — the
    2-arg overload DROPS FLAG_DEFAULT, so OR/AND/phrase syntax is silently
    dead in this binding (libzim bug, unfixable from Python). A title string
    AND-joined gram-by-gram finds nothing; merging per-gram ranked lists
    approximates 'share the most terms' ranking."""
    grams, seen = [], set()
    for tok in query.split():
        glist = [tok[i:i + 2] for i in range(len(tok) - 1)] or [tok] \
            if _CJK_RE.search(tok) else [tok]
        for g in glist:
            if g not in seen:
                seen.add(g)
                grams.append(g)
    grams = grams[:8]
    best = {}  # title -> (n_grams_matched, best_position)
    for g in grams:
        try:
            res = st["ft"].search(_query(g)).getResults(0, limit)
        except RuntimeError:
            continue
        for pos, t in enumerate(res):
            t = t.replace("_", " ").strip()
            if not t:
                continue
            cur = best.get(t)
            if cur is None or pos < cur[1]:
                best[t] = (cur[0] + 1 if cur else 1, pos)
    min_hits = 2 if len(grams) >= 3 else 1
    ranked = [t for t, (hits, _p) in best.items() if hits >= min_hits]
    ranked.sort(key=lambda t: (-best[t][0], best[t][1]))
    return ranked


def _related(query: str, lang: str = "en"):
    st = _ARCHS[lang]
    seen = []

    def add(t):
        t = t.replace("_", " ").strip()
        if t and t not in seen:
            seen.append(t)

    # FTS first: single-digit ms on a warm archive, while SuggestionSearcher
    # can take ~0.7s on common words (seconds on cold drvfs reads). Suggest
    # remains the fallback when FTS finds nothing (or the index is absent).
    if st["ft"] is not None:
        # BUG (2026-08-28 incident): libzim.Query(str) does NOT raise — it
        # silently creates an EMPTY query (binding has no string ctor; only
        # set_query()). Always use Query().set_query(text).
        # CJK: a guessed-title string is AND-joined gram by gram and usually
        # finds nothing; fall back to the per-gram merge (OR syntax is dead
        # in libzim 9.x — see _cjk_loose).
        try:
            for t in st["ft"].search(_query(query)).getResults(0, MAX_TITLES):
                add(t)
        except RuntimeError:
            pass
        if not seen and _CJK_RE.search(query):
            for t in _cjk_loose(query, st, MAX_TITLES):
                add(t)
    if not seen:
        for t in st["sugg"].suggest(query).getResults(0, MAX_TITLES):
            add(t)
    return seen[:MAX_TITLES]


def _convert(html: str) -> str:
    """HTML -> text with a FRESH converter. The shared _h must not be used on
    truncated HTML: cutting mid-tag leaves an open tag that poisons the
    parser state, making all later handle() calls return empty."""
    c = html2text.HTML2Text()
    c.ignore_images = True
    c.ignore_links = True
    c.body_width = 0
    return c.handle(html)


def _snippet(title: str, words, lang: str = "en"):
    """~220-char window around the first occurrence of a significant query
    word in the article body (longest words first)."""
    a = archive(lang)
    e = None
    for cand in (title, title.replace(" ", "_")):
        if a.has_entry_by_title(cand):
            e = a.get_entry_by_title(cand)
            break
    if e is None:
        return ""
    while e.is_redirect:
        e = e.get_redirect_entry()
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", "",
                  bytes(e.get_item().content).decode("utf-8", errors="ignore")[:60000],
                  flags=re.S)
    text = re.sub(r"\s+", " ", _convert(html))
    for w in sorted(words, key=len, reverse=True):
        i = text.lower().find(w)
        if i >= 0:
            lo = max(0, i - 110)
            return ("\n   " + ("…" if lo > 0 else "") + text[lo:i + 110].strip()
                    + ("…" if i + 110 < len(text) else ""))
    return "\n   " + text[:220].strip()


def search(query: str, limit: int = 8, lang: str = "en") -> str:
    """Search offline Wikipedia (full-text) by keyword or phrase. Returns up
    to `limit` ranked titles with matching snippets; feed a title to get().
    lang: 'en' (default) or 'zh' — match the archive to the query language;
    for zh topics the zh article is usually fuller. Never mix languages: a
    zh phrase against the en archive (or vice versa) finds nothing.
    No hits: retry once, shorter or more specific; two empty searches is
    strong evidence no such article exists.
    Frozen 2026 snapshot — reference, not current events."""
    try:
        archive(lang)
    except ValueError as e:
        return str(e)
    st = _ARCHS[lang]
    if limit <= 0:
        limit = 8
    # libzim chokes on absurdly long queries; cap like the other ZIM servers
    query = query.strip()[:120]
    res = []
    if st["ft"] is not None:
        try:
            s = st["ft"].search(_query(query))  # _query(): see _related
            # fetch a wider pool so the title-contains hoist below can see
            # candidates beyond the raw top-N
            res = [t.replace("_", " ") for t in s.getResults(0, max(limit * 5, 25))]
        except RuntimeError:
            res = []
    if res:
        # an article whose title contains the whole query outranks articles
        # that merely mention it (fulltext ranks by occurrence count).
        # Normalized comparison: "hungry jacks" matches "Hungry Jack's".
        norm = lambda s: re.sub(r"\s+", " ",
                                s.replace("'", "").replace("\u2019", "")
                                 .replace("-", " ").replace("\u2013", " ")
                                 .lower()).strip()
        ql = norm(query)
        res = [t for t in res if ql in norm(t)] + \
              [t for t in res if ql not in norm(t)]
        res = res[:limit]
    if not res:
        res = [t.replace("_", " ") for t in st["sugg"].suggest(query.strip()).getResults(0, limit)]
    if not res:
        return f"No articles matched: {query}"
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 3]
    words += [ch for ch in query if "\u4e00" <= ch <= "\u9fff"]
    return "\n".join(f"{i}. {t}" + _snippet(t, words, lang) for i, t in enumerate(res, 1))


def get(title: str, section: str = "", full: bool = False, lang: str = "en") -> str:
    """Read a Wikipedia article by exact title (unknown → search first).
    Long articles return the lead (intro) + section list by default; answer
    from the lead when it suffices, else section="Name" for one section or
    full=True for all; a wrong section name returns the full section list.
    lang: 'en' (default) or 'zh', as in search.
    Title not found → similar titles are listed; pick the semantically
    right one and retry once."""
    try:
        archive(lang)
    except ValueError as e:
        return str(e)
    found = _article_entry(title, lang)
    if found is None:
        related = _related(title, lang)
        if related:
            return "Article not found. Related articles:\n" + "\n".join(related)
        return f"Article not found, and no related articles for: {title}"
    e, html = found
    art_title = e.title.replace("_", " ")

    if section:
        secs = _toc(html)
        for lvl, stitle, s, end in secs:
            if _norm(stitle) == _norm(section):
                body = _convert(html[s:end])
                return (f"Section '{stitle}' of {art_title}:\n" + _cap(body).strip())
        listing = "\n".join("  " * (lvl - 2) + "- " + stitle
                            for lvl, stitle, _s, _e in secs)
        return (f"Section '{section}' not found in {art_title}. Sections:\n" + listing)

    text = _convert(html)
    heads = [t for _l, t, _s, _e in _toc(html) if _l == 2]
    if not full and len(text) > LEAD_MAX:
        # lead mode: intro up to the first h2 + section list + pointer.
        # (no h2 at all -> no lead boundary -> fall through to full text)
        lead_end = next((s for l, _t, s, _e in _toc(html) if l == 2), None)
        if lead_end is not None:
            lead = _convert(html[:lead_end]).strip()
            return ("Article:\n" + art_title + "\n" + lead +
                    f"\n\n[long article: {len(text)} chars — showing the lead "
                    f"only. Pass section=\"Name\" for one section, or "
                    f"full=True for the whole article.\n"
                    f"Sections:\n" + "\n".join("- " + t for t in heads) + "]")
    if len(text) > MAX_CHARS:
        text = (text[:MAX_CHARS] +
                f"\n\n[truncated: showing first {MAX_CHARS} of {len(text)} chars — "
                f"call get with section=\"Name\" to read a specific section.\n"
                f"Sections:\n" + "\n".join("- " + t for t in heads) + "]")
    elif len(text) > 20_000:
        text += (f"\n\n[long article: {len(text)} chars — call get with "
                f"section=\"Name\" to fetch a section only. "
                f"Sections: " + ", ".join(heads) + "]")
    return "Article:\n" + art_title + "\n" + text


# register the tools with their docstrings + the live archive list
for _fn in (search, get):
    _fn.__doc__ = (_fn.__doc__ or "") + _ZIM_NOTE
    mcp.tool()(_fn)


if __name__ == "__main__":
    import asyncio
    http_port = os.environ.get("LOCAL_WIKI_HTTP_PORT")
    if http_port:
        # Build the Starlette app ourselves (same args as run_streamable_http_async)
        # so we can wrap it in CORS: browser-based MCP clients (e.g. the llama.cpp
        # web UI on :8090) connect cross-origin, and without Access-Control-*
        # headers the browser kills the handshake with "Failed to fetch" before
        # the request ever reaches us. Non-browser clients send no Origin header
        # and are unaffected. starlette is already an mcp dependency.
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        app = mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            host="0.0.0.0",
        )
        app = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["mcp-session-id"],
        )
        uvicorn.run(app, host="0.0.0.0", port=int(http_port), log_level="info")
    else:
        asyncio.run(mcp.run_stdio_async())
