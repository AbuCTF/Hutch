import asyncio
import json
import os
import pytest
import pytest_asyncio
from aiohttp import web

from hutch.caido import CaidoClient, CaidoConfig, CaidoError


_MOCK_PROJECTS = [
    {"id": "p1", "name": "hunt-yandex", "status": "READY",
     "path": "/tmp/p1", "size": 1024, "version": "1"},
]
_MOCK_SCOPES = [
    {"id": "s1", "name": "yandex", "allowlist": ["*.yandex.ru"],
     "denylist": []},
]
_MOCK_REQUESTS = [
    {"id": "r1", "method": "GET", "host": "yandex.ru", "port": 443,
     "path": "/api/v1/user", "query": "", "isTls": True, "length": 200,
     "createdAt": "2026-09-01T00:00:00Z",
     "response": {"id": "rsp1", "statusCode": 200, "length": 500,
                   "roundtripTime": 150}},
]
_MOCK_FINDINGS = [
    {"id": "f1", "title": "IDOR", "description": "user data leak",
     "reporter": "hutch", "request": {"id": "r1", "method": "GET",
     "host": "yandex.ru", "path": "/api/v1/user"},
     "createdAt": "2026-09-01T00:00:00Z"},
]


async def _mock_graphql(request):
    body = await request.json()
    query = body.get("query", "")
    variables = body.get("variables", {})

    if "runtime" in query:
        return web.json_response({"data": {
            "runtime": {"version": "0.44.0", "platform": "linux"}}})

    if "projects" in query.lower() and "mutation" not in query.lower():
        return web.json_response({"data": {"projects": _MOCK_PROJECTS}})

    if "createProject" in query:
        name = variables.get("input", {}).get("name", "new")
        return web.json_response({"data": {"createProject": {
            "project": {"id": "p2", "name": name, "status": "READY"}}}})

    if "selectProject" in query:
        return web.json_response({"data": {"selectProject": {
            "project": {"id": variables["id"], "name": "selected"}}}})

    if "deleteProject" in query:
        return web.json_response({"data": {"deleteProject": {
            "deletedId": variables["id"]}}})

    if "renameProject" in query:
        return web.json_response({"data": {"renameProject": {
            "project": {"id": variables["id"], "name": variables["name"]}}}})

    if "scopes" in query.lower() and "mutation" not in query.lower():
        return web.json_response({"data": {"scopes": _MOCK_SCOPES}})

    if "createScope" in query:
        inp = variables.get("input", {})
        return web.json_response({"data": {"createScope": {"scope": {
            "id": "s2", "name": inp["name"],
            "allowlist": inp["allowlist"], "denylist": inp.get("denylist", [])}
        }}})

    if "updateScope" in query:
        return web.json_response({"data": {"updateScope": {"scope": {
            "id": variables["id"], "name": "updated",
            "allowlist": ["*.new.com"], "denylist": []}
        }}})

    if "deleteScope" in query:
        return web.json_response({"data": {"deleteScope": {
            "deletedId": variables["id"]}}})

    if "isInScope" in query:
        url = variables.get("url", "")
        return web.json_response({"data": {
            "isInScope": "yandex" in url}})

    if "requests" in query.lower() and "mutation" not in query.lower():
        return web.json_response({"data": {"requests": {
            "edges": [{"node": _MOCK_REQUESTS[0], "cursor": "c1"}],
            "pageInfo": {"hasNextPage": False, "endCursor": "c1"}
        }}})

    if "request(" in query.lower():
        return web.json_response({"data": {"request": {
            **_MOCK_REQUESTS[0],
            "raw": "R0VUIC9hcGkvdjEvdXNlcg==",
            "response": {**_MOCK_REQUESTS[0]["response"],
                         "raw": "SFRUUC8xLjEgMjAwIE9L",
                         "headers": [{"key": "Content-Type",
                                      "value": "application/json"}]}
        }}})

    if "findings" in query.lower() and "mutation" not in query.lower():
        return web.json_response({"data": {"findings": {
            "edges": [{"node": _MOCK_FINDINGS[0]}]
        }}})

    if "createFinding" in query:
        inp = variables.get("input", {})
        return web.json_response({"data": {"createFinding": {"finding": {
            "id": "f2", "title": inp["title"],
            "reporter": inp.get("reporter", "hutch"),
            "createdAt": "2026-09-02T00:00:00Z"
        }}}})

    if "deleteFindings" in query:
        return web.json_response({"data": {"deleteFindings": {
            "deletedIds": variables["ids"]}}})

    if "createReplaySessionCollection" in query:
        inp = variables.get("input", {})
        return web.json_response({"data": {
            "createReplaySessionCollection": {
                "collection": {"id": "rc1", "name": inp["name"]}}}})

    if "renameReplaySession" in query:
        return web.json_response({"data": {"renameReplaySession": {
            "session": {"id": variables["id"], "name": variables["name"]}}}})

    if "createReplaySession" in query:
        return web.json_response({"data": {"createReplaySession": {
            "session": {"id": "rs1", "name": "replay-1"}}}})

    if "replaySessions" in query:
        return web.json_response({"data": {"replaySessions": {
            "edges": [{"node": {"id": "rs1", "name": "replay-1"}}]
        }}})

    if "environments" in query.lower() and "mutation" not in query.lower():
        return web.json_response({"data": {"environments": [
            {"id": "e1", "name": "prod",
             "variables": [{"name": "TOKEN", "value": "xxx", "isSecret": True}]}
        ]}})

    if "createEnvironment" in query:
        inp = variables.get("input", {})
        return web.json_response({"data": {"createEnvironment": {
            "environment": {"id": "e2", "name": inp["name"]}}}})

    if "selectEnvironment" in query:
        return web.json_response({"data": {"selectEnvironment": {
            "environment": {"id": variables["id"], "name": "selected"}}}})

    if "createFilterPreset" in query:
        inp = variables.get("input", {})
        return web.json_response({"data": {"createFilterPreset": {
            "filter": {"id": "fp1", "name": inp["name"],
                       "alias": None, "query": inp["query"]}}}})

    if "workflows" in query.lower() and "mutation" not in query.lower():
        return web.json_response({"data": {"workflows": [
            {"id": "w1", "name": "passive-scan", "kind": "PASSIVE",
             "enabled": True}
        ]}})

    if "toggleWorkflow" in query:
        return web.json_response({"data": {"toggleWorkflow": {
            "workflow": {"id": variables["id"],
                         "enabled": variables["enabled"]}}}})

    if "sitemap" in query.lower():
        return web.json_response({"data": {"sitemap": [
            {"id": "sm1", "label": "yandex.ru", "kind": "DOMAIN",
             "children": [{"id": "sm2", "label": "/api", "kind": "PATH"}]}
        ]}})

    return web.json_response(
        {"errors": [{"message": f"unhandled query: {query[:80]}"}]},
        status=200)


@pytest_asyncio.fixture
async def mock_caido():
    app = web.Application()
    app.router.add_post("/graphql", _mock_graphql)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    config = CaidoConfig(url=f"http://127.0.0.1:{port}")
    client = CaidoClient(config)
    await client.connect()
    yield client
    await client.close()
    await runner.cleanup()


class TestConnection:

    @pytest.mark.asyncio
    async def test_connect(self, mock_caido):
        assert mock_caido._session is not None

    @pytest.mark.asyncio
    async def test_instance_info(self, mock_caido):
        info = await mock_caido.instance_info()
        assert info["version"] == "0.44.0"
        assert info["platform"] == "linux"


class TestProjects:

    @pytest.mark.asyncio
    async def test_list_projects(self, mock_caido):
        projects = await mock_caido.list_projects()
        assert len(projects) == 1
        assert projects[0]["name"] == "hunt-yandex"

    @pytest.mark.asyncio
    async def test_create_project(self, mock_caido):
        p = await mock_caido.create_project("new-hunt")
        assert p["name"] == "new-hunt"
        assert p["id"] == "p2"

    @pytest.mark.asyncio
    async def test_select_project(self, mock_caido):
        p = await mock_caido.select_project("p1")
        assert p["id"] == "p1"

    @pytest.mark.asyncio
    async def test_delete_project(self, mock_caido):
        await mock_caido.delete_project("p1")

    @pytest.mark.asyncio
    async def test_rename_project(self, mock_caido):
        p = await mock_caido.rename_project("p1", "renamed")
        assert p["name"] == "renamed"

    @pytest.mark.asyncio
    async def test_ensure_project_existing(self, mock_caido):
        p = await mock_caido.ensure_project("hunt-yandex")
        assert p["name"] == "hunt-yandex"

    @pytest.mark.asyncio
    async def test_ensure_project_new(self, mock_caido):
        p = await mock_caido.ensure_project("hunt-new")
        assert p["name"] == "hunt-new"


class TestScopes:

    @pytest.mark.asyncio
    async def test_list_scopes(self, mock_caido):
        scopes = await mock_caido.list_scopes()
        assert len(scopes) == 1
        assert scopes[0]["name"] == "yandex"

    @pytest.mark.asyncio
    async def test_create_scope(self, mock_caido):
        s = await mock_caido.create_scope("target", ["*.target.com"])
        assert s["name"] == "target"
        assert s["allowlist"] == ["*.target.com"]

    @pytest.mark.asyncio
    async def test_is_in_scope(self, mock_caido):
        assert await mock_caido.is_in_scope("https://yandex.ru/api")
        assert not await mock_caido.is_in_scope("https://google.com")

    @pytest.mark.asyncio
    async def test_ensure_scope_existing(self, mock_caido):
        s = await mock_caido.ensure_scope("yandex", ["*.yandex.ru"])
        assert s["name"] == "yandex"

    @pytest.mark.asyncio
    async def test_ensure_scope_new(self, mock_caido):
        s = await mock_caido.ensure_scope("new-target", ["*.new.com"])
        assert s["name"] == "new-target"


class TestRequests:

    @pytest.mark.asyncio
    async def test_list_requests(self, mock_caido):
        result = await mock_caido.list_requests()
        assert len(result["items"]) == 1
        assert result["items"][0]["host"] == "yandex.ru"
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_list_requests_with_filter(self, mock_caido):
        result = await mock_caido.list_requests(
            httpql='req.method.eq:"GET"', first=10)
        assert len(result["items"]) >= 1

    @pytest.mark.asyncio
    async def test_get_request(self, mock_caido):
        req = await mock_caido.get_request("r1")
        assert req["method"] == "GET"
        assert req["response"]["statusCode"] == 200


class TestFindings:

    @pytest.mark.asyncio
    async def test_list_findings(self, mock_caido):
        findings = await mock_caido.list_findings()
        assert len(findings) == 1
        assert findings[0]["title"] == "IDOR"

    @pytest.mark.asyncio
    async def test_create_finding(self, mock_caido):
        f = await mock_caido.create_finding(
            "r1", "XSS in search", description="reflected XSS")
        assert f["title"] == "XSS in search"
        assert f["reporter"] == "hutch"

    @pytest.mark.asyncio
    async def test_delete_findings(self, mock_caido):
        await mock_caido.delete_findings(["f1"])


class TestReplay:

    @pytest.mark.asyncio
    async def test_create_replay_session(self, mock_caido):
        s = await mock_caido.create_replay_session(request_id="r1")
        assert s["id"] == "rs1"

    @pytest.mark.asyncio
    async def test_create_replay_session_named(self, mock_caido):
        s = await mock_caido.create_replay_session(
            name="idor-test", request_id="r1")
        assert s["name"] == "idor-test"

    @pytest.mark.asyncio
    async def test_list_replay_sessions(self, mock_caido):
        sessions = await mock_caido.list_replay_sessions()
        assert len(sessions) >= 1

    @pytest.mark.asyncio
    async def test_create_collection(self, mock_caido):
        c = await mock_caido.create_replay_collection("validation")
        assert c["name"] == "validation"


class TestEnvironments:

    @pytest.mark.asyncio
    async def test_list_environments(self, mock_caido):
        envs = await mock_caido.list_environments()
        assert len(envs) == 1
        assert envs[0]["name"] == "prod"

    @pytest.mark.asyncio
    async def test_create_environment(self, mock_caido):
        e = await mock_caido.create_environment("staging")
        assert e["name"] == "staging"


class TestFilters:

    @pytest.mark.asyncio
    async def test_create_filter(self, mock_caido):
        f = await mock_caido.create_filter(
            "post-only", 'req.method.eq:"POST"')
        assert f["name"] == "post-only"


class TestWorkflows:

    @pytest.mark.asyncio
    async def test_list_workflows(self, mock_caido):
        workflows = await mock_caido.list_workflows()
        assert len(workflows) == 1
        assert workflows[0]["name"] == "passive-scan"

    @pytest.mark.asyncio
    async def test_toggle_workflow(self, mock_caido):
        await mock_caido.toggle_workflow("w1", False)


class TestSitemap:

    @pytest.mark.asyncio
    async def test_get_sitemap(self, mock_caido):
        sitemap = await mock_caido.get_sitemap()
        assert len(sitemap) >= 1
        assert sitemap[0]["label"] == "yandex.ru"


class TestErrorHandling:

    @pytest.mark.asyncio
    async def test_not_connected(self):
        client = CaidoClient(CaidoConfig(url="http://127.0.0.1:1"))
        with pytest.raises(CaidoError, match="not connected"):
            await client.list_projects()

    @pytest.mark.asyncio
    async def test_connection_refused(self):
        client = CaidoClient(CaidoConfig(url="http://127.0.0.1:1"))
        with pytest.raises(Exception):
            await client.connect()
        await client.close()
