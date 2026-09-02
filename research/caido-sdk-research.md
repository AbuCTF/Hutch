# Caido SDK & Integration Research

Comprehensive research on Caido's programmatic capabilities for integrating with Hutch
(Playwright-based browser session orchestrator).

**Source:** SDK source code from `caido/sdk-js` GitHub repo (primary),
official docs at `docs.caido.io`, developer docs at `developer.caido.io`.

**SDK version surveyed:** `@caido/sdk-client` 0.5.0 (latest as of research date).

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [CLI & Headless Operation](#2-cli--headless-operation)
3. [SDK Client Setup & Authentication](#3-sdk-client-setup--authentication)
4. [Complete SDK API Reference](#4-complete-sdk-api-reference)
5. [HTTPQL Query Language Reference](#5-httpql-query-language-reference)
6. [Project Management & Plan Limits](#6-project-management--plan-limits)
7. [GraphQL API Access](#7-graphql-api-access)
8. [Integration Patterns for Hutch](#8-integration-patterns-for-hutch)

---

## 1. Architecture Overview

Caido uses a **client/server architecture**:

- **CLI (Server):** The Rust binary that runs the proxy, stores data in SQLite, and
  exposes a **GraphQL API** + **REST API** over HTTP.
- **UI (Client):** An Electron/web frontend that communicates with the CLI via GraphQL.
- **SDK (`@caido/sdk-client`):** A TypeScript library that wraps the same GraphQL/REST
  API the UI uses, enabling full programmatic control from external scripts.

All three layers (UI, SDK, raw GraphQL) have access to the same underlying operations.
The SDK is the recommended path; raw GraphQL is available for anything the SDK doesn't
wrap yet.

### Key Data Flow

```
Browser (Playwright/Hutch)
    |
    | HTTP/HTTPS (proxy: ADDR:PORT)
    v
Caido Proxy (intercepts, stores to SQLite)
    |
    | GraphQL API (localhost:8080/graphql)
    v
@caido/sdk-client (Hutch integration layer)
    |
    | queries, replay, findings, scopes
    v
Hutch Orchestrator
```

---

## 2. CLI & Headless Operation

### CLI Flags (Complete Reference)

| Flag | Description |
|------|-------------|
| `-l, --listen <ADDR:PORT>` | Main listening address (both proxy + UI) |
| `--proxy-listen <ADDR:PORT>` | Proxy-only listener (separate from UI), repeatable |
| `--ui-listen <ADDR:PORT>` | UI-only listener |
| `--ui-domain <DOMAIN>` | Allowed domains for UI access |
| `--invisible` | Enable invisible proxy mode for all listeners |
| `--no-open` | Don't auto-open browser to UI |
| `--debug` | Record and display debug logs |
| `--data-path <PATH>` | Directory for storing data (projects, certs, etc.) |
| `--no-logging` | Disable file-based logging |
| `--no-renderer-sandbox` | Disable renderer sandbox |
| `--reset-cache` | Reset instance cache of cloud data |
| `--reset-credentials` | Reset instance credentials (DANGEROUS) |
| `--import-ca-cert <PATH>` | Import a CA certificate (PKCS#12) |
| `--import-ca-cert-pass <PASS>` | Password for CA cert import |
| `--allow-guests` | Allow guest login without auth |
| `--registration-key <KEY>` | Registration key for headless deployment |
| `--safe` | Enable safe mode |
| `-h, --help` | Display help |
| `-V, --version` | Show version |

### Headless Deployment (Teams plan required)

**Registration key method** (recommended for automation):

```bash
# Method 1: CLI flag
caido --registration-key ckey_xxxxx --no-open --listen 0.0.0.0:8080

# Method 2: Environment variable
export CAIDO_REGISTRATION_KEY=ckey_xxxxx
caido --no-open --listen 0.0.0.0:8080
```

The instance auto-registers and claims itself on startup. After registration,
authenticate scripts using a **Personal Access Token (PAT)**.

### For Hutch (Practical Launch)

For our use case (local, same-box), no registration key is needed:

```bash
# Launch Caido headless-style (still has UI but doesn't auto-open)
caido --no-open \
      --proxy-listen 127.0.0.1:8080 \
      --data-path /home/abu/.caido-hutch \
      --allow-guests
```

The `--allow-guests` flag simplifies local dev by not requiring auth for the
GraphQL API. For production, use PAT auth instead.

---

## 3. SDK Client Setup & Authentication

### Installation

```bash
npm install @caido/sdk-client
# or
pnpm add @caido/sdk-client
```

Requires Node.js 18+.

### Client Instantiation

```typescript
import { Client } from "@caido/sdk-client";

// Simple (guest/no auth, for local dev)
const client = new Client({
  url: "http://localhost:8080",
});
await client.connect();

// With PAT authentication (production/headless)
const client = new Client({
  url: "http://localhost:8080",
  auth: {
    pat: "caido_xxxxx",
    cache: {
      file: ".secrets.json",  // Caches tokens to disk
    },
  },
});
await client.connect();

// With direct token
const client = new Client({
  url: "http://localhost:8080",
  auth: {
    token: "your_access_token",
  },
});
await client.connect();
```

### ClientOptions (full type)

```typescript
interface ClientOptions {
  url: string;                  // Base URL of Caido instance
  auth?: AuthOptions;           // PAT, token, or browser auth
  request?: {
    timeout?: number;           // Request timeout in ms
    fetch?: typeof fetch;       // Custom fetch implementation
  };
  version?: Version;            // Skip /health version probe
  logger?: Logger;              // Custom logger (default: ConsoleLogger)
}
```

### Authentication Methods

1. **PATAuthOptions** -- Personal Access Token (recommended for scripts):
   ```typescript
   { pat: "caido_xxxxx", cache?: { file: "path" } | { localstorage: "key" } }
   ```

2. **TokenAuthOptions** -- Direct access token:
   ```typescript
   { token: "access_token" }
   // or with refresh:
   { token: { accessToken: "...", refreshToken: "..." } }
   ```

3. **BrowserAuthOptions** -- Interactive device code flow:
   ```typescript
   { onRequest?: (request) => void }
   ```

Token cache options:
- `{ file: "path" }` -- file-based (Node.js)
- `{ localstorage: "key" }` -- browser LocalStorage
- Custom `TokenCache` interface implementation

---

## 4. Complete SDK API Reference

The `Client` class exposes these SDK modules:

| Module | Class | Description |
|--------|-------|-------------|
| `client.graphql` | `GraphQLClient` | Low-level GraphQL (query/mutation/subscribe) |
| `client.rest` | `RestClient` | Low-level REST (GET/POST) |
| `client.version` | `Version` | Instance version (lazy `/health` or seeded) |
| `client.user` | `UserSDK` | Current user info |
| `client.project` | `ProjectSDK` | Project CRUD + selection |
| `client.scope` | `ScopeSDK` | Scope CRUD |
| `client.filter` | `FilterSDK` | Filter preset CRUD |
| `client.request` | `RequestSDK` | Query/create HTTP requests |
| `client.finding` | `FindingSDK` | Finding CRUD |
| `client.replay` | `ReplaySDK` | Replay sessions/collections/entries + send |
| `client.environment` | `EnvironmentSDK` | Environment variable management |
| `client.workflow` | `WorkflowSDK` | Workflow CRUD + test + run |
| `client.hostedFile` | `HostedFileSDK` | File upload/manage for payloads |
| `client.plugin` | `PluginSDK` | Plugin install + function calls + events |
| `client.instance` | `InstanceSDK` | Certificate + settings management |
| `client.dnsRewrite` | `DNSRewriteSDK` | DNS rewrite rules |
| `client.dnsUpstream` | `DNSUpstreamSDK` | DNS upstream resolvers |

### 4.1 ProjectSDK -- `client.project`

Manages workspaces (called "projects" in the SDK).

```typescript
// List all projects
const projects = await client.project.list();
// => Project[] with { id, name, path, status, temporary, createdAt, updatedAt, version, size, readOnly }

// Create a project
const project = await client.project.create({
  name: "Yandex Hunt",
  temporary: false,  // true = auto-deleted on close
});

// Select a project (switch active workspace)
const active = await client.project.select(project.id);

// Rename a project
await client.project.rename(project.id, "Yandex Hunt v2");

// Delete a project
await client.project.delete(project.id);
```

**Project type:**
```typescript
type Project = {
  id: ID;
  name: string;
  path: string;           // Filesystem path to project data
  status: "ERROR" | "READY" | "RESTORING";
  temporary: boolean;
  createdAt: Date;
  updatedAt: Date;
  version: string;
  size: number;            // Size in bytes
  readOnly: boolean;
};
```

### 4.2 ScopeSDK -- `client.scope`

Scopes filter requests by URL patterns (allowlist/denylist of globs).

```typescript
// List all scopes
const scopes = await client.scope.list();

// Create a scope
const scope = await client.scope.create({
  name: "Yandex Scope",
  allowlist: ["*.yandex.ru", "*.yandex.com", "*.ya.ru"],
  denylist: ["*.passport.yandex.ru"],
});

// Get a scope by ID
const scope = await client.scope.get(scopeId);

// Update a scope
await client.scope.update(scopeId, {
  name: "Yandex Scope v2",
  allowlist: ["*.yandex.ru", "*.yandex.com"],
  denylist: [],
});

// Delete a scope
await client.scope.delete(scopeId);
```

**Scope type:**
```typescript
type Scope = {
  id: ID;
  name: string;
  allowlist: string[];   // Glob patterns like "*.example.com"
  denylist: string[];
  indexed: boolean;
};
```

### 4.3 RequestSDK -- `client.request`

Query intercepted HTTP traffic and create requests programmatically.

```typescript
// List requests with HTTPQL filter (returns Connection with pagination)
const connection = await client.request.list()
  .filter('req.host.cont:"yandex" AND resp.code.eq:200')
  .first(50)
  .descending("req", "created_at");

// Access results
for (const edge of connection.edges) {
  const { request, response } = edge.node;
  console.log(request.method, request.host, request.path);
  console.log(response?.statusCode, response?.length);
  // request.raw and response.raw are Uint8Array when included
}

// Paginate
const nextPage = await connection.next();

// Get a single request by ID
const reqResp = await client.request.get(requestId);
const reqResp = await client.request.get(requestId, {
  requestRaw: true,    // Include raw request bytes
  responseRaw: false,  // Skip raw response bytes
});

// Control raw body inclusion on lists
const conn = await client.request.list()
  .includeRaw({ request: true, response: false })
  .first(100);

// Scope-filtered listing
const conn = await client.request.list()
  .scope(scopeId)
  .filter('req.method.eq:"POST"')
  .first(100);

// Create a request programmatically (writes to history)
const created = await client.request.create({
  host: "api.example.com",
  method: "GET",
  path: "/api/v1/users",
  port: 443,
  query: "page=1",
  raw: new TextEncoder().encode("GET /api/v1/users?page=1 HTTP/1.1\r\nHost: api.example.com\r\n\r\n"),
  alteration: "none",
  source: "sdk",
});
```

**Request/Response types:**
```typescript
type Request = {
  id: ID;
  host: string;
  port: number;
  method: string;
  path: string;
  query: string;
  isTls: boolean;
  metadata: { id: ID; color?: string };
  createdAt: Date;
  raw?: Uint8Array;  // Present when includeRaw true
};

type Response = {
  id: ID;
  statusCode: number;
  roundtripTime: number;  // ms
  length: number;         // bytes
  createdAt: Date;
  raw?: Uint8Array;
};

type RequestResponseOpt = {
  request: Request;
  response?: Response;
};
```

**Available order fields:**
- Request: `ext`, `host`, `id`, `method`, `path`, `query`, `created_at`, `source`
- Response: `length`, `roundtrip`, `code`

### 4.4 FindingSDK -- `client.finding`

Create security findings attached to specific requests.

```typescript
// Create a finding
const finding = await client.finding.create(requestId, {
  title: "Reflected XSS in search parameter",
  reporter: "hutch-scanner",
  description: "The `q` parameter is reflected without encoding...",
  dedupeKey: "xss-search-param-yandex",  // Prevents duplicate findings
});

// List findings (paginated)
const findings = await client.finding.list().first(50);

// Get a single finding
const finding = await client.finding.get(findingId);

// Update a finding
await client.finding.update(findingId, {
  title: "Confirmed: Reflected XSS in search",
  description: "Updated after validation...",
  hidden: false,
});

// Delete findings by ID
await client.finding.delete({ ids: ["finding-1", "finding-2"] });

// Delete all findings by reporter
await client.finding.delete({ reporter: "hutch-scanner" });

// Delete ALL findings
await client.finding.delete();
```

**Finding type:**
```typescript
type Finding = {
  id: ID;
  requestId: ID;
  title: string;
  reporter: string;
  description: string | undefined;
  dedupeKey: string | undefined;
  host: string;
  path: string;
  hidden: boolean;
  createdAt: Date;
};
```

### 4.5 ReplaySDK -- `client.replay`

Full request replay capabilities: sessions, collections, entries, and the
critical `send()` method.

#### Sessions

```typescript
// Create a session from scratch
const session = await client.replay.sessions.create();

// Create from an existing request (copies it into replay)
const session = await client.replay.sessions.create({
  requestSource: { id: existingRequestId },
});

// Create from raw input
const session = await client.replay.sessions.create({
  requestSource: {
    raw: "GET /api/test HTTP/1.1\r\nHost: example.com\r\n\r\n",
    connection: { host: "example.com", port: 443, isTLS: true },
  },
  collectionId: collectionId,  // Optional: organize into collection
});

// List sessions (paginated)
const sessions = await client.replay.sessions.list().first(100);

// Get a session by ID
const session = await client.replay.sessions.get(sessionId);

// Rename
await client.replay.sessions.rename(sessionId, "IDOR Test - User A");

// Move to another collection
await client.replay.sessions.move(sessionId, newCollectionId);

// Delete sessions
await client.replay.sessions.delete([sessionId1, sessionId2]);

// Set active entry in session
await client.replay.sessions.setActiveEntry(sessionId, entryId);

// List entries in a session
const entries = await session.entries().first(50);
```

#### Collections

```typescript
// Create a collection
const collection = await client.replay.collections.create({
  name: "Yandex IDOR Tests",
});

// List collections
const collections = await client.replay.collections.list().first(50);

// Rename
await client.replay.collections.rename(collectionId, "New Name");

// Delete
await client.replay.collections.delete(collectionId);
```

#### Sending Requests (Replay)

The `send()` method is the core replay mechanism. It:
1. Updates the draft on the session's active entry
2. Starts a replay task
3. Subscribes to task completion
4. Returns the result with the response

```typescript
const result = await client.replay.send(sessionId, {
  raw: "GET /api/v1/users/123 HTTP/1.1\r\nHost: api.example.com\r\nCookie: session=abc\r\n\r\n",
  connection: {
    host: "api.example.com",
    port: 443,
    isTLS: true,
    SNI: "api.example.com",  // Optional
  },
  settings: {
    connectionClose: false,
    updateContentLength: true,
    placeholders: [],  // For parameterized fuzzing
  },
});

// Result:
// result.status: "DONE" | "CANCELLED" | "ERROR"
// result.error?: { code: string }
// result.entry: ReplayEntry with full request/response data
console.log(result.entry.response?.statusCode);
console.log(new TextDecoder().decode(result.entry.response?.raw));
```

#### Entries

```typescript
// Get a specific entry
const entry = await client.replay.entries.get(entryId);
// entry.id, entry.createdAt, entry.error, entry.raw,
// entry.connection, entry.request, entry.response, entry.sessionId
```

### 4.6 EnvironmentSDK -- `client.environment`

Manage environment variables (for tokens, API keys, base URLs).

```typescript
// Create an environment with variables
const env = await client.environment.create({
  name: "Production",
  variables: [
    { name: "API_BASE_URL", value: "https://api.yandex.ru", kind: "PLAIN" },
    { name: "AUTH_TOKEN", value: "Bearer xxx", kind: "SECRET" },
  ],
});

// Get an environment instance (stateful helper)
const envInstance = await client.environment.get(envId);

// Add a variable
await envInstance.addVariable({ name: "NEW_VAR", value: "val", kind: "PLAIN" });

// Update a variable
await envInstance.updateVariable("AUTH_TOKEN", { value: "Bearer new_token" });

// Delete a variable
await envInstance.deleteVariable("OLD_VAR");

// List all environments
const envs = await client.environment.list();

// Select active environment
await client.environment.select(envId);

// Deselect current environment
await client.environment.select(undefined);

// Update environment (requires version for optimistic concurrency)
await client.environment.update(envId, {
  name: "Staging",
  variables: [...],
  version: currentVersion,
});

// Delete
await client.environment.delete(envId);
```

### 4.7 FilterSDK -- `client.filter`

Manage reusable filter presets (saved HTTPQL queries).

```typescript
// Create a filter preset
const filter = await client.filter.create({
  name: "API Endpoints Only",
  alias: "api-only",  // Short name for use in HTTPQL as preset:"api-only"
  clause: 'req.path.cont:"/api/" AND resp.code.gte:200 AND resp.code.lt:300',
  kind: FilterClauseKind.HTTPQL,  // or FilterClauseKind.StreamQL for WebSocket
  global: false,  // true = available across all projects
});

// List all filter presets
const filters = await client.filter.list();

// Get by ID
const filter = await client.filter.get(filterId);

// Update
await client.filter.update(filterId, {
  name: "Updated Filter",
  alias: "updated",
  clause: 'req.host.cont:"yandex"',
  global: true,
});

// Delete
await client.filter.delete(filterId);
```

### 4.8 WorkflowSDK -- `client.workflow`

Full workflow CRUD, testing, and execution.

```typescript
// List all workflows
const workflows = await client.workflow.list();

// Get a workflow by ID
const workflow = await client.workflow.get(workflowId);

// Create a workflow (definition is the YAML/JSON workflow spec)
const workflow = await client.workflow.create({
  definition: workflowDefinitionString,
  global: true,  // Available across all projects
});

// Update workflow definition
await client.workflow.update(workflowId, {
  definition: newDefinitionString,
});

// Toggle enabled/disabled
await client.workflow.toggle(workflowId, true);

// Delete
await client.workflow.delete(workflowId);

// Test a workflow without saving (dry run)
// For passive workflows:
const result = await client.workflow.test({
  kind: "passive",
  definition: "...",
  request: {
    connection: { host: "example.com", port: 443, isTLS: true },
    raw: new TextEncoder().encode("GET / HTTP/1.1\r\n..."),
  },
  response: { raw: new TextEncoder().encode("HTTP/1.1 200 OK\r\n...") },
});

// For convert workflows:
const result = await client.workflow.test({
  kind: "convert",
  definition: "...",
  data: new TextEncoder().encode("input data"),
});

// Run a convert workflow
const result = await client.workflow.run({
  kind: "convert",
  id: workflowId,
  data: new TextEncoder().encode("input"),
});

// Run an active workflow (returns a task handle)
const task = await client.workflow.run({
  kind: "active",
  id: workflowId,
  requestId: someRequestId,
});
```

### 4.9 HostedFileSDK -- `client.hostedFile`

Upload wordlists and payload files for use in Automate/fuzzing.

```typescript
// Upload a file
const file = await client.hostedFile.upload({
  name: "xss-payloads.txt",
  file: new Blob(["<script>alert(1)</script>\n<img onerror=alert(1)>"]),
});

// List all hosted files
const files = await client.hostedFile.list();

// Rename
await client.hostedFile.rename(fileId, "renamed.txt");

// Delete
await client.hostedFile.delete(fileId);
```

### 4.10 PluginSDK -- `client.plugin`

Install plugins and call their backend functions.

```typescript
// Install a plugin from the store
const pkg = await client.plugin.install({ manifestId: "quickssrf" });

// Install from file
const pkg = await client.plugin.install({
  file: new File([buffer], "plugin.zip"),
  force: true,
});

// Get an installed plugin package
const pkg = await client.plugin.pluginPackage("quickssrf");

// Call a backend function
const result = await pkg.callFunction({
  name: "checkUrl",
  arguments: ["https://example.com"],
});

// Shorthand (via Proxy):
const result = await pkg.checkUrl("https://example.com");

// Subscribe to plugin events
for await (const [data] of pkg.subscribeEvent("my-event")) {
  console.log(data);
}
```

### 4.11 InstanceSDK -- `client.instance`

Certificate and settings management.

```typescript
// Export CA certificate (PKCS#12 bundle)
const p12 = await client.instance.certificate.export("password");

// Import a CA certificate
await client.instance.certificate.import({
  file: certBlob,
  password: "cert_password",
});

// Generate new CA certificate
await client.instance.certificate.generate();

// Get instance settings
const settings = await client.instance.settings.get();

// Set AI provider (Caido's built-in AI assistant)
await client.instance.settings.setAI({
  anthropic: { apiKey: "sk-ant-..." },
});
// Supports: anthropic, google, openai, openrouter

// Set analytics settings
await client.instance.settings.setAnalytics({ ... });
```

### 4.12 DNS SDKs

```typescript
// Create DNS upstream resolver
const upstream = await client.dnsUpstream.create({
  name: "Custom DNS",
  ip: "8.8.8.8",
});

// List DNS upstreams
const upstreams = await client.dnsUpstream.list();

// Create DNS rewrite rule
const rewrite = await client.dnsRewrite.create({
  allowlist: ["*.internal.example.com"],
  denylist: [],
  resolution: { kind: "ip", ip: "192.168.1.100" },
  // Or: resolution: { kind: "upstream", upstreamId: upstream.id }
});
```

### 4.13 UserSDK -- `client.user`

```typescript
const viewer = await client.user.viewer();
// viewer.kind: "CloudUser" | "GuestUser" | "ScriptUser"
// viewer.id: ID
// For CloudUser: viewer.profile (user profile info)
```

### 4.14 TaskSDK (internal, accessed via replay/workflow)

```typescript
// List running tasks
const tasks = await client.task.list();  // Not directly exposed; internal

// Cancel a task
await task.cancel();

// Subscribe to finished tasks
for await (const result of tasks.finished()) {
  console.log(result.status, result.task.id);
}
```

### 4.15 Pagination Pattern (ListBuilder)

All list methods return a `ListBuilder` that implements `PromiseLike`:

```typescript
// Basic pagination
const page1 = await client.request.list().first(50);

// page1.edges: Array<{ cursor, node }>
// page1.pageInfo: { hasNextPage, hasPreviousPage, startCursor, endCursor }

// Get next page
const page2 = await page1.next();

// Get previous page
const prev = await page1.prev();

// Chain filters, ordering, pagination
const results = await client.request.list()
  .filter('req.host.cont:"api" AND resp.code.eq:200')
  .descending("req", "created_at")
  .scope(scopeId)
  .includeRaw(false)
  .first(100);

// Iterate through all pages
let page = await client.request.list().first(100);
while (page) {
  for (const edge of page.edges) {
    process(edge.node);
  }
  page = await page.next();
}
```

---

## 5. HTTPQL Query Language Reference

HTTPQL is Caido's query language for filtering HTTP traffic. It is used in:
- The HTTP History UI
- The Search UI
- SDK `client.request.list().filter(...)` calls
- Filter presets
- Export filtering

### Syntax

```
<namespace>.<field>.<operator>:"<value>"
```

### Namespaces

| Namespace | Purpose |
|-----------|---------|
| `req` | Request fields |
| `resp` | Response fields |
| `row` | Row identifiers in tables |
| `preset` | Reference saved filter presets |
| `source` | Request source (Search UI only) |

### Request Fields (`req`)

| Field | Type | Description |
|-------|------|-------------|
| `created_at` | DateTime | Timestamp (RFC3339, ISO 8601, RFC2822, etc.) |
| `ext` | String/Byte | File extension (include the dot) |
| `host` | String/Byte | Host header value |
| `len` | Integer | Request size in bytes |
| `method` | String/Byte | HTTP method |
| `path` | String/Byte | URL path including filename |
| `port` | Integer | Target server port |
| `query` | String/Byte | Query string (without `?`) |
| `raw` | String/Byte | Full raw request data |
| `tls` | Boolean | Whether connection uses TLS |
| `header` | String/Byte | All headers (name or value) |
| `header["name"]` | String/Byte | Specific header value by name |
| `body` | String/Byte | Request body only |

### Response Fields (`resp`)

| Field | Type | Description |
|-------|------|-------------|
| `code` | Integer | Status code |
| `len` | Integer | Response size in bytes |
| `raw` | String/Byte | Full raw response data |
| `roundtrip` | Integer | Round-trip time in milliseconds |
| `header` | String/Byte | All headers |
| `header["name"]` | String/Byte | Specific header value |
| `body` | String/Byte | Response body only |

### Row Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Row number in traffic table |

### Operators

| Operator | Full Name | Applicable Types | Notes |
|----------|-----------|------------------|-------|
| `eq` | Equal | String, Integer | Case sensitive |
| `ne` | Not equal | String, Integer | Case sensitive |
| `cont` | Contains | String | **Case insensitive** |
| `ncont` | Not contains | String | Case insensitive |
| `like` | SQL LIKE | String | `%` = wildcard, `_` = single char |
| `nlike` | Not LIKE | String | |
| `regex` | Regex match | String | Rust regex flavor |
| `nregex` | Not regex | String | |
| `gt` | Greater than | Integer, DateTime | |
| `gte` | Greater or equal | Integer | |
| `lt` | Less than | Integer, DateTime | |
| `lte` | Less or equal | Integer | |

### Logical Operators

- `AND` -- both clauses must match (higher precedence)
- `OR` -- either clause matches
- Parentheses `()` for explicit grouping

### Examples

```sql
-- Basic host filtering
req.host.eq:"api.yandex.ru"
req.host.cont:"yandex"
req.host.like:"%.yandex.%"

-- Method + path
req.method.eq:"POST" AND req.path.cont:"/api/"

-- Status code ranges
resp.code.gte:400 AND resp.code.lt:500

-- Header-specific filtering
req.header["Authorization"].cont:"Bearer"
resp.header["Content-Type"].cont:"application/json"

-- Body content
req.body.cont:"password"
resp.body.regex:"\"error\":\\s*\"[^\"]+\""

-- Time-based
req.created_at.gt:"2026-09-01T00:00:00Z"

-- Complex queries
(req.host.cont:"yandex" OR req.host.cont:"ya.ru")
  AND req.method.eq:"POST"
  AND resp.code.eq:200
  AND resp.body.cont:"token"

-- Using presets
preset:"api-only"

-- Source filtering (Search UI only)
source.eq:"replay"
source.eq:"intercept"

-- Shorthand: plain string searches both req and resp raw
"password"
-- Expands to: (req.raw.cont:"password" OR resp.raw.cont:"password")

-- Comments
req.host.eq:"example.com" // This is a comment
/* Multi-line
   comment */
```

---

## 6. Project Management & Plan Limits

### Plan Tier Limits

| Feature | Basic (Free) | Individual | Team | Enterprise |
|---------|-------------|------------|------|------------|
| **Projects** | 2 | Unlimited | Unlimited | Unlimited |
| **Workflows** | 7 | Unlimited | Unlimited | Unlimited |
| **Plugins** | 3 | Unlimited | Unlimited | Unlimited |
| **Filter Presets** | 5 | Unlimited | Unlimited | Unlimited |
| **Installations** | - | Unlimited | Unlimited | Unlimited |
| **AI Assistant** | No | Yes | Yes | Yes |
| **Nightly Builds** | No | Yes | Yes | Yes |
| **Export Current Rows** | No | Yes | Yes | Yes |
| **Shared Instances** | No | No | Yes | Yes |
| **Registration Keys** | No | No | Yes | Yes |

### Project Management Strategy (2-Project Limit on Free Tier)

The SDK gives us full CRUD on projects, so the 2-project limit is manageable:

**Strategy: Rotate projects per program**

```typescript
// Before starting a new hunt, check current projects
const projects = await client.project.list();

if (projects.length >= 2) {
  // Option A: Delete the oldest/completed project
  const oldest = projects.sort((a, b) =>
    a.updatedAt.getTime() - b.updatedAt.getTime()
  )[0];
  await client.project.delete(oldest.id);

  // Option B: Use temporary projects that auto-clean
  // (not ideal -- data lost on close)
}

// Create project for current hunt
const huntProject = await client.project.create({
  name: `hunt-${programName}-${Date.now()}`,
  temporary: false,
});
await client.project.select(huntProject.id);

// Set up scope for this program
await client.scope.create({
  name: `${programName}-scope`,
  allowlist: programDomains,
  denylist: outOfScopePatterns,
});
```

**Alternative strategy: Single project, multiple scopes**

Use one persistent project per "campaign type" (e.g., one for web, one for mobile),
and manage different programs through scopes and filter presets within it:

```typescript
// Create per-program scopes within a single project
const yandexScope = await client.scope.create({
  name: "yandex",
  allowlist: ["*.yandex.ru", "*.ya.ru"],
  denylist: [],
});

const hackeroneScope = await client.scope.create({
  name: "hackerone-target",
  allowlist: ["*.target.com"],
  denylist: [],
});

// Query requests filtered by scope
const yandexReqs = await client.request.list()
  .scope(yandexScope.id)
  .first(100);
```

This is the recommended approach for the free tier -- it avoids the 2-project
limit entirely while still keeping traffic organized.

### Export Capabilities

Caido supports exporting request data in:
- **JSON** -- Full request/response data, base64-encoded bodies
- **CSV** -- Tabular request metadata
- Exports can be filtered by HTTPQL before generation
- Export of "current rows" (filtered view) requires Individual+ tier

Note: There is no HAR export natively. The JSON export format is Caido-specific.
For HAR, you would need to build a converter from the SDK's request data.

---

## 7. GraphQL API Access

### Direct Access

The SDK wraps GraphQL, but you can also call it directly:

```typescript
// Execute any GraphQL query
const result = await client.graphql.query(someQueryDocument, variables);

// Execute mutations
const result = await client.graphql.mutation(someMutationDocument, variables);

// Subscribe to real-time events
for await (const event of client.graphql.subscribe(someSubscriptionDocument)) {
  console.log(event);
}
```

### GraphQL Playground

Access the interactive GraphQL playground:
- URL: `http://localhost:8080/graphql`
- Visual schema explorer: `https://graphql-explorer.caido.io`

Authentication for the playground:
```javascript
// Get token from browser console (when logged into Caido UI):
JSON.parse(localStorage.CAIDO_AUTHENTICATION).accessToken;

// Set in playground headers:
// { "Authorization": "Bearer <token>" }
```

Access tokens expire after 7 days.

### Key GraphQL Operations Used by SDK

**Queries:**
- `Projects` -- List projects
- `Scopes` / `Scope` -- List/get scopes
- `Requests` / `Request` -- List/get HTTP requests (supports HTTPQL filter)
- `Findings` / `Finding` -- List/get findings
- `ReplaySessions` / `ReplaySession` -- List/get replay sessions
- `ReplaySessionCollections` -- List replay collections
- `ReplayEntry` -- Get a replay entry with full request/response
- `Workflows` / `Workflow` -- List/get workflows
- `Environments` / `Environment` -- List/get environments
- `FilterPresets` / `FilterPreset` -- List/get filter presets
- `HostedFiles` -- List hosted files
- `Tasks` -- List running tasks
- `PluginPackages` -- List installed plugins
- `Viewer` -- Get current user
- `InstanceSettings` -- Get instance settings
- `DnsUpstreams` -- List DNS upstreams

**Mutations:**
- `CreateProject` / `DeleteProject` / `RenameProject` / `SelectProject`
- `CreateScope` / `UpdateScope` / `DeleteScope`
- `CreateFinding` / `UpdateFinding` / `DeleteFindings`
- `CreateReplaySession` / `DeleteReplaySessions` / `RenameReplaySession` / `MoveReplaySession`
- `CreateReplaySessionCollection` / `DeleteReplaySessionCollection` / `RenameReplaySessionCollection`
- `StartReplayTask` / `UpdateReplayEntryDraft` / `UpdateReplaySessionSettings`
- `CreateWorkflow` / `UpdateWorkflow` / `DeleteWorkflow` / `ToggleWorkflow`
- `TestWorkflowPassive` / `TestWorkflowActive` / `TestWorkflowConvert`
- `RunConvertWorkflow` / `RunActiveWorkflow`
- `CreateEnvironment` / `UpdateEnvironment` / `DeleteEnvironment` / `SelectEnvironment`
- `CreateFilterPreset` / `UpdateFilterPreset` / `DeleteFilterPreset`
- `UploadHostedFile` / `RenameHostedFile` / `DeleteHostedFile`
- `InstallPluginPackage`
- `CancelTask`
- `ImportCertificate` / `RegenerateCertificate`
- `CreateDnsRewrite` / `CreateDnsUpstream`
- `SetInstanceSettings`
- `CreateRequest`

**Subscriptions:**
- `FinishedTask` -- Notified when a task completes (used by replay.send())

The GraphQL schema is **intentionally public** but **not guaranteed stable** across releases.

---

## 8. Integration Patterns for Hutch

### 8.1 Core Integration: Playwright Through Caido

```typescript
import { chromium } from "playwright";
import { Client } from "@caido/sdk-client";

// 1. Connect to Caido SDK
const caido = new Client({
  url: "http://localhost:8080",
  auth: { pat: process.env.CAIDO_PAT },
});
await caido.connect();

// 2. Set up project and scope for this hunt
const project = await caido.project.create({ name: "hunt-session", temporary: false });
await caido.project.select(project.id);

const scope = await caido.scope.create({
  name: "target-scope",
  allowlist: ["*.target.com"],
  denylist: [],
});

// 3. Launch browser through Caido's proxy
const browser = await chromium.launch({
  proxy: {
    server: "http://127.0.0.1:8080",  // Caido proxy port
  },
});

const context = await browser.newContext({
  ignoreHTTPSErrors: true,  // Trust Caido's CA cert
});

// 4. Browse -- all traffic captured in Caido
const page = await context.newPage();
await page.goto("https://target.com");

// 5. Query captured traffic via SDK
const requests = await caido.request.list()
  .scope(scope.id)
  .filter('req.method.eq:"POST" AND resp.code.eq:200')
  .descending("req", "created_at")
  .first(50);
```

### 8.2 Replay for Validation

```typescript
// After finding a suspicious request in history, replay it to validate
async function validateFinding(caido: Client, requestId: ID) {
  // Create a replay session from the captured request
  const session = await caido.replay.sessions.create({
    requestSource: { id: requestId },
    collectionId: validationCollectionId,
  });

  // Get the original request to modify it
  const original = await caido.request.get(requestId);
  const rawStr = new TextDecoder().decode(original.request.raw);

  // Modify and replay (e.g., change a parameter for IDOR test)
  const modified = rawStr.replace("user_id=123", "user_id=456");

  const result = await caido.replay.send(session.id, {
    raw: modified,
    connection: {
      host: original.request.host,
      port: original.request.port,
      isTLS: original.request.isTls,
    },
  });

  if (result.status === "DONE" && result.entry.response) {
    const responseBody = new TextDecoder().decode(result.entry.response.raw);
    // Check if we got user 456's data
    if (responseBody.includes('"user_id":456')) {
      // Confirmed IDOR -- create a finding
      await caido.finding.create(requestId, {
        title: "IDOR: Unauthorized access to user data",
        reporter: "hutch-validator",
        description: `Replayed request with user_id=456, got valid response.`,
        dedupeKey: `idor-user-data-${original.request.path}`,
      });
    }
  }
}
```

### 8.3 Session Management for Hutch

```typescript
class HutchCaidoSession {
  private caido: Client;
  private projectId: ID;
  private scopeId: ID;
  private collectionId: ID;

  async initialize(programName: string, domains: string[]) {
    this.caido = new Client({
      url: process.env.CAIDO_URL || "http://localhost:8080",
      auth: { pat: process.env.CAIDO_PAT },
    });
    await this.caido.connect();

    // Use existing project or create
    const projects = await this.caido.project.list();
    let project = projects.find(p => p.name === programName);
    if (!project) {
      // On free tier, check limit
      if (projects.length >= 2) {
        // Find and select existing; use scopes to separate programs
        project = projects[0];
      } else {
        project = await this.caido.project.create({
          name: programName,
          temporary: false,
        });
      }
    }
    await this.caido.project.select(project.id);
    this.projectId = project.id;

    // Create scope
    const scope = await this.caido.scope.create({
      name: `${programName}-scope`,
      allowlist: domains,
      denylist: [],
    });
    this.scopeId = scope.id;

    // Create replay collection for this session
    const collection = await this.caido.replay.collections.create({
      name: `${programName}-${new Date().toISOString().slice(0, 10)}`,
    });
    this.collectionId = collection.id;
  }

  async queryHistory(httpql: string, limit = 100) {
    return this.caido.request.list()
      .scope(this.scopeId)
      .filter(httpql)
      .descending("req", "created_at")
      .first(limit);
  }

  async replayRequest(requestId: ID, modifications?: (raw: string) => string) {
    const original = await this.caido.request.get(requestId);
    if (!original) throw new Error("Request not found");

    const session = await this.caido.replay.sessions.create({
      requestSource: { id: requestId },
      collectionId: this.collectionId,
    });

    let raw = new TextDecoder().decode(original.request.raw);
    if (modifications) raw = modifications(raw);

    return this.caido.replay.send(session.id, {
      raw,
      connection: {
        host: original.request.host,
        port: original.request.port,
        isTLS: original.request.isTls,
      },
    });
  }

  async createFinding(requestId: ID, title: string, description: string) {
    return this.caido.finding.create(requestId, {
      title,
      reporter: "hutch",
      description,
      dedupeKey: `hutch-${title.toLowerCase().replace(/\s+/g, "-")}`,
    });
  }
}
```

### 8.4 Certificate Trust for Playwright

For HTTPS interception, Playwright needs to trust Caido's CA:

```typescript
// Option 1: ignoreHTTPSErrors (simple, recommended for testing)
const context = await browser.newContext({
  ignoreHTTPSErrors: true,
  proxy: { server: "http://127.0.0.1:8080" },
});

// Option 2: Export and trust the CA cert
const p12 = await caido.instance.certificate.export("password");
// Write to file and convert to PEM for system trust
// Then set PLAYWRIGHT_EXTRA_CA_CERTS env var
```

### 8.5 Environment Variables for Auth Rotation

```typescript
// Store auth tokens as Caido environment variables
// These can be used in workflows and match-and-replace rules
const env = await caido.environment.create({
  name: "Target Auth",
  variables: [
    { name: "SESSION_A", value: "user_a_cookie=abc123", kind: "SECRET" },
    { name: "SESSION_B", value: "user_b_cookie=xyz789", kind: "SECRET" },
  ],
});
await caido.environment.select(env.id);
```

---

## Key Findings and Recommendations

### What Works Well
1. **Full programmatic control** -- The SDK covers essentially every UI operation
2. **HTTPQL is powerful** -- Rich query language for filtering captured traffic
3. **Replay is first-class** -- `send()` handles the full lifecycle (draft update, task start, wait for completion)
4. **Findings system** -- Built-in dedup and reporter tracking fits our validation gate
5. **Scopes map perfectly** to bug bounty program scope definitions
6. **Cursor-based pagination** -- Efficient for large datasets
7. **GraphQL subscriptions** -- Real-time task completion notifications

### Limitations to Plan For
1. **2-project limit on free tier** -- Use single-project + multiple scopes strategy
2. **No HAR export** -- Build a converter if needed
3. **GraphQL schema instability** -- SDK may break across Caido versions; pin versions
4. **Registration keys require Teams plan** -- For headless automation at scale
5. **Export current rows requires Individual+ tier** -- Use SDK to query + export instead
6. **No native "interceptor API"** -- Cannot programmatically pause/modify in-flight
   requests via SDK (that's UI-only intercept toggling)

### Version Considerations
The SDK has transport versioning (V0_56, V0_57) and automatically adapts to the
connected Caido instance version. Pin `@caido/sdk-client` to a specific version
and test against the Caido CLI version you deploy.

---

## Sources

- [Caido Official Docs](https://docs.caido.io/)
- [Caido CLI Reference](https://docs.caido.io/app/reference/cli)
- [HTTPQL Reference](https://docs.caido.io/app/reference/httpql)
- [HTTPQL Query Guide](https://docs.caido.io/app/guides/filters_httpql)
- [Headless Orchestration Tutorial](https://docs.caido.io/app/tutorials/headless_orchestration)
- [Caido Pricing](https://www.caido.io/pricing/)
- [Project & Configuration](https://docs.caido.io/burp-suite/core/project-and-configuration)
- [Caido GraphQL Concepts](https://docs.caido.io/app/concepts/graphql)
- [GraphQL Explorer](https://graphql-explorer.caido.io)
- [Export Guide](https://docs.caido.io/app/guides/exports_requests)
- [SDK Client GitHub](https://github.com/caido/sdk-js/tree/main/packages/sdk-client)
- [SDK Client on npm](https://libraries.io/npm/@caido/sdk-client)
- [Developer Docs - Client SDK Guides](https://developer.caido.io/client-sdk/guides/)
- [Developer Docs - Scope Management](https://developer.caido.io/guides/scopes.html)
- [Developer Docs - Replay Management](https://developer.caido.io/plugins/guides/replay.html)
- [Developer Docs - Workflow Interaction](https://developer.caido.io/guides/workflows.html)
- [Developer Docs - Authentication](https://developer.caido.io/reference/authentication.html)
- [Caido Skills (caido-mode)](https://github.com/caido/skills/tree/main/skills/caido-mode)
- [Caido HTTPQL GitHub](https://github.com/caido/httpql)
- [Bugcrowd Beginner's Guide to Caido](https://www.bugcrowd.com/blog/the-ultimate-beginners-guide-to-caido/)
