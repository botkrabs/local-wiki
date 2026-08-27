#!/usr/bin/env python3
"""Tier-1 eval for the local_wiki MCP tools.

Deterministic tool-level checks (no LLM): loads eval_set.jsonl and calls the
`get` / `search` functions directly, scoring each against its `check`.

Run:  /usr/bin/python3 run_eval.py [--set eval_set.jsonl]

Tier-2 (model-level: does the LLM route to the right tool / read the right
section) is deliberately NOT here — it needs the agent in the loop and is
run ad hoc via pi, not as a batch.

Exit code: 0 if all non-skip checks pass, 1 otherwise.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_server():
    spec = importlib.util.spec_from_file_location("srv", os.path.join(HERE, "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    srv.archive()  # warm the ZIM once
    return srv


def call(srv, kind, args):
    fn = srv.get if kind in ("get", "redirect") else srv.search
    return fn(**args)


def top_titles(out, n):
    return [re.sub(r"^\d+\.\s*", "", ln).strip()
            for ln in out.splitlines() if re.match(r"^\d+\.\s", ln)][:n]


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def check(out, c):
    m = c["mode"]
    if m == "contains":
        return c["value"].lower() in out.lower()
    if m == "startswith":
        return out.strip().startswith(c["value"])
    if m == "marker":
        return bool(re.search(c["value"], out))
    if m == "topn":
        return norm(c["title"]) in [norm(t) for t in top_titles(out, c["value"])]
    if m == "topn_any":
        tops = [norm(t) for t in top_titles(out, c["value"])]
        return any(norm(t) in tops for t in c["titles"])
    if m == "lists_sections":
        return c["value"].lower() in out.lower() and "Sections:" in out
    if m == "notfound_contains":
        return out.strip().startswith("Article not found") and c["value"].lower() in out.lower()
    if m == "maxlen":
        return len(out) < c["value"]
    if m in ("not_contains", "not_contains_lead_marker"):
        return c["value"].lower() not in out.lower()
    raise ValueError(f"unknown check mode: {m}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=os.path.join(HERE, "eval_set.jsonl"))
    ap.add_argument("--only", help="substring filter on id")
    args = ap.parse_args()

    srv = load_server()
    rows = [json.loads(l) for l in open(args.set) if l.strip()]
    if args.only:
        rows = [r for r in rows if args.only in r["id"]]

    passed = skipped = failed = 0
    for r in rows:
        t0 = time.time()
        try:
            out = call(srv, r["kind"], r["args"])
            ok = check(out, r["check"])
        except Exception as e:  # noqa: BLE001
            out, ok = f"EXC: {e}", False
        ms = (time.time() - t0) * 1000
        # a 'note' marks an informational case (known weakness) — don't fail the run
        if r["check"].get("note"):
            tag = "INFO " if ok else "WARN "
            skipped += 1
        else:
            tag = "PASS  " if ok else "FAIL  "
            passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{tag}{r['id']:<9} {ms:6.0f}ms  {r['kind']} {r['args']}")
        if not ok and not r["check"].get("note"):
            print(f"       got: {out[:160]!r}")

    print(f"\n{passed} passed, {failed} failed, {skipped} informational, of {len(rows)} total")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
