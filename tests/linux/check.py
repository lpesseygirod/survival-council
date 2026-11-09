"""Runs inside the Linux container as the protected user `council`."""
import json, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

from survival.config import Paths, load_config
from survival.sandbox import Sandbox

cfg = load_config()
paths = Paths.for_mode("paper", None)
sb = Sandbox(cfg, paths, "paper")
assert sb.kind == "bwrap", sb.kind
print("code folder:", paths.code)

# a stand-in gateway on 127.0.0.1
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({"result": {"ok": self.headers.get("Authorization")}}).encode())
srv = HTTPServer(("127.0.0.1", cfg.gateway_port), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

(paths.code / "probe.py").write_text(r'''
import os, json, urllib.request
r = {}
def tryread(p):
    try: open(p).read(); return "READ"
    except Exception as e: return type(e).__name__
r["uid"] = os.getuid()
r["vault"] = tryread("/home/council/app/secrets/vault.key")
r["config"] = tryread("/home/council/app/config.toml")
r["game_code"] = tryread("/home/council/app/survival/watchdog.py")
r["home_listing"] = os.path.exists("/home/council")
r["shadow"] = tryread("/etc/shadow")
r["env"] = sorted(os.environ)
r["pid1"] = open("/proc/1/cmdline").read().replace("\0", " ")
open("/work/written.txt", "w").write("ok"); r["write_work"] = True
try: open("/usr/evil", "w"); r["write_usr"] = True
except Exception as e: r["write_usr"] = type(e).__name__
import council
r["sdk"] = council._call("status")
print(json.dumps(r))
''')
res = sb.run(["probe.py"], "tok123", 60)
print("probe exit", res["exit_code"], res["stderr"][-2000:])
r = json.loads(res["stdout"].strip().splitlines()[-1])
print(json.dumps(r, indent=1))
assert r["uid"] != 0 and r["vault"] != "READ" and r["config"] != "READ" and r["game_code"] != "READ"
assert r["home_listing"] is False and r["shadow"] != "READ" and r["write_usr"] != True
assert r["write_work"] and r["sdk"] == {"ok": "Bearer tok123"}
assert "COUNCIL_CODE_ROOT" not in r["env"] and "PYTHONPATH" in r["env"]
assert (paths.code / "written.txt").read_text() == "ok"  # protected side can read its files

# pause / resume / kill catch every process of the sandbox user
(paths.code / "loop.py").write_text("import os,time\nos.setsid() if hasattr(os,'setsid') else None\nwhile True: time.sleep(0.1)\n")
argv, env, cwd = sb.command(["loop.py"], "t")
p = subprocess.Popen(argv, env=env, cwd=cwd, start_new_session=True)
time.sleep(1.5)
def states():
    out = subprocess.run(["ps", "-u", cfg.sandbox_user, "-o", "stat=,args="], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "loop.py" in l and "bwrap" not in l and "sudo" not in l]
print("running:", states())
sb.signal_all("STOP"); time.sleep(0.5)
print("paused:", states()); assert states() and all(s.strip().startswith("T") for s in states())
sb.signal_all("CONT"); time.sleep(0.5)
assert all(not s.strip().startswith("T") for s in states())
sb.signal_all("KILL"); time.sleep(1)
print("after kill:", states()); assert states() == []
print("SANDBOX CHECK PASSED")
