# studio5000-mcp

MCP server for **live tag access** to Rockwell Automation Logix5000
controllers (ControlLogix / CompactLogix) over EtherNet/IP (CIP), using
[`pycomm3`](https://github.com/ottowayi/pycomm3).

This talks directly to a running controller on the network. It does **not**
require Studio 5000 Logix Designer to be installed, and it does not touch
project files (`.ACD`/`.L5X`) or automate the Logix Designer application
itself — that's a separate integration path if you need it later.

## Tools

| Tool | Purpose |
|---|---|
| `studio5000_discover_plcs` | Broadcast-discover EtherNet/IP devices on the local network |
| `studio5000_get_controller_info` | Connect and return controller identity (name, revision, serial, etc.) |
| `studio5000_list_tags` | List controller/program tag definitions (name, data type, dims, access) |
| `studio5000_read_tag` | Read one tag's live value |
| `studio5000_read_tags` | Batch-read multiple tags in one round trip |
| `studio5000_write_tag` | Write one tag's value |
| `studio5000_write_tags` | Batch-write multiple tags in one round trip |
| `studio5000_disconnect` | Close a cached connection to a controller path |

Every tool takes a `path` (CIP path to the controller): a plain IP assumes
backplane slot 0 (`192.168.1.10`); append `/<slot>` for a specific
ControlLogix backplane slot (`192.168.1.10/1`).

## ⚠️ Safety note

**Writes are unrestricted by design** — there is no read-only gate, no dry-run
mode, and no tag allowlist. Any tool call that reaches `studio5000_write_tag`
or `studio5000_write_tags` will write to the live controller immediately, and
that can change the state of real, running physical equipment. Point this at
a test/offline controller while getting familiar with it, and only connect
it to a production controller path once you trust the calling context.

## Setup

```powershell
cd studio5000-mcp
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Register with Claude Code

```powershell
claude mcp add studio5000 -- "C:\Users\fjgonzalez\Desktop\studio5000-mcp\.venv\Scripts\python.exe" "C:\Users\fjgonzalez\Desktop\studio5000-mcp\server.py"
```

## Register with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "studio5000": {
      "command": "C:\\Users\\fjgonzalez\\Desktop\\studio5000-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\fjgonzalez\\Desktop\\studio5000-mcp\\server.py"]
    }
  }
}
```

## Manual test

```powershell
.\.venv\Scripts\python.exe -c "import asyncio, server as s; print(asyncio.run(s.studio5000_discover_plcs()))"
```
