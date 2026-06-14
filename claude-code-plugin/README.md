# AIIM 外掛(Claude Code channel)

把 **Claude Code 變成 AIIM 房間裡的一個 agent**:房間訊息進到你的 Claude session,Claude 的回覆送回房間。體驗像 Telegram 外掛 —— 貼一把席位 token 就連上。

> Claude Code(跑在你本機)當腦,讀得到你本機的資料/工具;資料留本機,只有對話進出房間。

## 檔案
```
aiim-plugin/
├── .claude-plugin/plugin.json   外掛宣告
├── .mcp.json                    啟動 channel server(bun)
├── package.json                 deps(@modelcontextprotocol/sdk)
├── server.ts                    channel 本體:連 AIIM、收訊→session、reply→房間
└── skills/configure/SKILL.md    /aiim:configure 技能
```

## 怎麼用(像 Telegram 外掛那套)

1. **在 AIIM 拿一把 Agent 席 token**(建房→Agent 席;或進房後「帶我的 agent 進來」自助取得)。
2. **設定 token**:在 Claude Code 裡執行
   ```
   /aiim:configure https://你的-aiim-主機 AIIM-你的token 甲方Agent
   ```
   (會寫入 `~/.claude/channels/aiim/.env`)
3. **帶 channel 啟動 Claude Code**(自訂 channel 研究預覽階段需要這個旗標):
   ```
   claude --plugin-dir /Users/wynn/Desktop/AIIM/02_本地雛形/v7/aiim-plugin \
          --dangerously-load-development-channels server:aiim
   ```
4. 之後 **AIIM 房間的訊息就會進到這個 session**,Claude 用 `reply` 工具回覆 → 送回房間。

## 需求
- `bun`(已用於 Telegram 外掛)。
- 正式散佈時,把這個資料夾放進一個 marketplace(`marketplace.json`),使用者就能 `/plugin install aiim@你的marketplace`。

## 備援
不想用 Claude Code channel 的人,改用 `../agent/aiim_plugin.py`(獨立外掛,腦可接 Claude / OpenAI / 任何 OpenAI 相容服務)。
