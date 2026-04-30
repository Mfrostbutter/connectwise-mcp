# ConnectWise Live MCP Server

FastMCP HTTP server wrapping the ConnectWise Manage REST API. 45 tools across Service, Operations, and Finance domains. Finance tools are gated behind `CW_TIER=leadership` so you can expose a restricted manifest to non-finance users.

## Installation

**Via uvx (recommended — no clone, no venv):**
```bash
uvx connectwise-mcp
```
Set credentials via environment variables or a `.env` file in your working directory.

**Via pip:**
```bash
pip install connectwise-mcp
connectwise-mcp
```

**From source:**
```bash
git clone https://github.com/Mfrostbutter/connectwise-mcp
cd connectwise-mcp
cp .env.example .env
# fill in your CW_* credentials
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 server.py
# verify: curl http://localhost:8085/health
```

## Transport modes

| Mode | How to set | Best for |
|------|-----------|----------|
| `http` (default) | `MCP_TRANSPORT=http` | Persistent server shared across sessions or team members |
| `stdio` | `MCP_TRANSPORT=stdio` | Cursor, VS Code, Zed, Continue, or any stdio-based MCP client |

In stdio mode the server is spawned per-session by the client — no port, no persistent process.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `CW_BASE_URL` | Yes | — | e.g. `https://na.myconnectwise.net/v4_6_release/apis/3.0` |
| `CW_COMPANY_ID` | Yes | — | Your CW company ID |
| `CW_PUBLIC_KEY` | Yes | — | CW API public key |
| `CW_PRIVATE_KEY` | Yes | — | CW API private key |
| `CW_CLIENT_ID` | Yes | — | CW API client ID (from developer.connectwise.com) |
| `CW_TIER` | No | `leadership` | `leadership` (all 31 tools) or `tech` (21 tools, no finance) |
| `MCP_AUTH_TOKEN` | No | — | Bearer token for MCP client auth; omit to run without auth |
| `CW_LIVE_MCP_PORT` | No | `8085` | HTTP listen port |

## Tools

**Service (12):** `get_ticket`, `search_tickets`, `get_open_tickets`, `add_ticket_note`, `create_ticket`, `update_ticket_status`, `get_boards`, `get_board_statuses`, `get_priorities`, `get_ticket_time`, `search_cw_kb_articles`, `get_ticket_count`

**Operations (19):** `get_company`, `search_companies`, `get_contacts`, `get_configurations`, `get_configuration`, `get_projects`, `get_project_tickets`, `get_time_entries`, `log_time`, `get_members`, `get_company_types`, `get_company_statuses`, `get_configuration_statuses`, `get_work_types`, `get_project_statuses`, `create_project`, `update_project`, `add_project_phase`, `add_project_note`

**Finance (14, leadership only):** `get_invoices`, `get_invoice`, `get_agreements`, `get_agreement`, `get_agreement_types`, `create_agreement`, `update_agreement`, `add_agreement_addition`, `get_agreement_additions`, `get_agreement_count_by_type`, `get_client_mrr`, `get_aging_invoices`, `get_opportunities`, `create_opportunity`

## Client configuration

**HTTP mode** — Claude Desktop, Claude Code (server runs persistently):

`claude_desktop_config.json` / `.claude/settings.json`:
```json
{
  "mcpServers": {
    "connectwise": {
      "type": "http",
      "url": "http://localhost:8085/mcp",
      "headers": { "Authorization": "Bearer your_token_here" }
    }
  }
}
```

**stdio mode** — Cursor, VS Code, Zed, Continue, or any stdio client (server spawned per-session):

```json
{
  "mcpServers": {
    "connectwise": {
      "command": "uvx",
      "args": ["connectwise-mcp"],
      "env": {
        "CW_BASE_URL": "https://na.myconnectwise.net/v4_6_release/apis/3.0",
        "CW_COMPANY_ID": "yourcompanyid",
        "CW_PUBLIC_KEY": "your_public_key",
        "CW_PRIVATE_KEY": "your_private_key",
        "CW_CLIENT_ID": "your_client_id",
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

## Customization

**`_JUNK_PRODUCTS`** (top of `server.py`): lowercase product identifiers excluded from MRR calculations. Add your own setup fees, shipping line items, discount codes, and non-recurring labor entries here so they don't inflate MRR/ARR figures.

**Tier gating**: set `CW_TIER=tech` to expose a 21-tool manifest with no finance visibility. Useful for shared environments or tech-facing deployments.

## Running as a service

```ini
[Unit]
Description=ConnectWise Live MCP
After=network.target

[Service]
User=mcp
WorkingDirectory=/opt/connectwise-mcp
EnvironmentFile=/opt/connectwise-mcp/.env
ExecStart=/opt/connectwise-mcp/venv/bin/python3 server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## ConnectWise API credentials

Generate API keys in ConnectWise Manage: **System > Members > [your member] > API Keys**. Get your Client ID from [developer.connectwise.com](https://developer.connectwise.com).

## License

MIT
