#!/usr/bin/env python3
"""Manual ZIM refresh for local_wiki.

Downloads the latest full-English Wikipedia ZIM (no images) from kiwix,
verifies it, and atomically swaps it into place. No scheduling — run by
hand whenever a fresher snapshot is wanted:

    /usr/bin/python3 refresh_zim.py            # latest wikipedia_en_all_nopic
    /usr/bin/python3 refresh_zim.py --url URL  # explicit file/URL
    /usr/bin/python3 refresh_zim.py --dry-run  # resolve + size check only

Layout:
  data dir:  /mnt/shared/wiki_zim/
    wikipedia_nopic.zim       <- stable name the workspace symlink points to
    wikipedia_nopic.zim.bak   <- previous good copy (one generation)
  server:    ~/.openclaw/workspace/local_wiki/ (env LOCAL_WIKI_ZIM)

The running server keeps serving the OLD data (its mmap holds the old inode)
until you restart it — the swap itself is non-disruptive.

Verify uses the SYSTEM python (3.12, has libzim), not the shell default.
"""
import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time

DATA_DIR = "/mnt/shared/wiki_zim"
STABLE = os.path.join(DATA_DIR, "wikipedia_nopic.zim")
BACKUP = STABLE + ".bak"
TMP = os.path.join(DATA_DIR, "wikipedia_nopic.dl")
LOCK = os.path.join(DATA_DIR, "refresh.lock")
LISTING_URL = "https://download.kiwix.org/zim/wikipedia/"
VERIFY_PY = "/usr/bin/python3"
MIN_BYTES = 10 * 1024**3          # full EN nopic is tens of GB; refuse small files
AGENTS_MD = os.path.expanduser("~/.pi/agent/AGENTS.md")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def curl(*args):
    return subprocess.run(["curl", *args], check=True, capture_output=True, text=True)


def resolve_latest():
    """Find the newest wikipedia_en_all_nopic_YYYY-MM.zim on the kiwix listing."""
    log("Fetching kiwix listing ...")
    html = curl("-sL", "--max-time", "60", LISTING_URL).stdout
    names = sorted(set(re.findall(r'href="(wikipedia_en_all_nopic_\d{4}-\d{2}\.zim)"', html)))
    if not names:
        fail("no wikipedia_en_all_nopic_*.zim found in listing — naming may have changed")
    name = names[-1]
    return f"{LISTING_URL.rstrip('/')}/{name}"


def content_length(url):
    out = curl("-sIL", "--max-time", "60", url).stdout
    sizes = [int(s) for s in re.findall(r"(?im)^content-length:\s*(\d+)", out)]
    return sizes[-1] if sizes else None


def acquire_lock():
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"pid {os.getpid()} at {time.ctime()}\n".encode())
        return fd
    except FileExistsError:
        with open(LOCK) as f:
            fail(f"another refresh appears to be running: {f.read().strip()}")


def check_space(url, size):
    free = shutil.disk_usage(DATA_DIR).free
    need = size + int(size * 0.10)  # file + .bak + margin
    if free < need:
        fail(f"need ~{need / 2**30:.0f} GB free in {DATA_DIR}, have {free / 2**30:.0f} GB")
    log(f"file is {size / 2**30:.1f} GB; {free / 2**30:.0f} GB free in {DATA_DIR} — ok")


def download(url):
    log(f"downloading {url}")
    log("-> " + TMP)
    log("resumable; safe to interrupt and re-run")
    for attempt in range(3):
        r = subprocess.run(
            ["curl", "-fL", "--retry", "5", "--retry-delay", "10", "--retry-all-errors",
             "-C", "-", "-o", TMP, "--max-time", "0", url])
        if r.returncode == 0:
            break
        log(f"curl exited {r.returncode}; retrying (attempt {attempt + 1}/3) ...")
        time.sleep(10)
    else:
        fail("download failed after 3 attempts — remove the partial file and re-run")


def verify(path, expected):
    log("verifying with libzim ...")
    code = f"""
import libzim
a = libzim.reader.Archive({path!r})
print("articles:", a.count_articles)
print("main page:", a.get_main_page().title)
"""
    r = subprocess.run([VERIFY_PY, "-c", code], capture_output=True, text=True)
    out = r.stdout.strip()
    if r.returncode != 0:
        fail(f"libzim verification failed (file may be corrupt):\n{out}\n{r.stderr}")
    print(out)
    if expected and os.path.getsize(path) != expected:
        fail(f"size mismatch: {os.path.getsize(path)} != {expected}")
    log("verification OK")


def swap():
    new_size = os.path.getsize(TMP)
    if new_size < MIN_BYTES:
        fail(f"new file is only {new_size / 2**30:.2f} GB (< {MIN_BYTES / 2**30} GB) — refusing to swap")
    if os.path.exists(STABLE):
        log(f"backing up current -> {BACKUP}")
        os.replace(STABLE, BACKUP)
    log(f"swapping new -> {STABLE}")
    os.replace(TMP, STABLE)
    log("swap complete")


def update_notes():
    """Point the AGENTS.md snapshot date at today (the date we actually got the data)."""
    if not os.path.exists(AGENTS_MD):
        log(f"note: {AGENTS_MD} not found, skipping notes update")
        return
    src = open(AGENTS_MD).read()
    today = datetime.date.today().isoformat()
    new, n = re.subn(r"\(currently \d{4}-\d{2}-\d{2}\)", f"(currently {today})", src, count=1)
    if n:
        open(AGENTS_MD, "w").write(new)
        log(f"updated AGENTS.md snapshot date -> {today}")
    else:
        log("note: no '(currently YYYY-MM-DD)' line in AGENTS.md, leaving it alone")


def restart_hint():
    print("""
The running server still serves the OLD snapshot (mmap keeps the old inode).
Restart it to load the new ZIM:

    kill $(cat $HOME/.openclaw/workspace/local_wiki/run.pid) 2>/dev/null; sleep 2
    # ~/.bashrc auto-starts it on the next interactive shell, or start it now:
    cd $HOME/.openclaw/workspace/local_wiki \\
      && LOCAL_WIKI_HTTP_PORT=3211 LOCAL_WIKI_ZIM=$PWD/wikipedia.zim \\
      setsid nohup /usr/bin/python3 server.py >> /tmp/local-wiki-mcp.log 2>&1 < /dev/null & \\
      echo $! > run.pid; sleep 5

Then verify:  curl -s 'http://127.0.0.1:3211/mcp' >/dev/null && echo up
""")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="explicit ZIM URL or local path (skip listing resolution)")
    ap.add_argument("--dry-run", action="store_true", help="resolve URL and check size/space only")
    ap.add_argument("--no-notes", action="store_true", help="don't touch AGENTS.md snapshot date")
    args = ap.parse_args()

    url = args.url or resolve_latest()
    is_remote = url.startswith("http://") or url.startswith("https://")
    size = None
    if is_remote:
        log(f"target ZIM: {url}")
        size = content_length(url)
        if size:
            check_space(url, size)
        else:
            log("could not determine remote size (continuing; will verify by size after download)")
    else:
        log(f"using local path {url}")

    if args.dry_run:
        log("dry run complete")
        return

    lock = acquire_lock()
    try:
        if is_remote:
            download(url)
        else:
            log(f"copying {url} -> {TMP}")
            shutil.copyfile(url, TMP)
        verify(TMP, size)
        swap()
        if not args.no_notes:
            update_notes()
        restart_hint()
    finally:
        os.unlink(LOCK)


if __name__ == "__main__":
    main()
