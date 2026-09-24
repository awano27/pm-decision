# PoC: reading Teams via UI Automation (2026-09-23)

Goal: ingest Teams chats with zero admin consent, no Graph API, no Power Automate.

## Setup
- sbroenne/mcp-windows v1.3.24 standalone (`windows-mcp-server-1.3.24-win-x64.zip`, SHA256 verified against `SHA256SUMS.txt`)
- Driven over MCP stdio (`ui_snapshot`, `window_management`); read-only tools only
- New Teams (MSTeams 25306.804), Windows 11, x64

## Results (structure only; no message text recorded here)

| State | Success | Elements | Time | Content identical to normal |
|---|---|---|---|---|
| Normal (foreground) | yes | 196 | 1.97 s | — |
| Occluded (another window maximized on top) | yes | 196 | 1.89 s | yes |
| Minimized | yes | 195 | 2.16 s | yes (44/44 names) |

- Framework auto-detected as Chromium/Electron. `maxDepth: 20` required; the default depth 5 misses content.
- Chat list: `TreeItem` nodes, each name = chat title + last-message preview + timestamp.
- Open chat: message `Group` nodes with `Text` children (sender, time, body).

## Implications for kimeru
- Viable as the zero-consent default Teams source.
- Without clicking, only the **open chat** has full messages; other chats expose **last-message preview** only.
  Polling the chat list and diffing previews detects new activity; reading full text needs switching chats (UI side effect).

## Unverified
- Whether a minimized window reflects *new* messages live (tested content was unchanged between states).
- Work-account tenant UI vs. this account; localization differences in node names.
- Robustness across Teams UI updates.
