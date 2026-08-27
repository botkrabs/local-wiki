#!/usr/bin/env python3
"""local_wiki-mcp: offline Wikipedia reader as an MCP server.

Design: ONE read tool, `get` — if the title exists it returns the article
text; if not, it returns related article titles so the model can recover on
its own.

Deps: libzim, mcp, html2text, uvicorn (pip --user --break-system-packages)
Env:
  LOCAL_WIKI_ZIM      path to the .zim archive
  LOCAL_WIKI_MAX_CHARS cap for article text (default 65535, like the original)
  LOCAL_WIKI_MAX_TITLES max related titles returned (default 20)
  LOCAL_WIKI_HTTP_PORT if set, run as stateless streamable-HTTP MCP on
                    0.0.0.0:<port> (/mcp) for pi and other LAN clients;
                    unset = stdio (default, used by OpenClaw)
"""
import os
import re

import html2text
import libzim
from mcp.server.mcpserver import MCPServer

ZIM_PATH = os.environ.get(
    "LOCAL_WIKI_ZIM", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wikipedia.zim")
)
MAX_CHARS = int(os.environ.get("LOCAL_WIKI_MAX_CHARS", "65535"))
MAX_TITLES = int(os.environ.get("LOCAL_WIKI_MAX_TITLES", "20"))
# articles longer than this return their lead (intro) by default; pass
# full=True for the whole article
LEAD_MAX = int(os.environ.get("LOCAL_WIKI_LEAD_MAX", "8000"))

mcp = MCPServer("local_wiki")

_archive = None
_sugg = None
_ft = None


def archive():
    global _archive, _sugg, _ft
    if _archive is None:
        _archive = libzim.reader.Archive(ZIM_PATH)
        _sugg = libzim.suggestion.SuggestionSearcher(_archive)
        if _archive.has_fulltext_index:
            _ft = libzim.search.Searcher(_archive)
    return _archive


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


def _article_entry(title: str):
    a = archive()
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


def _related(query: str):
    seen = []

    def add(t):
        t = t.replace("_", " ").strip()
        if t and t not in seen:
            seen.append(t)

    for t in _sugg.suggest(query).getResults(0, MAX_TITLES):
        add(t)
    if _ft is not None:
        q = libzim.Query()
        q.set_query(query)
        for t in _ft.search(q).getResults(0, MAX_TITLES):
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


def _snippet(title: str, words):
    """~220-char window around the first occurrence of a significant query
    word in the article body (longest words first)."""
    a = archive()
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


@mcp.tool()
def search(query: str, limit: int = 8) -> str:
    """Search offline Wikipedia article text by keywords or phrase. Returns up
    to `limit` ranked article titles, each with a short snippet of the matching
    text. Follow up with get(title) or get(title, section=...) to read a hit."""
    archive()
    if limit <= 0:
        limit = 8
    # libzim chokes on absurdly long queries; cap like the other ZIM servers
    query = query.strip()[:120]
    res = []
    if _ft is not None:
        q = libzim.Query()
        q.set_query(query)
        try:
            s = _ft.search(q)
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
        res = [t.replace("_", " ") for t in _sugg.suggest(query.strip()).getResults(0, limit)]
    if not res:
        return f"No articles matched: {query}"
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 3]
    return "\n".join(f"{i}. {t}" + _snippet(t, words) for i, t in enumerate(res, 1))


@mcp.tool()
def get(title: str, section: str = "", full: bool = False) -> str:
    """Read an offline Wikipedia article, or one section of it. Pass an exact
    article title (e.g. "American bison"). Long articles return their lead
    (intro) plus a section list by default — answer from the lead when it
    suffices, else pass section="Section Name" for one section or full=True
    for the whole article. If no article with that title exists, the tool
    returns similar article titles — pick one and call get again. If the
    section is not found, the tool lists the article's sections."""
    found = _article_entry(title)
    if found is None:
        related = _related(title)
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


if __name__ == "__main__":
    import asyncio
    http_port = os.environ.get("LOCAL_WIKI_HTTP_PORT")
    if http_port:
        asyncio.run(mcp.run_streamable_http_async(
            host="0.0.0.0",
            port=int(http_port),
            streamable_http_path="/mcp",
            stateless_http=True,
        ))
    else:
        asyncio.run(mcp.run_stdio_async())
