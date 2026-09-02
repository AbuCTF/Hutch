# Caido MCP Server Research

> Research date: 2026-09-02
> Scope: MCP server capabilities, SDK comparison, integration patterns for Hutch + Caido

---

## 1. Overview

The **caido-mcp-server** is a community-developed (not official Caido) MCP server written in Go
that exposes Caido's proxy capabilities to AI agents via the Model Context Protocol. It
communicates with Caido over its GraphQL API using
[caido-community/sdk-go](https://github.com/caido-community/sdk-go).

- **Repo**: https://github.com/c0tton-fluff/caido-mcp-server
- **License**: MIT
- **Latest version**: v4.x (Go module path `v4/`); our installed binary is **v3.0.0**
- **Transport**: stdio (standard MCP stdio transport)
- **Language**: Go 1.24+

There is also **Drift** (by six2dez) -- a Caido *plugin* that embeds an MCP server directly
inside Caido, exposing 18 tools. It pipes local CLI tools (Claude Code, Gemini, Codex) through
the MCP. More limited than caido-mcp-server but tighter UI integration.

---

## 2. Connection & Configuration

### Environment Variables

| Variable                         | Purpose                                                       |
|----------------------------------|---------------------------------------------------------------|
| `CAIDO_URL`                      | Caido instance address (e.g. `http://127.0.0.1:8080`)        |
| `CAIDO_ACCESS_TOKEN`             | Static local access token (7-day expiry)                      |
| `CAIDO_ALLOW_SENSITIVE_HEADERS`  | Set `true` to reveal Authorization/Cookie headers (redacted by default) |

### Authentication Methods

**Option A -- OAuth Device Flow (recommended)**:
```bash
CAIDO_URL=http://localhost:8080 caido-mcp-server login
```
Prompts in Caido UI, auto-refreshes, stores token to `~/.caido-mcp/token.json`.

**Option B -- Static Token**:
Extract from Caido developer console:
```javascript
JSON.parse(localStorage.CAIDO_AUTHENTICATION).accessToken
```
Expires after 7 days.

### Claude Code Configuration

Already configured in `~/.claude.json`:
```json
{
  "mcpServers": {
    "caido": {
      "type": "stdio",
      "command": "/home/abu/.local/bin/caido-mcp-server",
      "args": ["serve"],
      "env": {
        "CAIDO_URL": "http://127.0.0.1:8081"
      }
    }
  }
}
```

### Cursor / Generic MCP Client
```json
{
  "mcpServers": {
    "caido": {
      "command": "/path/to/caido-mcp-server",
      "args": ["serve"],
      "env": {
        "CAIDO_URL": "http://127.0.0.1:8080"
      }
    }
  }
}
```

---

## 3. Complete MCP Tools List (66 tools in v4, ~34 in our v3)

### Request Management
| Tool                      | Description                                                       |
|---------------------------|-------------------------------------------------------------------|
| `caido_list_requests`     | Filter proxied requests with HTTPQL, pagination                   |
| `caido_get_request`       | Full request/response details with selective field includes       |
| `caido_diff_responses`    | Structural diff between two responses                             |
| `caido_export_curl`       | Convert captured request to executable curl command               |

### Replay & Sending
| Tool                          | Description                                                   |
|-------------------------------|---------------------------------------------------------------|
| `caido_send_request`          | Execute HTTP via Replay; 10s polling; auto-cookie injection   |
| `caido_batch_send`            | Parallel requests (max 50); returns fingerprints              |
| `caido_edit_request`          | Modify and resend existing request                            |
| `caido_create_replay_session` | Named session with optional seed request                      |
| `caido_list_replay_sessions`  | View all replay sessions                                      |
| `caido_delete_replay_sessions`| Bulk deletion of sessions                                     |
| `caido_move_replay_session`   | Reorganize sessions between collections                       |
| `caido_get_replay_entry`      | Retrieve specific replay results                              |
| `caido_clear_session_cookies` | Wipe session cookie jar                                       |
| `caido_get_session_cookies`   | List stored cookie metadata (values excluded by default)      |

### Replay Collections
| Tool                              | Description                          |
|-----------------------------------|--------------------------------------|
| `caido_list_replay_collections`   | Browse session groups                |
| `caido_create_replay_collection`  | Create new collection                |
| `caido_rename_replay_collection`  | Update collection name               |
| `caido_delete_replay_collection`  | Remove collection                    |

### Fuzzing / Automate
| Tool                         | Description                                           |
|------------------------------|-------------------------------------------------------|
| `caido_list_automate_sessions`| View fuzzing sessions                                |
| `caido_get_automate_session` | Session details with entries                          |
| `caido_get_automate_entry`   | Fuzz results and payloads                             |
| `caido_automate_task_control`| Start/pause/resume/cancel fuzzing tasks               |

### Findings
| Tool                    | Description                                        |
|-------------------------|----------------------------------------------------|
| `caido_list_findings`   | Security findings with pagination                  |
| `caido_create_finding`  | Attach finding to request with title/description   |
| `caido_delete_findings` | Remove by IDs or reporter                          |
| `caido_export_findings` | Generate findings report                           |

### Sitemap & Scopes
| Tool                | Description                                        |
|---------------------|----------------------------------------------------|
| `caido_get_sitemap` | Browse discovered endpoint hierarchy               |
| `caido_list_scopes` | Target scope definitions                           |
| `caido_is_in_scope` | Host/URL validation against scope rules            |
| `caido_create_scope`| Define allowlist/denylist scope rules              |
| `caido_rename_scope`| Update scope name                                  |
| `caido_delete_scope`| Remove scope preset                                |

### Projects
| Tool                  | Description                          |
|-----------------------|--------------------------------------|
| `caido_list_projects` | All projects with current marker     |
| `caido_select_project`| Switch active project                |
| `caido_create_project`| Create new project                   |
| `caido_rename_project`| Rename project                       |
| `caido_delete_project`| Remove project                       |

### Workflows
| Tool                   | Description                                 |
|------------------------|---------------------------------------------|
| `caido_list_workflows` | List automation workflows                   |
| `caido_run_workflow`   | Execute active or convert-type workflow     |
| `caido_toggle_workflow`| Enable/disable workflow                     |

### Tamper Rules (Match & Replace)
| Tool                      | Description                                        |
|---------------------------|----------------------------------------------------|
| `caido_list_tamper_rules` | All collections with nested rules                  |
| `caido_create_tamper_rule`| New rule with operation modes (updateRaw/updateValue/add/remove) |
| `caido_update_tamper_rule`| Full rule state replacement                        |
| `caido_test_tamper_rule`  | Dry-run without persistence                        |
| `caido_toggle_tamper_rule`| Enable/disable rule                                |
| `caido_delete_tamper_rule`| Remove rule                                        |

### Intercept
| Tool                          | Description                              |
|-------------------------------|------------------------------------------|
| `caido_intercept_status`      | Current state (PAUSED/RUNNING)           |
| `caido_intercept_control`     | Pause or resume interception             |
| `caido_list_intercept_entries`| Queued items with HTTPQL filtering       |
| `caido_forward_intercept`     | Forward with optional modifications      |
| `caido_drop_intercept`        | Discard intercepted request              |

### Environments & Variables
| Tool                      | Description                          |
|---------------------------|--------------------------------------|
| `caido_list_environments` | All environments with variables      |
| `caido_select_environment`| Switch active environment            |
| `caido_create_environment`| Create new environment               |
| `caido_delete_environment`| Remove environment                   |

### Filters & Utilities
| Tool                     | Description                                   |
|--------------------------|-----------------------------------------------|
| `caido_list_filters`     | Saved HTTPQL filter presets                    |
| `caido_create_filter`    | Save named HTTPQL query                        |
| `caido_delete_filter`    | Remove filter preset                           |
| `caido_list_hosted_files`| Served payload files                           |
| `caido_list_tasks`       | Background operations                          |
| `caido_cancel_task`      | Stop task by ID                                |
| `caido_list_plugins`     | Installed plugin packages                      |
| `caido_list_ws_streams`  | WebSocket connections                          |
| `caido_list_ws_messages` | Frame details with decoding                    |
| `caido_convert_body`     | Transform between JSON/form/XML/multipart      |
| `caido_race_window_send` | Synchronized race condition testing            |
| `caido_get_instance`     | Version and platform info                      |

### MCP Resources (read-only, no tool call needed)

| URI                          | Content                         |
|------------------------------|---------------------------------|
| `caido://requests/{id}`      | Complete request/response       |
| `caido://replay-sessions/{id}`| Session details with entries   |
| `caido://sitemap`            | Root domains                    |
| `caido://findings`           | Summaries (max 100)             |
| `caido://scopes`             | All scopes with rules           |
| `caido://project`            | Current project, version, status|

---

## 4. Built-in Safety Features

| Feature                  | Detail                                                                    |
|--------------------------|---------------------------------------------------------------------------|
| **Header redaction**     | Authorization, Cookie, Set-Cookie, Proxy-Authorization, X-Api-Key, X-Auth-Token replaced with `[REDACTED]` by default |
| **Opt-out**              | `CAIDO_ALLOW_SENSITIVE_HEADERS=true` to see real values                  |
| **Body limits**          | JSON 4KB, HTML 3KB, binary 200B; override with explicit `bodyLimit`      |
| **Batch cap**            | Max 50 parallel requests in `batch_send`                                 |
| **Input validation**     | Length-validated input strings to prevent context flooding                |
| **Response dedup**       | Repeated identical responses collapse to one-line summary                |
| **Token security**       | Credentials never in process args or logs; token file is 0600            |
| **Session cookie jar**   | RFC 6265-compliant per-session jar; auto-persists Set-Cookie; disable with `useCookieJar: false` |
| **Response fingerprint** | Auto-returns title, redirect, cookieNames, wordCount, notableHeaders     |
| **Token auto-refresh**   | Expired OAuth tokens refresh mid-session                                 |

---

## 5. Comparison: MCP vs SDK vs GraphQL

### Interface Overview

| Aspect               | MCP Server (caido-mcp-server)          | Client SDK (@caido/sdk-client)         | Raw GraphQL                          |
|-----------------------|----------------------------------------|----------------------------------------|--------------------------------------|
| **Language**          | Any (stdio transport)                  | JavaScript/TypeScript                  | Any (HTTP POST)                      |
| **Protocol**          | MCP over stdio                         | JS wrapper over GraphQL                | GraphQL over HTTP                    |
| **Auth**              | OAuth device flow or static token      | Token in Client constructor            | Bearer token in Authorization header |
| **Tool count**        | 66 tools + 6 resources (v4)            | Domain SDKs (requests, replay, findings, scope, etc.) | Full schema (~all operations)  |
| **Latency**           | Process spawn + stdio overhead         | Direct HTTP/WS to Caido                | Direct HTTP to Caido                 |
| **Real-time**         | No streaming; poll-based               | GraphQL subscriptions available        | Subscriptions available              |
| **Safety**            | Header redaction, body limits, batch cap | None built-in                         | None built-in                        |
| **Best for**          | AI agent interaction                   | External scripts/tools, automation     | Maximum control, custom queries      |

### Capability Matrix

| Capability                 | MCP | SDK | GraphQL | Notes                                    |
|----------------------------|-----|-----|---------|------------------------------------------|
| List/filter requests       | Y   | Y   | Y       | MCP uses HTTPQL; SDK wraps GraphQL       |
| Get request details        | Y   | Y   | Y       | MCP has body-size limits by default      |
| Send request (Replay)      | Y   | Y   | Y       | MCP adds cookie jar + fingerprinting     |
| Batch send                 | Y   | -   | Y       | MCP-specific convenience tool            |
| Race condition testing     | Y   | -   | -       | MCP-only `race_window_send`              |
| Create findings            | Y   | Y   | Y       |                                          |
| Export findings             | Y   | -   | Y       | MCP convenience tool                     |
| Manage scopes              | Y   | Y   | Y       |                                          |
| Check in-scope             | Y   | Y   | Y       | MCP returns rule details                 |
| Manage projects            | Y   | Y   | Y       |                                          |
| Manage environments        | Y   | Y   | Y       |                                          |
| Automate/fuzzing sessions  | Y   | -   | Y       | MCP wraps GraphQL automate queries       |
| Automate task control      | Y   | -   | Y       | MCP: start/pause/resume/cancel           |
| Tamper rules (match/replace)| Y  | -   | Y       | MCP: create/update/test/toggle/delete    |
| Intercept control          | Y   | -   | Y       | MCP: status/pause/resume/forward/drop    |
| Workflows                  | Y   | -   | Y       | MCP: list/run/toggle                     |
| WebSocket streams/messages | Y   | -   | Y       | MCP convenience                          |
| Response diffing           | Y   | -   | -       | MCP-only structural diff                 |
| Export to curl              | Y   | -   | -       | MCP-only                                 |
| Body format conversion     | Y   | -   | -       | MCP: JSON/form/XML/multipart             |
| Header redaction           | Y   | -   | -       | MCP-only safety feature                  |
| Response fingerprinting    | Y   | -   | -       | MCP-only token-saving feature            |
| GraphQL subscriptions      | -   | Y   | Y       | Real-time events; MCP has no equivalent  |
| Plugin execution           | -   | Y   | Y       | SDK has plugin function calling           |
| Plugin events              | -   | Y   | -       | SDK-specific event reception             |
| Custom caching             | -   | Y   | -       | SDK supports custom cache layer          |

### Performance Considerations

- **MCP**: Adds process-spawn overhead per session. Each tool call is a stdio round-trip.
  Response polling adds ~10s max wait for replay. Best for AI agent orchestration where
  latency tolerance is higher.
- **SDK**: Direct HTTP/WebSocket connection to Caido. Lower latency. Supports GraphQL
  subscriptions for real-time traffic monitoring. Best for programmatic automation.
- **GraphQL**: Lowest level, most flexible. No overhead beyond HTTP. Subscriptions for
  real-time. Best for maximum control and custom queries.

---

## 6. Alternative: Drift (Caido Plugin with Embedded MCP)

**Drift** by six2dez is a Caido plugin that embeds its own MCP server directly inside Caido.

| Aspect          | caido-mcp-server                 | Drift                                |
|-----------------|----------------------------------|--------------------------------------|
| **Type**        | External Go binary               | Caido plugin (JS)                    |
| **Tools**       | 66                               | 18                                   |
| **Installation**| Binary + auth setup              | Plugin zip install in Caido UI       |
| **CLI support** | Yes (caido-cli companion)        | No                                   |
| **UI integration**| None (headless)                | Chat UI inside Caido                 |
| **Backends**    | Any MCP client                   | Claude Code, Gemini, Codex, Copilot  |
| **Auth**        | OAuth or static token            | Uses Caido's session token           |
| **Context**     | HTTPQL + explicit queries        | Tracks UI context (active project)   |

### Drift's 18 Tools

| Tool                  | Purpose                                       |
|-----------------------|-----------------------------------------------|
| `search_history`      | Query HTTP history with HTTPQL + Drift context|
| `get_current_context` | Current UI context / override / effective     |
| `list_projects`       | Show projects with current marker             |
| `select_project`      | Set explicit project override                 |
| `clear_context_override`| Revert to UI context                        |
| `get_request`         | Full raw request/response by ID               |
| `send_request`        | Execute via Caido Replay                      |
| `create_replay_session`| Generate session from request ID             |
| `create_finding`      | Document a security finding                   |
| `list_findings`       | Display all findings                          |
| `get_scope`           | List scope definitions                        |
| `check_scope`         | Verify URL matches scope                      |
| `get_environment`     | List environments and variables               |
| `set_environment`     | Modify environment variables                  |
| `run_workflow`        | Execute a convert workflow                    |
| `intercept_status`    | Check intercept state                         |
| `intercept_pause`     | Suspend HTTP intercept                        |
| `intercept_resume`    | Reactivate HTTP intercept                     |

---

## 7. Strix Integration Pattern (Reference Architecture)

Strix (autonomous pentesting platform) uses Caido as its proxy backend, demonstrating a
production-grade integration:

- Uses **GraphQL API + SDK** (not MCP) for agent-to-Caido communication
- Multi-agent system with parallel specialized agents routing through one Caido instance
- Headless operation: Caido's client-server separation means no UI dependency
- Agents pull auth tokens, query traffic with HTTPQL, create replay sessions, chain requests
- All agent work persists in Caido projects (History, Sitemap, Findings, Replay sessions)
- Result: vulnerability detection accuracy improved from 82% to 96%

**Key insight**: For high-performance autonomous operation, Strix chose the SDK/GraphQL
path over MCP. MCP is better for interactive AI-assisted workflows where a human is in
the loop.

---

## 8. Integration Recommendations for Hutch + Caido

### Architecture Decision

**Use BOTH MCP and SDK, for different purposes:**

1. **MCP server** -- for Claude Code / agent orchestration layer:
   - Agent-driven reconnaissance and request analysis
   - Finding creation and management
   - Scope validation (critical for BBP gates)
   - Interactive security testing workflows
   - The header redaction is a *feature* for LLM context (prevents credential leakage)
   - Response fingerprinting saves tokens

2. **SDK (@caido/sdk-client)** -- for Hutch's programmatic layer:
   - High-throughput request sending and response processing
   - Real-time traffic monitoring via GraphQL subscriptions
   - Tight integration with Playwright browser automation
   - Cookie/session management for multi-account testing
   - Custom automation scripts

3. **GraphQL** -- escape hatch for anything neither covers:
   - Custom queries not yet wrapped by MCP or SDK
   - Subscription-based real-time monitoring

### Recommended Connection Topology

```
                    +------------------+
                    |  Claude Code     |
                    |  (Orchestrator)  |
                    +--------+---------+
                             |
                    MCP (stdio)
                             |
                    +--------+---------+
                    | caido-mcp-server |
                    +--------+---------+
                             |
                    GraphQL (HTTP)
                             |
+---------------+   +--------+---------+   +----------------+
|  Playwright   |---|    Caido Proxy    |---|   Target App   |
|  (Hutch)      |   |  (intercepting)  |   |                |
+---------------+   +------------------+   +----------------+
        |                    |
   SDK (@caido/sdk-client)   |
        |                    |
  +-----+-------+     +-----+------+
  | Request Eng. |     |  Findings  |
  | (send/replay)|     |  (create)  |
  +--------------+     +------------+
```

### Scope Gate Integration

The MCP's `caido_is_in_scope` and `caido_create_scope` tools map directly to the BBP
scope gate requirement:
- Before any outward request, call `caido_is_in_scope` to validate the target
- Use `caido_create_scope` to configure program-specific allowlists
- The scope check returns rule details, enabling audit logging

### Upgrade Path

Our installed binary is **v3.0.0** but the latest is **v4.x** with 66 tools (vs ~34 in v3).
Key additions in v4:
- Batch send, race condition testing
- Tamper rule management
- Intercept control (forward/drop)
- Replay collections
- Response diffing and curl export
- Body format conversion
- WebSocket support

**Recommend upgrading**:
```bash
go install github.com/c0tton-fluff/caido-mcp-server/v4/cmd/caido-mcp-server@latest
```
Or use the install script:
```bash
curl -fsSL https://raw.githubusercontent.com/c0tton-fluff/caido-mcp-server/main/install.sh | bash
```

### Configuration for CAIDO_ALLOW_SENSITIVE_HEADERS

For the BBP harness, we **need** real header values (Authorization, Cookie) to:
- Extract auth tokens for cross-account IDOR testing
- Validate session management bugs
- Chain requests with proper authentication

Set `CAIDO_ALLOW_SENSITIVE_HEADERS=true` in the harness environment, but **only** within
the scope-gated execution path (never in the orchestrator's general MCP config where
headers would leak into LLM context).

### CLI for Non-AI Automation

The caido-mcp-server includes a standalone CLI (`caido-cli`) useful for:
```bash
# Check instance health
caido-cli status -u http://localhost:8081

# Quick request send
caido-cli send GET https://target.com/api/users

# Browse history with HTTPQL
caido-cli history -f 'req.host.eq:"target.com"' -n 20

# Batch fuzz a parameter
caido-cli batch fuzz "https://target.com/api/search?q=test" -p q -v "test,test',<script>"
```

---

## 9. Other Relevant Tools in the Ecosystem

| Tool                | Type          | Purpose                                         |
|---------------------|---------------|--------------------------------------------------|
| **Drift**           | Caido plugin  | 18 MCP tools embedded in Caido UI               |
| **Shift**           | Caido plugin  | Official AI plugin with floating prompts + skills|
| **Ebka AI**         | Caido plugin  | 30+ operational tools                            |
| **Chatio**          | Caido plugin  | Purpose-built hacker assistant                   |
| **Vibe Hacking**    | Caido plugin  | On-demand MCP server launching                   |
| **caido-mode**      | Claude Skill  | Caido SDK interaction skill (already in our stack)|
| **WireMCP**         | MCP server    | Wireshark/TShark packet analysis                 |
| **Shodan MCP**      | MCP server    | API querying + vuln database                     |
| **PentestAgent**    | Framework     | Black-box testing with Caido backend             |

---

## 10. Key Takeaways

1. **MCP server is mature and comprehensive** -- 66 tools covering nearly everything
   Caido can do, with built-in safety features ideal for AI agent use.

2. **Not official Caido** -- community-developed, MIT licensed. The MCP server is a
   separate process that talks to Caido via its public GraphQL API.

3. **We're on v3, should upgrade to v4** -- doubles the tool count, adds critical
   capabilities (batch send, race testing, intercept control, tamper rules).

4. **MCP for orchestration, SDK for performance** -- this is what Strix proved at scale.
   MCP is great for agent decision-making; SDK is better for high-throughput automation.

5. **Header redaction is both a feature and a limitation** -- protects against credential
   leakage to LLM context, but the harness execution path needs real headers. Use
   `CAIDO_ALLOW_SENSITIVE_HEADERS` selectively.

6. **No real-time streaming in MCP** -- GraphQL subscriptions are the only way to get
   live traffic events. The MCP server polls with timeouts.

7. **Scope tools map directly to our scope gate** -- `caido_is_in_scope` and
   `caido_create_scope` are the natural integration point.

8. **The CLI is a bonus** -- useful for shell-script automation in the harness pipeline
   without needing to go through MCP or SDK.
