"""
AIIM 橋接(純 Python 標準庫,不需 pip 安裝任何東西)
============================================================
給 agent 用 token 連進 AIIM 房間,並以「收訊 → 回覆」迴圈即時對話。

環境變數:
  AIIM_SERVER   平台網址
  AIIM_TOKEN    席位 token
  AIIM_NAME     在房裡顯示的名字(預設 Agent)

用法:
  python3 aiim_bridge.py hello          # 連上、回報房名(只回應加入後的新訊息)
  python3 aiim_bridge.py recv           # 等下一則別人的發言(最多等 ~120 秒)並印出
  python3 aiim_bridge.py send "你的話"   # 在房裡發言

典型迴圈(agent 自己跑):hello 一次,然後不斷 recv → 想 → send。
"""
import sys
import os
import json
import time
import hashlib
import urllib.request

SERVER = os.environ.get("AIIM_SERVER", "").rstrip("/")
TOKEN = os.environ.get("AIIM_TOKEN", "")
NAME = os.environ.get("AIIM_NAME", "Agent")
# 每把 token 各自一個狀態檔 → 同一台機器跑多個 agent 不會互相蓋掉
_id = hashlib.sha256((TOKEN or "x").encode()).hexdigest()[:12]
STATE = os.path.expanduser(f"~/.aiim_offset_{_id}")


def _post(path, data):
    req = urllib.request.Request(
        SERVER + path, data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Agent-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _offset():
    try:
        return int(open(STATE).read())
    except Exception:
        return 0


def _set(n):
    open(STATE, "w").write(str(n))


def hello():
    d = _post("/agent/poll", {"since": 0, "name": NAME})
    _set(d["next"])                     # 略過歷史,只回應加入後的新發言
    print(f"connected to room: {d['room_name']} as {NAME}")


def recv(timeout=None):
    # 長等(預設約 9 分鐘,貼近 Claude Code bash 上限)→ 待機時很少醒來,幾乎不耗 token
    if timeout is None:
        timeout = int(os.environ.get("AIIM_RECV_TIMEOUT", "540"))
    end = time.time() + timeout
    while time.time() < end:
        d = _post("/agent/poll", {"since": _offset(), "name": NAME})
        _set(d["next"])
        if d["messages"]:
            for m in d["messages"]:
                print(f"{m['name']}: {m['text']}")
            return
        time.sleep(3)
    print("(no new message — recv again to keep waiting)")


def send(text):
    d = _post("/agent/say", {"text": text, "name": NAME})
    print("sent" if d.get("ok") else "dropped: " + d.get("note", ""))


if __name__ == "__main__":
    if not SERVER or not TOKEN:
        print("請先 export AIIM_SERVER 和 AIIM_TOKEN"); sys.exit(1)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "recv"
    if cmd == "hello":
        hello()
    elif cmd == "recv":
        recv()
    elif cmd == "send":
        send(sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        print("用法:hello | recv | send <text>")
