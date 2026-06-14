#!/usr/bin/env bun
/**
 * AIIM channel —— 把 Claude Code 變成 AIIM 房間裡的一個 agent。
 * 收訊:AIIM 房間的訊息 → notifications/claude/channel → 進到這個 Claude session。
 * 回覆:Claude 呼叫 reply 工具 → 透過 WebSocket 送回 AIIM 房間。
 * 設定來自 ~/.claude/channels/aiim/.env(由 /aiim:configure 寫入)。
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { readFileSync } from 'node:fs'

const STATE_DIR = process.env.AIIM_STATE_DIR ?? join(homedir(), '.claude', 'channels', 'aiim')
const ENV_FILE = join(STATE_DIR, '.env')
try {
  for (const line of readFileSync(ENV_FILE, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z_]+)\s*=\s*(.*?)\s*$/)
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2]
  }
} catch {}

const SERVER = (process.env.AIIM_SERVER ?? '').replace(/\/+$/, '')
const TOKEN = process.env.AIIM_TOKEN ?? ''
const NAME = process.env.AIIM_NAME ?? 'Claude'

if (!SERVER || !TOKEN) {
  process.stderr.write(
    `aiim channel: AIIM_SERVER 和 AIIM_TOKEN 必填\n  請在 ${ENV_FILE} 設定:\n` +
    `    AIIM_SERVER=https://你的-aiim-主機\n    AIIM_TOKEN=AIIM-...\n` +
    `  或用 /aiim:configure <server> <token> [name]\n`,
  )
  process.exit(1)
}

const mcp = new Server(
  { name: 'aiim', version: '0.0.1' },
  {
    capabilities: { tools: {}, experimental: { 'claude/channel': {} } },
    instructions: [
      '你正在一個 AIIM 多方協作聊天室裡擔任一個 agent,代表這位使用者。',
      '房間訊息會以 <channel source="aiim" room_id="..." user="..." ts="..."> 進來。',
      '對方讀的是 AIIM 房間,不是這個 transcript —— 你想讓他們看到的話,一定要用 reply 工具送出(你的一般輸出不會到房間)。',
      '回覆時把 room_id 帶回去,簡潔切題。用你本機掌握的資訊/資料來貢獻討論。不要回覆你自己發的訊息。',
    ].join('\n'),
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'reply',
    description: '在 AIIM 房間發言。把 inbound <channel> 區塊的 room_id 帶回來,text 放你要說的話。',
    inputSchema: {
      type: 'object',
      properties: { room_id: { type: 'string' }, text: { type: 'string' } },
      required: ['text'],
    },
  }],
}))

let ws: WebSocket | null = null
let myName = NAME

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  if (req.params.name === 'reply') {
    const text = String((req.params.arguments as Record<string, unknown> | undefined)?.text ?? '')
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ text }))
      return { content: [{ type: 'text', text: 'sent' }] }
    }
    return { content: [{ type: 'text', text: 'not connected to AIIM room' }] }
  }
  throw new Error(`unknown tool: ${req.params.name}`)
})

await mcp.connect(new StdioServerTransport())

async function connect() {
  let info: { ws_path: string; room_id: string; room_name: string; display_name: string }
  try {
    const r = await fetch(`${SERVER}/agent/connect`, {
      method: 'POST',
      headers: { 'X-Agent-Token': TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: NAME }),
    })
    if (!r.ok) { process.stderr.write(`aiim: 連線失敗 ${r.status}(token 對嗎?)\n`); return }
    info = await r.json()
  } catch (e) { process.stderr.write(`aiim: 連線錯誤 ${e}\n`); setTimeout(connect, 5000); return }

  myName = info.display_name ?? NAME
  const roomId = info.room_id
  const roomName = info.room_name
  const wsUrl = SERVER.replace(/^https/, 'wss').replace(/^http/, 'ws') + info.ws_path
  let live = false

  ws = new WebSocket(wsUrl)
  ws.onopen = () => process.stderr.write(`aiim: 已進房「${roomName}」,身分 ${myName}\n`)
  ws.onmessage = (evt: MessageEvent) => {
    let p: { type?: string; name?: string; text?: string; time?: string }
    try { p = JSON.parse(String(evt.data)) } catch { return }
    if (p.type === 'system') {
      if (typeof p.text === 'string' && p.text.includes(myName) && p.text.includes('已連線')) live = true
      return
    }
    if (p.type !== 'message') return
    if (!live || p.name === myName) return          // 跳過歷史與自己的話
    mcp.notification({
      method: 'notifications/claude/channel',
      params: {
        content: p.text ?? '',
        meta: { room_id: roomId, room_name: roomName, user: p.name ?? '?', ts: String(p.time ?? '') },
      },
    }).catch((err: unknown) => process.stderr.write(`aiim: 送進 session 失敗 ${err}\n`))
  }
  ws.onclose = () => { process.stderr.write('aiim: 斷線,5 秒後重連\n'); setTimeout(connect, 5000) }
  ws.onerror = (e: Event) => process.stderr.write(`aiim: ws 錯誤 ${e}\n`)
}
connect()
