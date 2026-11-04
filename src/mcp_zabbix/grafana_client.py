"""Cliente assíncrono da API HTTP (REST) do Grafana.

Usado pelas Tools de dashboard. Diferente do Zabbix, não há sessão: cada
requisição leva a credencial, em ordem de preferência:

1. `GRAFANA_API_TOKEN`: token de service account, enviado como `Authorization: Bearer`.
2. `GRAFANA_USER` + `GRAFANA_PASSWORD`: autenticação HTTP Basic.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx2

from .config import GrafanaSettings

logger = logging.getLogger("mcp_zabbix.grafana")


class GrafanaError(Exception):
    """Base de todas as falhas ao falar com o Grafana."""


class GrafanaConfigError(GrafanaError):
    """Configuração ausente ou inválida (por exemplo, sem credenciais)."""


class GrafanaConnectionError(GrafanaError):
    """Rede, tempo esgotado ou resposta que não é da API do Grafana."""


class GrafanaAPIError(GrafanaError):
    """O Grafana respondeu com um status HTTP de erro."""

    def __init__(self, method: str, path: str, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"{method} {path}: HTTP {status} {message}".strip())


class GrafanaClient:
    """Faz requisições REST ao Grafana e devolve o corpo JSON."""

    def __init__(
        self,
        settings: GrafanaSettings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        auth = None
        if not settings.api_token and settings.user and settings.password:
            auth = httpx2.BasicAuth(settings.user, settings.password)
        headers = {"Accept": "application/json"}
        if settings.api_token:
            headers["Authorization"] = f"Bearer {settings.api_token}"
        self._http = httpx2.AsyncClient(
            base_url=settings.base_url,
            auth=auth,
            headers=headers,
            timeout=httpx2.Timeout(settings.timeout_seconds),
            verify=settings.verify_tls,
            transport=transport,
        )

    # --- transporte -------------------------------------------------------

    async def request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        if not self.settings.has_credentials:
            raise GrafanaConfigError(
                "Sem credenciais do Grafana: defina GRAFANA_API_TOKEN ou GRAFANA_USER e GRAFANA_PASSWORD."
            )
        url = f"{self.settings.base_url}{path}"
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx2.TimeoutException as exc:
            raise GrafanaConnectionError(
                f"Tempo esgotado ({self.settings.timeout_seconds:g}s) em {method} {url}."
            ) from exc
        except httpx2.ConnectError as exc:
            raise GrafanaConnectionError(
                f"Não consegui conectar em {self.settings.base_url}. O Grafana está no ar? ({exc})"
            ) from exc
        except httpx2.HTTPError as exc:
            raise GrafanaConnectionError(f"Erro de rede em {method} {url}: {exc}") from exc

        try:
            body = response.json() if response.content else None
        except ValueError as exc:
            raise GrafanaConnectionError(
                f"A resposta de {url} não é JSON. A GRAFANA_URL aponta para o Grafana?"
            ) from exc

        if response.status_code >= 400:
            message = body.get("message", "") if isinstance(body, dict) else ""
            if response.status_code == 401:
                message = message or "credenciais recusadas"
            raise GrafanaAPIError(method, path, response.status_code, message)
        return body

    # --- operações ----------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        return await self.request("GET", "/api/health")

    async def search_dashboards(self, query: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"type": "dash-db", "limit": limit}
        if query:
            params["query"] = query
        return await self.request("GET", "/api/search", params=params)

    async def save_dashboard(
        self, dashboard: dict[str, Any], *, folder_uid: str | None = None, overwrite: bool = True
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"dashboard": dashboard, "overwrite": overwrite}
        if folder_uid:
            payload["folderUid"] = folder_uid
        return await self.request("POST", "/api/dashboards/db", json=payload)

    async def delete_dashboard(self, uid: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/api/dashboards/uid/{uid}")

    async def aclose(self) -> None:
        await self._http.aclose()
