import asyncio
import logging
import time
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger(__name__)


class ANAError(RuntimeError):
    pass


class ANAClient:
    def __init__(self) -> None:
        self.base_url = settings.ana_base_url.rstrip("/")
        self.identifier = settings.ana_identifier
        self.password = settings.ana_password

        self._token: str | None = None
        self._token_valid_until_monotonic: float = 0.0
        self._token_lock = asyncio.Lock()

        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(40.0, connect=15.0),
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _authenticate(self) -> str:
        url = f"{self.base_url}/OAUth/v1"

        # Conforme o Swagger/manual da ANA, identificador e senha são headers.
        response = await self.http.get(
            url,
            headers={
                "Identificador": self.identifier,
                "Senha": self.password,
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()

        items = payload.get("items") or {}
        if isinstance(items, list):
            if not items:
                raise ANAError("Resposta de autenticação sem items.")
            items = items[0]

        token = items.get("tokenautenticacao")
        if not token:
            raise ANAError(
                f"ANA não retornou tokenautenticacao. "
                f"status={payload.get('status')} message={payload.get('message')}"
            )

        self._token = token

        # O manual da ANA informa validade de 60 min, mas na prática é menos:
        # em 2026-09-09 um token obtido às 17:28:55 já foi recusado com 401 às
        # 18:13:54, ou seja, ~45 min. A janela de 55 min do código anterior
        # estourava a validade real e gastava uma requisição recusada a cada
        # ciclo (o retry de 401 salvava, mas era desperdício).
        self._token_valid_until_monotonic = time.monotonic() + (40 * 60)
        logger.info("Novo token ANA obtido com sucesso.")
        return token

    async def get_token(self, force: bool = False) -> str:
        if (
            not force
            and self._token
            and time.monotonic() < self._token_valid_until_monotonic
        ):
            return self._token

        async with self._token_lock:
            if (
                not force
                and self._token
                and time.monotonic() < self._token_valid_until_monotonic
            ):
                return self._token
            return await self._authenticate()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self.get_token()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        response = await self.http.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.status_code in (401, 403):
            token = await self.get_token(force=True)
            response = await self.http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )

        response.raise_for_status()
        payload = response.json()

        if payload.get("code") not in (None, 200):
            raise ANAError(
                f"Erro lógico da ANA: code={payload.get('code')} "
                f"message={payload.get('message')}"
            )

        return payload

    async def inventory(self, uf: str = "RR") -> list[dict[str, Any]]:
        payload = await self._get(
            "HidroInventarioEstacoes/v1",
            params={"Unidade Federativa": uf.upper()},
        )
        return payload.get("items") or []

    async def telemetry_adopted(
        self,
        station_code: str,
        *,
        search_date: str,
        range_filter: str = "HORA_2",
    ) -> list[dict[str, Any]]:
        payload = await self._get(
            "HidroinfoanaSerieTelemetricaAdotada/v1",
            params={
                "Código da Estação": station_code,
                "Tipo Filtro Data": "DATA_LEITURA",
                "Data de Busca (yyyy-MM-dd)": search_date,
                "Range Intervalo de busca": range_filter,
            },
        )
        return payload.get("items") or []

    async def historical_levels_raw(
        self,
        station_code: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """
        Rota preparada para o próximo passo.

        A ANA documenta HidroSerieCotas/v1 e limite de 366 dias por requisição.
        Antes de normalizar a série, vamos validar o formato real retornado pelas
        suas credenciais/estações.
        """
        return await self._get(
            "HidroSerieCotas/v1",
            params={
                "Código da Estação": station_code,
                "Tipo Filtro Data": "DATA_LEITURA",
                "Data Inicial (yyyy-MM-dd)": start_date,
                "Data Final (yyyy-MM-dd)": end_date,
            },
        )
