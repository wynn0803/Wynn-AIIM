"""
AIIM v7 後端 — 「席位網址」大廳模型
============================================================
跟舊整合版最大的差別:

  舊：註冊→開群組→用『帳號』互相邀請→各取網址→進場
  v7：登入→建房間時直接設定『幾個真人席、幾個 Agent 席』
      → 每個席位當場產生一條【單次認領網址】
      → 真人席網址給人用瀏覽器點;Agent 席網址複製給 agent 連接器
      → 任何一條網址被成功認領後就鎖死,別人再用無效
      → 全部認領完,大家在同一間聊天室

保留:帳號密碼(認領時要登入,證明你是誰)、金鑰簽名(防冒用)、
      WebSocket 房間、歷史補送、回合護欄。

伺服器位置現在是這台 Mac;之後搬 AWS 只要換網址主機,程式不動。

需要:pip install "fastapi[standard]" uvicorn cryptography
執行:python server_v7.py
"""

import secrets
import hashlib
import time
import json
import asyncio
import httpx
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# 認領網址要塞哪個主機:預設用請求自己的主機(支援這台 Mac / 之後 AWS 都不用改)
# 仍保留可被環境覆寫的單一變數,給特殊情況用
import os
PUBLIC_BASE = os.environ.get("AIIM_PUBLIC_BASE", "")  # 空字串=用請求的主機

# ─────────────────────────────────────────────────────────────
# 共用資料(真實產品用資料庫)
# ─────────────────────────────────────────────────────────────
USERS = {}          # username -> {pw_hash, salt}
LOGIN_TOKENS = {}   # login_token -> username
ROOMS_DATA = {}     # room_id -> {name, owner, seats[], history[], settings{}}
CLAIM = {}          # claim_token -> {room_id, seat_id}
SESSIONS = {}       # session_token -> {room_id, seat_id, display_name}
RECONNECT = {}      # reconnect_token -> {...}
WS_ROOMS = {}       # room_id -> [websocket]
PENDING = {}        # claim_token -> {nonce, expires}
AGENTS = {}         # room_id -> [{seat_id, display_name, config}]  平台代接的 agent(跑在伺服器端)
# ── bot 不註冊帳號:每個 Agent 席建房時直接生一把 token,貼進外掛就連 ──
BOT_TOKENS = {}     # token_hash -> {room_id, seat_id}   每席一把、單次
# (下面這組是舊的「先註冊 agent」模型,保留不刪以免壞,UI 已不使用)
AGENT_IDS = {}
TOKEN_INDEX = {}
AGENT_ASSIGN = {}

CLAIM_TTL = 24 * 60 * 60   # 認領網址有效 24 小時


# ═════════════════════════════════════════════════════════════
# 帳號:密碼雜湊、註冊、登入(保留)
# ═════════════════════════════════════════════════════════════
def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return h, salt

def verify_password(password, h, salt):
    check, _ = hash_password(password, salt)
    return secrets.compare_digest(check, h)

def current_user(token):
    u = LOGIN_TOKENS.get(token)
    if not u:
        raise HTTPException(401, "請先登入")
    return u

class AuthReq(BaseModel):
    username: str
    password: str
    display_name: str = ""        # 顯示名稱(暱稱),登入不需要

@app.post("/register")
def register(req: AuthReq):
    if req.username in USERS:
        raise HTTPException(400, "這個帳號已經有人用了")
    if len(req.password) < 6:
        raise HTTPException(400, "密碼至少 6 個字")
    h, salt = hash_password(req.password)
    USERS[req.username] = {"pw_hash": h, "salt": salt,
                           "display": (req.display_name or "").strip() or req.username}
    return {"ok": True, "msg": f"帳號 {req.username} 註冊成功"}

@app.post("/login")
def login(req: AuthReq):
    u = USERS.get(req.username)
    if not u or not verify_password(req.password, u["pw_hash"], u["salt"]):
        raise HTTPException(401, "帳號或密碼錯誤")
    token = secrets.token_urlsafe(24)
    LOGIN_TOKENS[token] = req.username
    return {"ok": True, "login_token": token, "username": req.username,
            "display_name": u.get("display", req.username)}


# ═════════════════════════════════════════════════════════════
# 建房間:設定『幾個真人席、幾個 Agent 席』,每席產生單次認領網址
# ═════════════════════════════════════════════════════════════
class CreateRoomReq(BaseModel):
    name: str
    num_humans: int          # 真人席數量
    num_agents: int          # Agent 席數量
    max_turns: int = 50
    cost_budget: float = 2.00
    auto_rounds: int = 6     # 每次有人發言後,agent 們最多自動接幾句(防爆量/防無限迴圈)

def _new_seat(kind):
    tok = secrets.token_urlsafe(18)
    return {"seat_id": "seat_" + secrets.token_hex(4),
            "kind": kind,                 # "human" | "agent"
            "claim_token": tok,
            "bot_token": ("AIIM-" + secrets.token_urlsafe(24)) if kind == "agent" else None,
            "claimed_by": None,           # 認領者帳號 / agent 顯示名
            "display_name": None,
            "agent_pubkey": None,
            "used": False,
            "created": time.time()}

@app.post("/create-room")
def create_room(req: CreateRoomReq, authorization: str = Header(None)):
    owner = current_user(authorization)
    total = req.num_humans + req.num_agents
    if total < 1 or total > 50:
        raise HTTPException(400, "席位總數請在 1 到 50 之間")
    if req.num_humans < 0 or req.num_agents < 0:
        raise HTTPException(400, "人數不能是負的")
    rid = "room_" + secrets.token_hex(6)
    seats = ([_new_seat("human") for _ in range(req.num_humans)] +
             [_new_seat("agent") for _ in range(req.num_agents)])
    for s in seats:
        CLAIM[s["claim_token"]] = {"room_id": rid, "seat_id": s["seat_id"]}
        if s["kind"] == "agent":          # 每個 Agent 席當場登記一把 token
            BOT_TOKENS[_hash_token(s["bot_token"])] = {"room_id": rid, "seat_id": s["seat_id"]}
    # 房主建房即入座(佔第一個真人席)→ 之後點房間直接回聊天,不必再認領
    h0 = next((s for s in seats if s["kind"] == "human"), None)
    if h0:
        h0["used"] = True; h0["claimed_by"] = owner
        h0["display_name"] = USERS.get(owner, {}).get("display", owner)   # 房主用暱稱顯示
    ROOMS_DATA[rid] = {"name": req.name, "owner": owner, "seats": seats, "history": [],
                       "auto_left": req.auto_rounds,   # 初始就給預算 → agent 能先打招呼
                       "settings": {"max_turns": req.max_turns, "cost_budget": req.cost_budget,
                                    "auto_rounds": req.auto_rounds}}
    return {"ok": True, "room_id": rid, "name": req.name,
            "num_humans": req.num_humans, "num_agents": req.num_agents}


def _seat_view(s, base):
    n_human = None
    view = {"seat_id": s["seat_id"], "kind": s["kind"],
            "claim_url": f"{base}/?claim={s['claim_token']}",
            "claim_token": s["claim_token"],
            "claimed_by": s["claimed_by"], "display_name": s["display_name"],
            "used": s["used"]}
    if s["kind"] == "agent":
        view["bot_token"] = s.get("bot_token")     # 房主才看得到(此函式只給房主用)
    return view


# ═════════════════════════════════════════════════════════════
# Agent 身分 + Token(像 Telegram BotFather):外掛靠 token 自動進房
# ═════════════════════════════════════════════════════════════
def _hash_token(tok):
    return hashlib.sha256((tok or "").encode()).hexdigest()

class CreateAgentReq(BaseModel):
    name: str

@app.post("/create-agent")
def create_agent(req: CreateAgentReq, authorization: str = Header(None)):
    owner = current_user(authorization)
    aid = "agt_" + secrets.token_hex(5)
    token = "AIIM-" + secrets.token_urlsafe(24)
    th = _hash_token(token)
    AGENT_IDS[aid] = {"owner": owner, "name": req.name, "token_hash": th}
    TOKEN_INDEX[th] = aid
    return {"ok": True, "agent_id": aid, "name": req.name, "token": token,
            "note": "這把 token 只顯示這一次,貼進你的 AIIM 外掛即可"}

@app.get("/my-agents")
def my_agents_list(authorization: str = Header(None)):
    me = current_user(authorization)
    return {"agents": [{"agent_id": aid, "name": a["name"]}
                       for aid, a in AGENT_IDS.items() if a["owner"] == me]}

class AssignReq(BaseModel):
    room_id: str
    seat_id: str
    agent_id: str

@app.post("/assign-agent")
def assign_agent_to_seat(req: AssignReq, authorization: str = Header(None)):
    me = current_user(authorization)
    r = ROOMS_DATA.get(req.room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    if r["owner"] != me:
        raise HTTPException(403, "只有房主能指派 agent")
    a = AGENT_IDS.get(req.agent_id)
    if not a or a["owner"] != me:
        raise HTTPException(404, "找不到你的這個 agent")
    seat = next((s for s in r["seats"] if s["seat_id"] == req.seat_id), None)
    if not seat:
        raise HTTPException(404, "找不到席位")
    if seat["kind"] != "agent":
        raise HTTPException(400, "只有 Agent 席能指派 agent")
    if seat["used"]:
        raise HTTPException(403, "這個席位已被佔用")
    seat["used"] = True
    seat["claimed_by"] = a["name"]
    seat["display_name"] = a["name"]
    AGENT_ASSIGN.setdefault(req.agent_id, []).append(
        {"room_id": req.room_id, "seat_id": req.seat_id})
    return {"ok": True, "msg": f"已把「{a['name']}」指派到這個席位,啟動它的外掛就會自動進房"}

class ClaimBotReq(BaseModel):
    room_id: str

@app.post("/claim-bot-token")
def claim_bot_token(req: ClaimBotReq, authorization: str = Header(None)):
    """房內成員自己取一把 Agent 席 token(不必房主發)。單次:取走後別人拿不到。"""
    me = current_user(authorization)
    r = ROOMS_DATA.get(req.room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    is_member = (r["owner"] == me) or any(s["claimed_by"] == me for s in r["seats"])
    if not is_member:
        raise HTTPException(403, "你不在這個房間,先進場才能取 agent token")
    seat = next((s for s in r["seats"] if s["kind"] == "agent" and not s["used"]), None)
    if not seat:
        raise HTTPException(400, "沒有空的 Agent 席了")
    seat["used"] = True
    seat["claimed_by"] = f"{me} 的 agent"
    return {"ok": True, "seat_id": seat["seat_id"], "bot_token": seat["bot_token"],
            "msg": "這把 token 只顯示一次,複製貼進你的 AIIM 外掛"}

class ReenterReq(BaseModel):
    room_id: str

@app.post("/reenter")
def reenter_room(req: ReenterReq, authorization: str = Header(None)):
    """已在房內有席位的人重新進聊天:沿用原席位,不佔新位。"""
    me = current_user(authorization)
    r = ROOMS_DATA.get(req.room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    seat = next((s for s in r["seats"] if s["kind"] == "human" and s["claimed_by"] == me), None)
    if not seat:
        raise HTTPException(404, "你在這間房還沒有席位")
    session = secrets.token_urlsafe(24)
    SESSIONS[session] = {"room_id": req.room_id, "seat_id": seat["seat_id"],
                         "display_name": seat["display_name"] or me}
    return {"ok": True, "ws_path": f"/ws/{session}", "room_name": r["name"]}

class RenameReq(BaseModel):
    room_id: str
    name: str

@app.post("/rename-room")
def rename_room(req: RenameReq, authorization: str = Header(None)):
    me = current_user(authorization)
    r = ROOMS_DATA.get(req.room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    if r["owner"] != me:
        raise HTTPException(403, "只有房主能改名")
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "房間名稱不能空白")
    r["name"] = name
    return {"ok": True, "name": name}

class RoomRefReq(BaseModel):
    room_id: str

@app.post("/delete-room")
def delete_room(req: RoomRefReq, authorization: str = Header(None)):
    me = current_user(authorization)
    r = ROOMS_DATA.get(req.room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    if r["owner"] != me:
        raise HTTPException(403, "只有房主能刪除房間")
    for s in r["seats"]:                       # 清掉這間房的票券與 token
        CLAIM.pop(s["claim_token"], None)
        if s.get("bot_token"):
            BOT_TOKENS.pop(_hash_token(s["bot_token"]), None)
    AGENTS.pop(req.room_id, None)
    WS_ROOMS.pop(req.room_id, None)
    for k in [k for k, v in SESSIONS.items() if v.get("room_id") == req.room_id]:
        SESSIONS.pop(k, None)
    ROOMS_DATA.pop(req.room_id, None)
    return {"ok": True}

class RenameAgentReq(BaseModel):
    room_id: str
    seat_id: str
    name: str

@app.post("/rename-agent")
def rename_agent(req: RenameAgentReq, authorization: str = Header(None)):
    """房主隨時替某個 agent 席改顯示名。"""
    me = current_user(authorization)
    r = ROOMS_DATA.get(req.room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    if r["owner"] != me:
        raise HTTPException(403, "只有房主能改 agent 名")
    seat = next((s for s in r["seats"] if s["seat_id"] == req.seat_id and s["kind"] == "agent"), None)
    if not seat:
        raise HTTPException(404, "找不到這個 Agent 席")
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "名稱不能空白")
    seat["display_name"] = name
    if seat.get("claimed_by"):
        seat["claimed_by"] = name
    return {"ok": True, "name": name}

class AutoRoundsReq(BaseModel):
    room_id: str
    auto_rounds: int            # -1 = 無限

@app.post("/set-auto-rounds")
def set_auto_rounds(req: AutoRoundsReq, authorization: str = Header(None)):
    me = current_user(authorization)
    r = ROOMS_DATA.get(req.room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    if r["owner"] != me:
        raise HTTPException(403, "只有房主能改設定")
    r["settings"]["auto_rounds"] = req.auto_rounds
    if req.auto_rounds >= 0:
        r["auto_left"] = req.auto_rounds
    return {"ok": True, "auto_rounds": req.auto_rounds}

@app.get("/room/{room_id}/members")
def room_members(room_id: str, authorization: str = Header(None)):
    """房內成員(給 @ 提及用)。任何成員可取。"""
    me = current_user(authorization)
    r = ROOMS_DATA.get(room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    if r["owner"] != me and not any(s["claimed_by"] == me for s in r["seats"]):
        raise HTTPException(403, "你不在這間房")
    out = [{"name": s["display_name"] or s["claimed_by"], "kind": s["kind"]}
           for s in r["seats"] if s["used"] and (s["display_name"] or s["claimed_by"])]
    return {"members": out}

# 純 HTTP 收發(給零依賴橋接用:agent 用 token 輪詢收訊、發言)
class AgentPollReq(BaseModel):
    since: int = 0
    name: str = "Agent"

@app.post("/agent/poll")
def agent_poll(req: AgentPollReq, x_agent_token: str = Header(None)):
    bt = BOT_TOKENS.get(_hash_token(x_agent_token))
    if not bt:
        raise HTTPException(401, "token 無效")
    room = ROOMS_DATA.get(bt["room_id"])
    if not room:
        raise HTTPException(404, "房間不存在")
    seat = next((s for s in room["seats"] if s["seat_id"] == bt["seat_id"]), None)
    if seat and not seat["used"]:
        seat["used"] = True; seat["claimed_by"] = req.name; seat["display_name"] = req.name
    self_name = (seat.get("display_name") if seat else None) or req.name   # 以席位現名為準(被改名也對)
    hist = [m for m in room["history"] if m.get("type") == "message"]
    new = [{"name": m["name"], "text": m["text"]} for m in hist[req.since:] if m["name"] != self_name]
    return {"room_name": room["name"], "messages": new, "next": len(hist), "my_name": self_name}

class AgentSayReq(BaseModel):
    text: str
    name: str = "Agent"

@app.post("/agent/say")
async def agent_say_http(req: AgentSayReq, x_agent_token: str = Header(None)):
    bt = BOT_TOKENS.get(_hash_token(x_agent_token))
    if not bt:
        raise HTTPException(401, "token 無效")
    room = ROOMS_DATA.get(bt["room_id"])
    if not room:
        raise HTTPException(404, "房間不存在")
    seat = next((s for s in room["seats"] if s["seat_id"] == bt["seat_id"]), None)
    if seat and not seat["used"]:
        seat["used"] = True; seat["claimed_by"] = req.name; seat["display_name"] = req.name
    name = (seat.get("display_name") if seat else None) or req.name   # 房主可改的席位名優先
    ar = room["settings"].get("auto_rounds", 6)
    if ar >= 0:                              # ar < 0 = 無限,不擋
        if room.get("auto_left", 0) <= 0:
            return {"ok": False, "dropped": True, "note": "等真人發言後才能再說"}
        room["auto_left"] = room.get("auto_left", 0) - 1
    msg = {"type": "message", "name": name, "text": req.text,
           "time": datetime.now().strftime("%m-%d %H:%M")}
    room["history"].append(msg)
    await broadcast(bt["room_id"], msg)
    return {"ok": True}

class AgentConnectReq(BaseModel):
    display_name: str = "Agent"

@app.post("/agent/connect")
def agent_connect(req: AgentConnectReq, x_agent_token: str = Header(None)):
    """外掛拿『席位 token』來連:解析到那一間房/席位,開一個 WebSocket 入口。"""
    bt = BOT_TOKENS.get(_hash_token(x_agent_token))
    if not bt:
        raise HTTPException(401, "token 無效")
    r = ROOMS_DATA.get(bt["room_id"])
    if not r:
        raise HTTPException(404, "房間已不存在")
    seat = next((s for s in r["seats"] if s["seat_id"] == bt["seat_id"]), None)
    if not seat:
        raise HTTPException(404, "席位不存在")
    seat["used"] = True                         # 標記已接入(單次:此 token 已被某 agent 拿去用)
    seat["claimed_by"] = req.display_name
    seat["display_name"] = req.display_name
    session = secrets.token_urlsafe(24)
    SESSIONS[session] = {"room_id": bt["room_id"], "seat_id": bt["seat_id"],
                         "display_name": req.display_name}
    return {"ok": True, "room_id": bt["room_id"], "room_name": r["name"],
            "display_name": req.display_name, "ws_path": f"/ws/{session}"}

@app.post("/agent/sessions")
def agent_sessions(x_agent_token: str = Header(None)):
    """外掛拿 token 來問:我被指派到哪些房間?回傳每間的 WebSocket 入口。"""
    th = _hash_token(x_agent_token)
    aid = TOKEN_INDEX.get(th)
    if not aid:
        raise HTTPException(401, "token 無效")
    out = []
    for asg in AGENT_ASSIGN.get(aid, []):
        r = ROOMS_DATA.get(asg["room_id"])
        if not r:
            continue
        seat = next((s for s in r["seats"] if s["seat_id"] == asg["seat_id"]), None)
        if not seat:
            continue
        name = seat["display_name"] or AGENT_IDS[aid]["name"]
        session = secrets.token_urlsafe(24)
        SESSIONS[session] = {"room_id": asg["room_id"], "seat_id": asg["seat_id"],
                             "display_name": name}
        out.append({"room_id": asg["room_id"], "room_name": r["name"],
                    "display_name": name, "ws_path": f"/ws/{session}"})
    return {"agent_id": aid, "name": AGENT_IDS[aid]["name"], "rooms": out}

def _base_from(authorization, host_header, scheme="https"):
    if PUBLIC_BASE:
        return PUBLIC_BASE.rstrip("/")
    # 用請求進來的主機組網址(這台 Mac、之後 AWS 都通用)
    if host_header:
        # cloudflare/https 一律當 https;本機 localhost 用 http
        proto = "http" if host_header.startswith(("localhost", "127.0.0.1")) else "https"
        return f"{proto}://{host_header}"
    return "http://localhost:8000"

@app.get("/room/{room_id}/seats")
def room_seats(room_id: str, authorization: str = Header(None), host: str = Header(None)):
    me = current_user(authorization)
    r = ROOMS_DATA.get(room_id)
    if not r:
        raise HTTPException(404, "找不到房間")
    if r["owner"] != me:
        raise HTTPException(403, "只有房主能看所有席位網址")
    base = _base_from(authorization, host)
    humans = [_seat_view(s, base) for s in r["seats"] if s["kind"] == "human"]
    agents = [_seat_view(s, base) for s in r["seats"] if s["kind"] == "agent"]
    return {"room_id": room_id, "name": r["name"], "owner": r["owner"],
            "auto_rounds": r["settings"]["auto_rounds"],
            "human_seats": humans, "agent_seats": agents}


@app.get("/my-rooms")
def my_rooms(authorization: str = Header(None)):
    me = current_user(authorization)
    owned, joined = [], []
    for rid, r in ROOMS_DATA.items():
        claimed_here = any(s["claimed_by"] == me for s in r["seats"])
        filled = sum(1 for s in r["seats"] if s["used"])
        item = {"room_id": rid, "name": r["name"],
                "filled": filled, "total": len(r["seats"]),
                "is_owner": r["owner"] == me}
        if r["owner"] == me:
            owned.append(item)
        elif claimed_here:
            joined.append(item)
    return {"username": me, "owned": owned, "joined": joined,
            "total": len(owned) + len(joined)}


# 查一條認領網址現在的狀態(給認領頁用,進場前先知道是真人席還 agent 席、是否已被佔)
@app.get("/claim-info")
def claim_info(claim_token: str):
    c = CLAIM.get(claim_token)
    if not c:
        raise HTTPException(404, "這條網址無效")
    r = ROOMS_DATA[c["room_id"]]
    seat = next(s for s in r["seats"] if s["seat_id"] == c["seat_id"])
    return {"room_id": c["room_id"], "room_name": r["name"],
            "kind": seat["kind"], "used": seat["used"],
            "claimed_by": seat["claimed_by"],
            "bot_token": seat.get("bot_token") if seat["kind"] == "agent" else None}


# ═════════════════════════════════════════════════════════════
# 認領進場:登入 + 金鑰簽名;成功後把這條網址鎖死(單次)
# ═════════════════════════════════════════════════════════════
@app.post("/enter-challenge")
def enter_challenge(claim_token: str):
    if claim_token not in CLAIM:
        raise HTTPException(404, "這條網址無效")
    nonce = secrets.token_hex(16)
    PENDING[claim_token] = {"nonce": nonce, "expires": time.time() + 30}
    return {"challenge": nonce}

class ClaimReq(BaseModel):
    claim_token: str
    display_name: str
    agent_pubkey_hex: str
    challenge: str
    signature_hex: str

@app.post("/claim")
def claim(req: ClaimReq, authorization: str = Header(None)):
    me = current_user(authorization)            # 認領要登入(證明你是誰)
    c = CLAIM.get(req.claim_token)
    if not c:
        raise HTTPException(404, "這條網址無效")
    r = ROOMS_DATA[c["room_id"]]
    seat = next(s for s in r["seats"] if s["seat_id"] == c["seat_id"])

    if seat["used"]:
        raise HTTPException(403, f"這個席位已被 {seat['claimed_by']} 認領,網址已失效(單次)")
    ch = PENDING.get(req.claim_token)
    if not ch or req.challenge != ch["nonce"]:
        raise HTTPException(400, "請先索取挑戰題目")
    if time.time() > ch["expires"]:
        raise HTTPException(400, "挑戰題目已過期,請重新進場")

    # 金鑰簽名驗證(防冒用)
    try:
        pub = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), bytes.fromhex(req.agent_pubkey_hex))
        raw = bytes.fromhex(req.signature_hex)
        der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"),
                                         int.from_bytes(raw[32:], "big"))
        pub.verify(der, req.challenge.encode(), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        raise HTTPException(403, "金鑰簽名驗證失敗")

    # 鎖死這個席位 + 這條網址
    seat["used"] = True
    seat["claimed_by"] = me
    seat["display_name"] = req.display_name
    seat["agent_pubkey"] = req.agent_pubkey_hex
    PENDING.pop(req.claim_token, None)

    session = secrets.token_urlsafe(24)
    reconnect = secrets.token_urlsafe(24)
    payload = {"room_id": c["room_id"], "seat_id": seat["seat_id"],
               "display_name": req.display_name}
    SESSIONS[session] = dict(payload)
    RECONNECT[reconnect] = dict(payload)
    return {"ok": True, "msg": f"{req.display_name} 已認領 {seat['kind']} 席位並進場",
            "kind": seat["kind"], "room_id": c["room_id"],
            "session_token": session, "reconnect_token": reconnect,
            "ws_path": f"/ws/{session}"}

class ReconnectReq(BaseModel):
    reconnect_token: str

@app.post("/reconnect")
def reconnect(req: ReconnectReq):
    rc = RECONNECT.get(req.reconnect_token)
    if not rc:
        raise HTTPException(404, "重連憑證無效")
    session = secrets.token_urlsafe(24)
    SESSIONS[session] = dict(rc)
    return {"ok": True, "msg": f"{rc['display_name']} 已重新接回",
            "session_token": session, "ws_path": f"/ws/{session}"}


# ═════════════════════════════════════════════════════════════
# 平台代接 agent:別人在網頁表單填好「他的 agent 在哪」,
# 平台就在伺服器端替他把 agent 接進房間、自動收發(對方不用裝程式)
# ═════════════════════════════════════════════════════════════
def _dig(data, path):
    cur = data
    for part in path.split("."):
        cur = cur[int(part)] if part.isdigit() else cur[part]
    return cur

def _set_nested(obj, path, value):
    parts = path.split("."); cur = obj
    for i, part in enumerate(parts[:-1]):
        nxt = parts[i + 1]; key = int(part) if part.isdigit() else part
        if isinstance(key, int):
            while len(cur) <= key: cur.append({} if not nxt.isdigit() else [])
            cur = cur[key]
        else:
            if key not in cur: cur[key] = [] if nxt.isdigit() else {}
            cur = cur[key]
    last = parts[-1]; cur[int(last) if last.isdigit() else last] = value

async def call_agent(cfg, text, display_name):
    """把一句話交給接入者的 agent,拿回它的回覆。各家差異都在 cfg 裡。"""
    preset = cfg.get("preset", "echo")
    if preset == "echo":
        return f"(自動回覆)我是 {display_name},收到:「{text}」"
    timeout = cfg.get("timeout_seconds", 30)
    sysmsg = cfg.get("system", f"你正在一個多方協作聊天室裡,以「{display_name}」的身分參與討論。請簡潔、切題地回覆。")
    async with httpx.AsyncClient(timeout=timeout) as client:
        if preset == "compat":          # OpenAI 相容端點:使用者只貼自己的網址(+金鑰)
            headers = {"Content-Type": "application/json"}
            if cfg.get("api_key"):
                headers["Authorization"] = f"Bearer {cfg['api_key']}"
            r = await client.post(cfg["url"], headers=headers,
                json={"model": cfg.get("model") or "gpt-4o-mini",
                      "messages": [{"role": "system", "content": sysmsg},
                                   {"role": "user", "content": text}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        if preset == "openai":
            r = await client.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json={"model": cfg.get("model", "gpt-4o-mini"),
                      "messages": [{"role": "system", "content": sysmsg},
                                   {"role": "user", "content": text}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        if preset == "claude":
            r = await client.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": cfg.get("model", "claude-haiku-4-5-20251001"), "max_tokens": 1024,
                      "system": sysmsg, "messages": [{"role": "user", "content": text}]})
            r.raise_for_status()
            return r.json()["content"][0]["text"]
        if preset == "custom":
            req = cfg["request"]
            body = json.loads(json.dumps(req["body_template"]))
            _set_nested(body, req["message_field"], text)
            r = await client.request(req.get("method", "POST"), req["url"],
                                     headers=dict(req.get("headers", {})), json=body)
            r.raise_for_status()
            return _dig(r.json(), cfg["response"]["reply_field"])
    raise ValueError(f"未知 preset:{preset}")

class AttachReq(BaseModel):
    claim_token: str
    display_name: str
    agent: dict          # {preset, api_key?, model?, request?, response?}

@app.post("/attach-agent")
async def attach_agent(req: AttachReq, authorization: str = Header(None)):
    me = current_user(authorization)
    c = CLAIM.get(req.claim_token)
    if not c:
        raise HTTPException(404, "這條網址無效")
    r = ROOMS_DATA[c["room_id"]]
    seat = next(s for s in r["seats"] if s["seat_id"] == c["seat_id"])
    if seat["kind"] != "agent":
        raise HTTPException(400, "這是真人席,請用瀏覽器進場;Agent 席才用接入表單")
    if seat["used"]:
        raise HTTPException(403, f"這個席位已被 {seat['claimed_by']} 接入,網址已失效(單次)")
    # 防呆:非測試模式,先實際呼叫一次,確認真的連得到、key/格式對
    if req.agent.get("preset", "echo") != "echo":
        try:
            probe = await call_agent(req.agent, "這是一則接入連線測試,請簡短回覆。", req.display_name)
            if not isinstance(probe, str) or not probe.strip():
                raise ValueError("agent 回了空內容,檢查欄位設定")
        except Exception as e:
            raise HTTPException(400, f"接入測試失敗:{e}")
    # 鎖死席位 + 登記到伺服器端 agent 清單
    seat["used"] = True
    seat["claimed_by"] = me
    seat["display_name"] = req.display_name
    AGENTS.setdefault(c["room_id"], []).append(
        {"seat_id": seat["seat_id"], "display_name": req.display_name, "config": req.agent})
    await broadcast(c["room_id"], {"type": "system", "text": f"{req.display_name}(Agent)已接入"})
    return {"ok": True, "room_id": c["room_id"],
            "msg": f"{req.display_name} 已接入房間,開始自動參與對話"}

def build_context(room_id, limit=12):
    """把最近的對話組成脈絡文字(發言者: 內容),讓 agent 知道前因後果。"""
    hist = [m for m in ROOMS_DATA[room_id]["history"] if m.get("type") == "message"][-limit:]
    return "\n".join(f'{m["name"]}: {m["text"]}' for m in hist)

async def agent_say(room_id, name, text):
    msg = {"type": "message", "name": name, "text": text,
           "time": datetime.now().strftime("%m-%d %H:%M")}
    ROOMS_DATA[room_id]["history"].append(msg)
    await broadcast(room_id, msg)
    # agent 的發言也可能引出其他 agent 回應(agent 之間自動往返),受 auto_left 預算約束
    asyncio.create_task(trigger_agents(room_id, msg))

async def trigger_agents(room_id, msg):
    """有人或別的 agent 說話時,讓房裡『其他』agent 帶著脈絡回應。
    auto_left 預算約束 agent 間自動往返,避免無限迴圈與爆量花費;有人發言時會重置。"""
    room = ROOMS_DATA.get(room_id)
    if not room:
        return
    agents = AGENTS.get(room_id, [])
    speaker = msg["name"]
    responders = [a for a in agents if a["display_name"] != speaker]   # 不回自己
    if not responders:
        return
    settings = room["settings"]
    transcript = build_context(room_id)
    for a in responders:
        ar = settings.get("auto_rounds", 6)
        if ar >= 0 and room.get("auto_left", 0) <= 0:   # ar<0=無限,不擋
            return
        msg_count = sum(1 for m in room["history"] if m.get("type") == "message")
        if msg_count >= settings["max_turns"]:      # 總訊息護欄
            return
        if ar >= 0:
            room["auto_left"] = room.get("auto_left", 0) - 1
        prompt = (f"以下是多方協作聊天室「{room['name']}」的最近對話:\n{transcript}\n\n"
                  f"你是其中的「{a['display_name']}」。請根據你掌握的資料,自然接續討論、"
                  f"回應最新發言;只需回覆你要說的話,不必加說明。")
        try:
            reply = await call_agent(a["config"], prompt, a["display_name"])
        except Exception:
            reply = f"[系統提示] {a['display_name']} 暫時無法回覆"
        await agent_say(room_id, a["display_name"], reply)


# ═════════════════════════════════════════════════════════════
# 群組對話:WebSocket、歷史補送、回合護欄(保留)
# ═════════════════════════════════════════════════════════════
@app.websocket("/ws/{session_token}")
async def ws(websocket: WebSocket, session_token: str):
    sess = SESSIONS.get(session_token)
    if not sess:
        await websocket.close(code=4001)
        return
    rid = sess["room_id"]
    name = sess["display_name"]
    seat = next((s for s in ROOMS_DATA[rid]["seats"] if s["seat_id"] == sess["seat_id"]), None)
    is_agent = bool(seat and seat["kind"] == "agent")   # 發話者是不是 agent
    await websocket.accept()
    WS_ROOMS.setdefault(rid, []).append(websocket)
    for past in ROOMS_DATA[rid]["history"]:
        await websocket.send_text(json.dumps(past, ensure_ascii=False))
    await broadcast(rid, {"type": "system", "text": f"{name} 已連線"})
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            room = ROOMS_DATA[rid]
            settings = room["settings"]
            msg_count = sum(1 for m in room["history"] if m.get("type") == "message")
            if msg_count >= settings["max_turns"]:
                await websocket.send_text(json.dumps(
                    {"type": "system", "text": f"已達回合上限 {settings['max_turns']}"},
                    ensure_ascii=False))
                continue
            # 自動往返預算:真人發言重置;agent 發言扣;扣完就不再轉發 agent 的話,等真人說話
            if is_agent:
                ar = settings.get("auto_rounds", 6)
                if ar >= 0:                  # ar < 0 = 無限
                    if room.get("auto_left", 0) <= 0:
                        continue
                    room["auto_left"] = room.get("auto_left", 0) - 1
            else:
                room["auto_left"] = settings.get("auto_rounds", 6)
            msg = {"type": "message", "name": name, "text": data.get("text", ""),
                   "time": datetime.now().strftime("%m-%d %H:%M")}
            room["history"].append(msg)
            await broadcast(rid, msg)
            if not is_agent:                       # 真人發言才另外觸發「伺服器端代接」的 agent
                asyncio.create_task(trigger_agents(rid, msg))
    except WebSocketDisconnect:
        if websocket in WS_ROOMS.get(rid, []):
            WS_ROOMS[rid].remove(websocket)
        await broadcast(rid, {"type": "system", "text": f"{name} 已斷線(可重連接回)"})

async def broadcast(rid, packet):
    for w in list(WS_ROOMS.get(rid, [])):
        try:
            await w.send_text(json.dumps(packet, ensure_ascii=False))
        except Exception:
            pass


@app.get("/health")
def health():
    return {"service": "AIIM v7", "status": "running",
            "flow": "login→create-room(num_humans,num_agents)→seat claim-urls→claim→ws"}


# 由同一個 server 提供前端網頁(認領網址 base/?claim=... 才載得到頁面)
from fastapi.responses import HTMLResponse
_FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "frontend", "index.html")

@app.get("/", response_class=HTMLResponse)
def index():
    with open(_FRONTEND, encoding="utf-8") as f:
        return f.read()


# 讓使用者的 agent 能自己下載外掛(配合「給 agent 的設定 prompt」)
from fastapi.responses import FileResponse
_V7 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOWNLOADS = {
    "aiim-plugin.zip": os.path.join(_V7, "aiim-plugin.zip"),          # Claude Code channel 外掛
    "aiim_plugin.py": os.path.join(_V7, "agent", "aiim_plugin.py"),   # 獨立外掛(單檔)
    "aiim_bridge.py": os.path.join(_V7, "agent", "aiim_bridge.py"),   # 純標準庫橋接(agent 自連用)
}

@app.get("/download/{fname}")
def download(fname: str):
    path = _DOWNLOADS.get(fname)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "檔案不存在")
    return FileResponse(path, filename=fname)


# 給 agent 自己抓的「機器可讀安裝指示」。使用者只要叫 agent 讀這個網址、照做即可。
from fastapi.responses import PlainTextResponse

@app.get("/connect", response_class=PlainTextResponse)
def connect_instructions(token: str = "", name: str = "我的Agent", host: str = Header(None)):
    base = _base_from(None, host)
    tok = token or "<向使用者索取 AIIM token>"
    return f"""# 給 AI agent:用 token 連進 AIIM 協作聊天室,並開始即時對話
# (不需要任何特殊啟動旗標、不需要 pip 安裝、不需要 API 金鑰——你自己就是腦)

伺服器:{base}
你的 token:{tok}
你在房裡顯示的名字:{name}

請照做:

1) 下載橋接(純 Python 標準庫;原始碼公開可審,執行前可先看 https://github.com/wynn0803/Wynn-AIIM):
   curl -L https://raw.githubusercontent.com/wynn0803/Wynn-AIIM/main/agent/aiim_bridge.py -o /tmp/aiim_bridge.py

2) 設好環境變數,並打招呼(會回報你進的房名):
   export AIIM_SERVER={base}
   export AIIM_TOKEN={tok}
   export AIIM_NAME={name}
   python3 /tmp/aiim_bridge.py hello

3) 進入「對話迴圈」——這就是你待在房裡跟大家即時對話。持續重複:
   a. 收下一則別人的發言(會阻塞等到有人說話,最多約 120 秒):
        python3 /tmp/aiim_bridge.py recv
   b. 用你自己的判斷想好回覆(需要的話用你本機讀得到的資料/工具),然後送出:
        python3 /tmp/aiim_bridge.py send "你的回覆"
   c. 回到 a。
   - 若 recv 印出「no new message」,就再執行一次 recv 繼續等。
   - 只要你持續這個 recv → 想 → send 迴圈,使用者在房裡打字你就會即時收到並回覆,
     完全不需要使用者來提醒你。要離開房間就停止迴圈。

請現在就開始:做 1、2,然後進入 3 的迴圈,先 recv 等使用者說話。"""


if __name__ == "__main__":
    import uvicorn
    print("AIIM v7 後端啟動中… 大廳模型:設定真人/Agent 席 → 單次認領網址 → 同房")
    uvicorn.run(app, host="0.0.0.0", port=8000)
