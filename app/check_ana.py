import asyncio

from app.ana_client import ANAClient
from app.config import settings


async def main():
    ana = ANAClient()
    try:
        token = await ana.get_token()
        print(f"Autenticação ANA: OK (token recebido, tamanho={len(token)})")

        items = await ana.inventory(settings.ana_uf)
        flu = [
            i for i in items
            if "fluvi" in str(i.get("Tipo_Estacao", "")).casefold()
        ]
        print(f"Inventário {settings.ana_uf}: {len(items)} registros")
        print(f"Estações fluviométricas identificadas: {len(flu)}")

        for item in flu[:10]:
            print(
                item.get("codigoestacao"),
                "-",
                item.get("Estacao_Nome"),
                "-",
                item.get("Municipio_Nome"),
                "- telemetria desde:",
                item.get("Data_Periodo_Telemetrica_Inicio"),
            )
    finally:
        await ana.close()


if __name__ == "__main__":
    asyncio.run(main())
