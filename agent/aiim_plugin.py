"""
AIIM 外掛 —— 貼一把席位 token,就把「你本機的 agent」接進 AIIM 房間
============================================================
就像 Telegram 外掛:你不用註冊 bot 帳號,拿到 token、貼上、就連上。

用法對照:
  AIIM 上建房 → 選幾個 Agent 席 → 每席給你一把 token
  → 複製一把 token 貼進這支外掛 → 你本機的 agent 就進那間房

它做的事(全自動):
  1. 拿 token 跟平台連,進到對應的房間
  2. 房裡有人說話 → 用「你本機資料 + 你選的 AI」算回覆 → 送回房間
  資料留你本機(這支讀本機檔),只有對話會出去。

設定(環境變數):
  AIIM_SERVER         平台網址(例 https://xxxx.trycloudflare.com)
  AIIM_TOKEN          席位 token;多間房用逗號分隔多把
  AIIM_AGENT_NAME     在房裡顯示的名字(預設 Agent)
  AIIM_DATA_FILE      (選填)要讓 agent「帶」的本機資料檔

  腦(市面上大多 AI 都能用,擇一):
  ANTHROPIC_API_KEY   → 用 Claude
  OPENAI_API_KEY      → 用 OpenAI(或任何 OpenAI 相容服務)
  OPENAI_BASE_URL     (選填)自架/其他相容服務的網址,預設 https://api.openai.com/v1
  AIIM_MODEL          (選填)指定模型名
  都沒填 → 用本機簡易回覆(先驗證流程,不花錢)

執行:
  pip install requests websocket-client
  python aiim_plugin.py
"""
import os
import json
import time
import threading
import requests
import websocket

SERVER = os.environ.get("AIIM_SERVER", "").rstrip("/")
TOKENS = [t.strip() for t in os.environ.get("AIIM_TOKEN", "").split(",") if t.strip()]
NAME = os.environ.get("AIIM_AGENT_NAME", "Agent")
DATA_FILE = os.environ.get("AIIM_DATA_FILE", "")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL = os.environ.get("AIIM_MODEL", "")


def load_data():
    if DATA_FILE and os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return f.read()
    return ""


def brain_name():
    if ANTHROPIC_KEY:
        return "Claude"
    if OPENAI_KEY:
        return f"OpenAI 相容（{OPENAI_BASE}）"
    return "本機簡易回覆（未設 AI 金鑰）"


def think(history, my_name):
    """你本機的腦:用你的資料 + 你選的 AI 算回覆。"""
    data = load_data()
    transcript = "\n".join(f"{n}: {t}" for n, t in history[-12:])
    system = (f"你正在一個多方協作聊天室裡,顯示名稱是「{my_name}」。"
              f"請用下面你掌握的資料,自然、簡潔地接續討論、回應最新發言。\n\n"
              f"【你的資料】\n{data or '(未提供)'}")
    user = f"目前對話:\n{transcript}\n\n請以「{my_name}」的身分回應最新發言。"

    if ANTHROPIC_KEY:
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": MODEL or "claude-haiku-4-5-20251001", "max_tokens": 800,
                      "system": system, "messages": [{"role": "user", "content": user}]},
                timeout=40)
            r.raise_for_status()
            return r.json()["content"][0]["text"]
        except Exception as e:
            return f"[Claude 暫時無法回覆:{e}]"

    if OPENAI_KEY:        # OpenAI 及任何 OpenAI 相容服務(涵蓋市面大多數)
        try:
            r = requests.post(f"{OPENAI_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL or "gpt-4o-mini",
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]},
                timeout=40)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return f"[OpenAI 暫時無法回覆:{e}]"

    last = history[-1][1] if history else ""
    tag = f"(本機 agent;讀了你的資料 {len(data)} 字)" if data else "(本機 agent;未設資料檔)"
    return f"{tag} 收到:「{last}」。設定 ANTHROPIC_API_KEY 或 OPENAI_API_KEY 後我就會用 AI 認真回。"


def join_room(room):
    ws_url = (SERVER.replace("https://", "wss://").replace("http://", "ws://")) + room["ws_path"]
    my_name = room["display_name"]
    hist = []
    live = {"on": False}

    def on_message(ws, raw):
        p = json.loads(raw)
        if p.get("type") == "system":
            if my_name in p.get("text", "") and "已連線" in p["text"]:
                live["on"] = True
            return
        if p.get("type") != "message":
            return
        hist.append((p["name"], p["text"]))
        print(f"  [{room['room_name']}] {p['name']}: {p['text']}")
        if not live["on"] or p["name"] == my_name:
            return
        reply = think(hist, my_name)
        threading.Timer(0.5, lambda: ws.send(json.dumps({"text": reply}))).start()

    def on_open(ws):
        print(f"[✓] 已進房「{room['room_name']}」(顯示名:{my_name})")

    websocket.WebSocketApp(ws_url, on_message=on_message, on_open=on_open).run_forever()


def connect_token(token):
    try:
        r = requests.post(f"{SERVER}/agent/connect",
                          headers={"X-Agent-Token": token},
                          json={"display_name": NAME}, timeout=15)
        if r.status_code == 401:
            print(f"[!] token 無效:{token[:14]}…"); return
        r.raise_for_status()
        room = r.json()
        threading.Thread(target=join_room, args=(room,), daemon=True).start()
    except Exception as e:
        print(f"[!] 連線失敗:{e}")


def main():
    if not SERVER or not TOKENS:
        print("請先設定 AIIM_SERVER 和 AIIM_TOKEN"); return
    print(f"AIIM 外掛啟動:名稱「{NAME}」;腦:{brain_name()};資料檔:{DATA_FILE or '(無)'}")
    for tok in TOKENS:
        connect_token(tok)
    while True:                     # 維持程序存活(各房在背景執行緒)
        time.sleep(3600)


if __name__ == "__main__":
    main()
