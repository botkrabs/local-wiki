import json, os, time, urllib.request

# same env var the server itself uses (server.py LOCAL_WIKI_HTTP_PORT)
BASE = "http://127.0.0.1:%s/mcp" % os.environ.get("LOCAL_WIKI_HTTP_PORT", "3211")
HDRS = {"Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}

def post(payload):
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode(), headers=HDRS)
    t0 = time.time()
    resp = urllib.request.urlopen(req)
    body = resp.read().decode()
    dt = time.time() - t0
    return dt, body, dict(resp.headers)

dt, body, hdrs = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "e2e", "version": "0"}}})
print("initialize: %.3fs (stateless, no session expected)" % dt)

def call(n, title, tag):
    dt, body, _ = post({"jsonrpc": "2.0", "id": n, "method": "tools/call",
        "params": {"name": "get", "arguments": {"title": title}}})
    data = json.loads(body.split("data: ", 1)[1])
    text = data["result"]["content"][0]["text"]
    snippet = text.split("\n", 2)[2][:50] if text.startswith("Article:") else text.split("\n", 1)[-1][:50]
    print("%s: %.2fs | %d chars | %r" % (tag, dt, len(text), snippet))

call(2, "United States", "E2E big    (United States)")
call(3, "Quantum gravity", "E2E medium (Quantum gravity)")
call(4, "Largest mammal in North America", "E2E miss   -> related")
call(5, "London", "E2E big    (London, truncates)")
call(6, "London", "E2E repeat (London)")
call(7, "Bison bison", "E2E species  (Bison bison)")
