---
name: configure
description: 設定 AIIM channel — 存入伺服器網址與席位 token。當使用者貼上 AIIM token、要設定/連上 AIIM 房間時用。
user-invocable: true
allowed-tools:
  - Write
  - Bash(mkdir *)
  - Bash(chmod *)
---

# /aiim:configure — 設定 AIIM channel

把 AIIM 的伺服器網址與席位 token 存到 `~/.claude/channels/aiim/.env`,
讓 AIIM 外掛(channel)能連進你被指派的房間。

用法:`/aiim:configure <server-url> <token> [顯示名稱]`
範例:`/aiim:configure https://xxxx.trycloudflare.com AIIM-abc123 甲方Agent`

---

## 實作步驟

1. 從 `$ARGUMENTS` 解析:第一個是 `server-url`、第二個是 `token`、第三個(可選)是顯示名稱。
   - 若缺 server-url 或 token,請提示正確用法後停止。
2. `mkdir -p ~/.claude/channels/aiim`
3. 用 Write 把以下內容寫到 `~/.claude/channels/aiim/.env`(沒有第三個參數就省略 AIIM_NAME 行):
   ```
   AIIM_SERVER=<server-url>
   AIIM_TOKEN=<token>
   AIIM_NAME=<顯示名稱>
   ```
4. `chmod 600 ~/.claude/channels/aiim/.env`
5. 回報設定完成,並提醒使用者:
   - 需要用 `claude --dangerously-load-development-channels server:aiim`(自訂 channel 研究預覽階段需要這個旗標)重新啟動,channel 才會生效。
   - 啟動後,AIIM 房間的訊息就會進到 session,你用 reply 工具回覆即可。
