"""Tools de dashboard do Grafana, com Zabbix e Grafana falsos em memória."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx2
import pytest
from mcp import Client

from mcp_zabbix.config import GrafanaSettings
from mcp_zabbix.grafana_client import GrafanaAPIError, GrafanaClient, GrafanaConfigError

from .conftest import FakeGrafana, FakeZabbix
from .test_server import add_host, text_of

pytestmark = pytest.mark.anyio

MakeClient = Callable[..., AbstractAsyncContextManager[Client]]


def add_items(fake: FakeZabbix, host_id: str, *items: tuple[str, str, str]) -> None:
    """Cadastra itens (nome, value_type, unidade) no host."""
    for name, value_type, units in items:
        fake.items.append(
            {"itemid": fake.new_id(), "hostid": host_id, "name": name, "key_": name, "value_type": value_type, "units": units}
        )


def linux_host(fake: FakeZabbix) -> str:
    host_id = add_host(fake, "web-01", name="Web 01")
    add_items(
        fake,
        host_id,
        ("CPU utilization", "0", "%"),
        ("Memory utilization", "0", "%"),
        ("Interface eth0: Bits sent", "3", "bps"),
        ("System name", "1", ""),  # texto: não vira gráfico
    )
    return host_id


# --- listar ------------------------------------------------------------------


async def test_list_dashboards_returns_full_links(make_client: MakeClient, grafana: FakeGrafana) -> None:
    grafana.add("abc", "Meu painel")

    async with make_client() as client:
        result = await client.call_tool("list_dashboards", {})

    assert result.structured_content == {
        "count": 1,
        "items": [
            {"uid": "abc", "title": "Meu painel", "folder": "General", "tags": [], "url": "http://grafana.test/d/abc/x"}
        ],
    }


# --- criar -------------------------------------------------------------------


async def test_create_host_dashboard_uses_default_items(
    make_client: MakeClient, fake: FakeZabbix, grafana: FakeGrafana
) -> None:
    host_id = linux_host(fake)

    async with make_client() as client:
        result = await client.call_tool("create_host_dashboard", {"host": "web-01"})

    assert not result.is_error, text_of(result)
    assert result.structured_content["uid"] == f"zbx-{host_id}"
    assert result.structured_content["panels"] == ["CPU utilization", "Memory utilization"]

    saved = grafana.dashboards[f"zbx-{host_id}"]
    assert saved["title"] == "Web 01"
    cpu, memory = saved["panels"]
    assert cpu["gridPos"] == {"x": 0, "y": 0, "w": 12, "h": 8}
    assert memory["gridPos"] == {"x": 12, "y": 0, "w": 12, "h": 8}
    assert cpu["fieldConfig"]["defaults"]["unit"] == "percent"
    target = cpu["targets"][0]
    assert target["group"] == {"filter": "Linux servers"}
    assert target["host"] == {"filter": "Web 01"}
    assert target["item"] == {"filter": "CPU utilization"}
    assert target["datasource"]["uid"] == "zabbix"


async def test_create_host_dashboard_with_chosen_items(
    make_client: MakeClient, fake: FakeZabbix, grafana: FakeGrafana
) -> None:
    linux_host(fake)

    async with make_client() as client:
        result = await client.call_tool(
            "create_host_dashboard",
            {"host": "web-01", "items": ["interface eth0: bits sent", "CPU utilization"], "title": "Rede"},
        )

    assert not result.is_error, text_of(result)
    assert result.structured_content["panels"] == ["Interface eth0: Bits sent", "CPU utilization"]
    saved = grafana.dashboards[result.structured_content["uid"]]
    assert saved["panels"][0]["fieldConfig"]["defaults"]["unit"] == "bps"
    assert saved["panels"][1]["gridPos"]["x"] == 12


async def test_create_host_dashboard_rejects_unknown_and_text_items(
    make_client: MakeClient, fake: FakeZabbix, grafana: FakeGrafana
) -> None:
    linux_host(fake)

    async with make_client() as client:
        unknown = await client.call_tool("create_host_dashboard", {"host": "web-01", "items": ["CPU"]})
        text = await client.call_tool("create_host_dashboard", {"host": "web-01", "items": ["System name"]})

    assert unknown.is_error
    assert "['CPU']" in text_of(unknown)
    assert "Parecidos: CPU utilization" in text_of(unknown)
    assert text.is_error, "item de texto não pode virar gráfico"
    assert grafana.dashboards == {}


async def test_create_host_dashboard_without_default_items(
    make_client: MakeClient, fake: FakeZabbix, grafana: FakeGrafana
) -> None:
    host_id = add_host(fake, "switch-01")
    add_items(fake, host_id, ("Uptime", "3", "s"))

    async with make_client() as client:
        result = await client.call_tool("create_host_dashboard", {"host": "switch-01"})

    assert result.is_error
    assert "Informe 'items'" in text_of(result)
    assert grafana.dashboards == {}


async def test_same_title_updates_and_new_title_creates(
    make_client: MakeClient, fake: FakeZabbix, grafana: FakeGrafana
) -> None:
    linux_host(fake)

    async with make_client() as client:
        first = await client.call_tool("create_host_dashboard", {"host": "web-01"})
        again = await client.call_tool("create_host_dashboard", {"host": "web-01"})
        other = await client.call_tool("create_host_dashboard", {"host": "web-01", "title": "Rede"})

    assert first.structured_content["uid"] == again.structured_content["uid"]
    assert again.structured_content["version"] == 2
    assert other.structured_content["uid"] != first.structured_content["uid"]
    assert len(grafana.dashboards) == 2


# --- apagar ------------------------------------------------------------------


async def test_delete_dashboard_needs_confirmation(make_client: MakeClient, grafana: FakeGrafana) -> None:
    grafana.add("abc", "Velho")

    async with make_client() as client:
        refused = await client.call_tool("delete_dashboard", {"dashboard": "Velho"})
        assert refused.is_error
        assert "confirm=true" in text_of(refused)
        assert "abc" in grafana.dashboards

        done = await client.call_tool("delete_dashboard", {"dashboard": "abc", "confirm": True})

    assert done.structured_content == {"deleted": True, "uid": "abc", "title": "Velho"}
    assert grafana.dashboards == {}


async def test_delete_dashboard_ambiguous_or_missing(make_client: MakeClient, grafana: FakeGrafana) -> None:
    grafana.add("a1", "Igual")
    grafana.add("a2", "Igual")

    async with make_client() as client:
        ambiguous = await client.call_tool("delete_dashboard", {"dashboard": "Igual", "confirm": True})
        missing = await client.call_tool("delete_dashboard", {"dashboard": "Nada", "confirm": True})

    assert ambiguous.is_error and "use o uid" in text_of(ambiguous)
    assert missing.is_error and "list_dashboards" in text_of(missing)
    assert len(grafana.dashboards) == 2


# --- segurança e erros -------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("create_host_dashboard", {"host": "web-01"}),
        ("delete_dashboard", {"dashboard": "abc", "confirm": True}),
    ],
)
async def test_read_only_blocks_grafana_writes(
    make_client: MakeClient, fake: FakeZabbix, grafana: FakeGrafana, tool: str, args: dict[str, Any]
) -> None:
    linux_host(fake)
    grafana.add("abc", "Fica")

    async with make_client(read_only=True) as client:
        result = await client.call_tool(tool, args)

    assert result.is_error
    assert "somente leitura" in text_of(result)
    assert grafana.calls == [], "nenhuma chamada deveria chegar ao Grafana"
    assert "abc" in grafana.dashboards


async def test_grafana_auth_error_is_readable(make_client: MakeClient, grafana: FakeGrafana) -> None:
    grafana.password = "outra"

    async with make_client() as client:
        result = await client.call_tool("list_dashboards", {})

    assert result.is_error
    assert "HTTP 401" in text_of(result)
    assert "Invalid username or password" in text_of(result)


# --- cliente -----------------------------------------------------------------


async def test_client_prefers_token_over_basic_auth() -> None:
    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx2.Response(200, json=[])

    settings = GrafanaSettings(url="http://g.test", api_token="tok", user="admin", password="admin")
    client = GrafanaClient(settings, transport=httpx2.MockTransport(handle))
    await client.search_dashboards()
    await client.aclose()

    assert seen == ["Bearer tok"]


async def test_client_without_credentials_fails_before_the_network() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("não deveria chamar a rede")

    client = GrafanaClient(GrafanaSettings(url="http://g.test"), transport=httpx2.MockTransport(handle))
    with pytest.raises(GrafanaConfigError):
        await client.health()
    await client.aclose()


async def test_client_reports_http_errors() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"message": "Dashboard not found"})

    settings = GrafanaSettings(url="http://g.test", user="admin", password="admin")
    client = GrafanaClient(settings, transport=httpx2.MockTransport(handle))
    with pytest.raises(GrafanaAPIError) as error:
        await client.delete_dashboard("x")
    await client.aclose()

    assert error.value.status == 404
    assert "Dashboard not found" in str(error.value)
