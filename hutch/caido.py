import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

import aiohttp


_TOKEN_FILE = os.path.expanduser("~/.caido-mcp/token.json")


@dataclass
class CaidoConfig:
    url: str = "http://127.0.0.1:8080"
    token: Optional[str] = None
    token_file: str = _TOKEN_FILE
    allow_sensitive_headers: bool = False


class CaidoError(Exception):
    pass


class CaidoClient:

    def __init__(self, config=None):
        self.config = config or CaidoConfig()
        self._session = None
        self._token = None

    async def connect(self):
        self._token = self._resolve_token()
        self._session = aiohttp.ClientSession()
        info = await self.instance_info()
        return info

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def _resolve_token(self):
        if self.config.token:
            return self.config.token
        if t := os.environ.get("CAIDO_ACCESS_TOKEN"):
            return t
        if os.path.exists(self.config.token_file):
            with open(self.config.token_file) as f:
                data = json.load(f)
            return (data.get("accessToken")
                    or data.get("access_token")
                    or data.get("token"))
        return None

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _gql(self, query, variables=None):
        if not self._session:
            raise CaidoError("not connected — call connect() first")
        url = urljoin(self.config.url, "/graphql")
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        async with self._session.post(url, json=payload,
                                       headers=self._headers()) as resp:
            if resp.status == 401:
                raise CaidoError("authentication failed — check token")
            if resp.status != 200:
                text = await resp.text()
                raise CaidoError(f"graphql error {resp.status}: {text}")
            data = await resp.json()
            if errors := data.get("errors"):
                raise CaidoError(f"graphql: {errors[0]['message']}")
            return data.get("data", {})

    # --- instance ---

    async def instance_info(self):
        data = await self._gql("{ runtime { version platform } }")
        return data.get("runtime", {})

    # --- projects ---

    async def list_projects(self):
        data = await self._gql("""
            { projects { id name status path size version } }
        """)
        return data.get("projects", [])

    async def create_project(self, name, *, temporary=False):
        data = await self._gql("""
            mutation($input: CreateProjectInput!) {
                createProject(input: $input) { project { id name status } }
            }
        """, {"input": {"name": name, "temporary": temporary}})
        return data["createProject"]["project"]

    async def select_project(self, project_id):
        data = await self._gql("""
            mutation($id: ID!) {
                selectProject(id: $id) { project { id name } }
            }
        """, {"id": project_id})
        return data["selectProject"]["project"]

    async def delete_project(self, project_id):
        await self._gql("""
            mutation($id: ID!) { deleteProject(id: $id) { deletedId } }
        """, {"id": project_id})

    async def rename_project(self, project_id, name):
        data = await self._gql("""
            mutation($id: ID!, $name: String!) {
                renameProject(id: $id, input: { name: $name }) {
                    project { id name }
                }
            }
        """, {"id": project_id, "name": name})
        return data["renameProject"]["project"]

    async def ensure_project(self, name, *, max_projects=2):
        projects = await self.list_projects()
        for p in projects:
            if p["name"] == name:
                await self.select_project(p["id"])
                return p
        if len(projects) >= max_projects:
            raise CaidoError(
                f"at project limit ({max_projects}); "
                f"existing: {[p['name'] for p in projects]}. "
                f"delete one first or use an existing project"
            )
        project = await self.create_project(name)
        await self.select_project(project["id"])
        return project

    # --- scopes ---

    async def list_scopes(self):
        data = await self._gql("""
            { scopes { id name allowlist denylist } }
        """)
        return data.get("scopes", [])

    async def create_scope(self, name, allowlist, denylist=None):
        data = await self._gql("""
            mutation($input: CreateScopeInput!) {
                createScope(input: $input) { scope { id name allowlist denylist } }
            }
        """, {"input": {
            "name": name,
            "allowlist": allowlist,
            "denylist": denylist or [],
        }})
        return data["createScope"]["scope"]

    async def update_scope(self, scope_id, *, name=None, allowlist=None,
                           denylist=None):
        inp = {}
        if name is not None:
            inp["name"] = name
        if allowlist is not None:
            inp["allowlist"] = allowlist
        if denylist is not None:
            inp["denylist"] = denylist
        data = await self._gql("""
            mutation($id: ID!, $input: UpdateScopeInput!) {
                updateScope(id: $id, input: $input) {
                    scope { id name allowlist denylist }
                }
            }
        """, {"id": scope_id, "input": inp})
        return data["updateScope"]["scope"]

    async def delete_scope(self, scope_id):
        await self._gql("""
            mutation($id: ID!) { deleteScope(id: $id) { deletedId } }
        """, {"id": scope_id})

    async def is_in_scope(self, url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ""
        scopes = await self.list_scopes()
        for scope in scopes:
            for pattern in scope.get("allowlist", []):
                if self._host_matches(host, pattern):
                    for deny in scope.get("denylist", []):
                        if self._host_matches(host, deny):
                            return False
                    return True
        return False

    @staticmethod
    def _host_matches(host, pattern):
        if pattern.startswith("*."):
            suffix = pattern[1:]
            return host.endswith(suffix) or host == pattern[2:]
        return host == pattern

    async def ensure_scope(self, name, allowlist, denylist=None):
        scopes = await self.list_scopes()
        for s in scopes:
            if s["name"] == name:
                if s["allowlist"] != allowlist or s.get("denylist", []) != (denylist or []):
                    return await self.update_scope(
                        s["id"], allowlist=allowlist, denylist=denylist)
                return s
        return await self.create_scope(name, allowlist, denylist)

    # --- requests (history) ---

    async def list_requests(self, *, httpql=None, scope_id=None,
                            first=50, after=None):
        variables = {"first": first}
        var_defs = ["$first: Int!"]
        args = []

        if httpql:
            variables["filter"] = {"code": httpql}
            var_defs.append("$filter: HTTPQLInput")
            args.append("filter: $filter")
        if scope_id:
            variables["scopeId"] = scope_id
            var_defs.append("$scopeId: ID")
            args.append("scopeId: $scopeId")
        if after:
            variables["after"] = after
            var_defs.append("$after: String")
            args.append("after: $after")

        var_str = ", ".join(var_defs)
        arg_str = ", ".join(args + ["first: $first",
                                     "order: { by: CREATED_AT, ordering: DESC }"])

        data = await self._gql(f"""
            query({var_str}) {{
                requests({arg_str}) {{
                    edges {{
                        node {{
                            id
                            method
                            host
                            port
                            path
                            query
                            isTls
                            length
                            createdAt
                            response {{
                                id
                                statusCode
                                length
                                roundtripTime
                            }}
                        }}
                        cursor
                    }}
                    pageInfo {{ hasNextPage endCursor }}
                }}
            }}
        """, variables)
        result = data.get("requests", {})
        return {
            "items": [e["node"] for e in result.get("edges", [])],
            "next_cursor": result.get("pageInfo", {}).get("endCursor"),
            "has_more": result.get("pageInfo", {}).get("hasNextPage", False),
        }

    async def get_request(self, request_id):
        data = await self._gql("""
            query($id: ID!) {
                request(id: $id) {
                    id method host port path query isTls
                    raw length createdAt
                    response {
                        id statusCode raw length roundtripTime
                        headers { key value }
                    }
                }
            }
        """, {"id": request_id})
        return data.get("request")

    # --- findings ---

    async def list_findings(self, *, first=50):
        data = await self._gql("""
            query($first: Int!) {
                findings(first: $first) {
                    edges {
                        node {
                            id title description reporter
                            request { id method host path }
                            createdAt
                        }
                    }
                }
            }
        """, {"first": first})
        return [e["node"] for e in data.get("findings", {}).get("edges", [])]

    async def create_finding(self, request_id, title, *,
                             description="", reporter="hutch",
                             dedupe_key=None):
        inp = {
            "title": title,
            "requestId": request_id,
            "description": description,
            "reporter": reporter,
        }
        if dedupe_key:
            inp["dedupeKey"] = dedupe_key
        data = await self._gql("""
            mutation($input: CreateFindingInput!) {
                createFinding(input: $input) {
                    finding { id title reporter createdAt }
                }
            }
        """, {"input": inp})
        return data["createFinding"]["finding"]

    async def delete_findings(self, finding_ids):
        await self._gql("""
            mutation($ids: [ID!]!) {
                deleteFindings(ids: $ids) { deletedIds }
            }
        """, {"ids": finding_ids})

    # --- replay ---

    async def create_replay_session(self, *, name=None, request_id=None,
                                     collection_id=None):
        inp = {}
        if request_id:
            inp["requestSource"] = {"id": request_id}
        if collection_id:
            inp["collectionId"] = collection_id
        data = await self._gql("""
            mutation($input: CreateReplaySessionInput!) {
                createReplaySession(input: $input) {
                    session { id name }
                }
            }
        """, {"input": inp})
        session = data["createReplaySession"]["session"]
        if name:
            await self._gql("""
                mutation($id: ID!, $name: String!) {
                    renameReplaySession(id: $id, input: { name: $name }) {
                        session { id name }
                    }
                }
            """, {"id": session["id"], "name": name})
            session["name"] = name
        return session

    async def list_replay_sessions(self, *, first=50):
        data = await self._gql("""
            query($first: Int!) {
                replaySessions(first: $first) {
                    edges { node { id name } }
                }
            }
        """, {"first": first})
        return [e["node"] for e in
                data.get("replaySessions", {}).get("edges", [])]

    async def create_replay_collection(self, name):
        data = await self._gql("""
            mutation($input: CreateReplaySessionCollectionInput!) {
                createReplaySessionCollection(input: $input) {
                    collection { id name }
                }
            }
        """, {"input": {"name": name}})
        return data["createReplaySessionCollection"]["collection"]

    # --- environments ---

    async def list_environments(self):
        data = await self._gql("""
            { environments { id name variables { name value isSecret } } }
        """)
        return data.get("environments", [])

    async def create_environment(self, name, variables=None):
        inp = {"name": name}
        if variables:
            inp["variables"] = variables
        data = await self._gql("""
            mutation($input: CreateEnvironmentInput!) {
                createEnvironment(input: $input) {
                    environment { id name }
                }
            }
        """, {"input": inp})
        return data["createEnvironment"]["environment"]

    async def select_environment(self, env_id):
        await self._gql("""
            mutation($id: ID!) {
                selectEnvironment(id: $id) { environment { id name } }
            }
        """, {"id": env_id})

    # --- filters ---

    async def create_filter(self, name, httpql):
        data = await self._gql("""
            mutation($input: CreateFilterPresetInput!) {
                createFilterPreset(input: $input) {
                    filter { id name alias query }
                }
            }
        """, {"input": {"name": name, "query": httpql}})
        return data["createFilterPreset"]["filter"]

    # --- workflows ---

    async def list_workflows(self):
        data = await self._gql("""
            { workflows { id name kind enabled } }
        """)
        return data.get("workflows", [])

    async def toggle_workflow(self, workflow_id, enabled):
        await self._gql("""
            mutation($id: ID!, $enabled: Boolean!) {
                toggleWorkflow(id: $id, enabled: $enabled) {
                    workflow { id enabled }
                }
            }
        """, {"id": workflow_id, "enabled": enabled})

    # --- sitemap ---

    async def get_sitemap(self, *, scope_id=None):
        variables = {}
        scope_arg = ""
        if scope_id:
            variables["scopeId"] = scope_id
            scope_arg = "(scopeId: $scopeId)"
        data = await self._gql(f"""
            query{' ($scopeId: ID!)' if scope_id else ''} {{
                sitemap{scope_arg} {{
                    id label kind
                    children {{ id label kind }}
                }}
            }}
        """, variables or None)
        return data.get("sitemap", [])
