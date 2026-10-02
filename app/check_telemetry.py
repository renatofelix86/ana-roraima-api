import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.ana_client import ANAClient


async def main():
    ana = ANAClient()

    try:
        codigo = "14530000"  # VILA SURUMU

        hoje = datetime.now(
            ZoneInfo("America/Boa_Vista")
        ).date().isoformat()

        print(f"Consultando estação {codigo}")
        print(f"Data: {hoje}")

        dados = await ana.telemetry_adopted(
            codigo,
            search_date=hoje,
            range_filter="HORA_2",
        )

        print(f"Registros recebidos: {len(dados)}")

        for item in dados:
            print("-" * 60)
            print(item)

    finally:
        await ana.close()


if __name__ == "__main__":
    asyncio.run(main())