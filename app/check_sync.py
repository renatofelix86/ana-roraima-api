"""Compara o que está no banco com o que a ANA publica AGORA.

    docker compose run --rm api python -m app.check_sync
    docker compose run --rm api python -m app.check_sync --todas

É o único teste que prova sincronia de verdade. As consultas de saúde da
coleta mostram que o worker rodou; esta mostra que o dado gravado é o mesmo
que a ANA está publicando.

Para cada estação telemétrica, busca a janela de 24h na ANA e confronta com o
banco: última leitura, valor da cota e quantos registros faltam.

Lembre que a própria ANA publica com atraso, e várias estações transmitem em
lote de hora em hora. Um atraso de até ~60 min é normal; o que importa é o
banco não estar atrás da ANA.
"""

import argparse
import asyncio
import logging

from sqlalchemy import text

from app.ana_client import ANAClient
from app.config import settings
from app.db import SessionLocal
from app.services import get_telemetered_station_codes
from app.timeutils import LOCAL_TZ, ana_timestamp_to_utc, today_local

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--todas", action="store_true", help="Verifica todas (padrão: 8 primeiras)."
    )
    args = parser.parse_args()

    ana = ANAClient()
    divergentes = 0
    sem_dado = 0

    try:
        with SessionLocal() as db:
            codigos = get_telemetered_station_codes(db, settings.ana_uf)
            if not args.todas:
                codigos = codigos[:8]

            print(f"Conferindo {len(codigos)} estações contra a ANA.")
            print(f"{'estação':10} {'ANA':>17} {'banco':>17} {'faltam':>7}  situação")
            print("-" * 72)

            hoje = today_local().isoformat()

            for code in codigos:
                itens = await ana.telemetry_adopted(
                    code, search_date=hoje, range_filter="HORA_24"
                )
                if not itens:
                    print(f"{code:10} {'sem dado na ANA':>17}")
                    sem_dado += 1
                    continue

                carimbos = {
                    ana_timestamp_to_utc(i.get("Data_Hora_Medicao")) for i in itens
                }
                carimbos.discard(None)
                ana_max = max(carimbos)

                linha = db.execute(
                    text(
                        "SELECT max(measured_at) FROM measurements"
                        " WHERE station_code = :c AND source = 'ANA_TELEMETRIA'"
                    ),
                    {"c": code},
                ).scalar()

                # Quantos carimbos a ANA tem que o banco não tem.
                gravados = {
                    r[0]
                    for r in db.execute(
                        text(
                            "SELECT measured_at FROM measurements"
                            " WHERE station_code = :c AND source = 'ANA_TELEMETRIA'"
                            "   AND measured_at >= :desde"
                        ),
                        {"c": code, "desde": min(carimbos)},
                    ).all()
                }
                faltando = len(carimbos - gravados)

                ana_txt = ana_max.astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")
                db_txt = (
                    linha.astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")
                    if linha
                    else "-"
                )

                if faltando == 0:
                    situacao = "em dia"
                else:
                    situacao = f"ATRASADO ({faltando} leituras)"
                    divergentes += 1

                print(f"{code:10} {ana_txt:>17} {db_txt:>17} {faltando:>7}  {situacao}")
                await asyncio.sleep(0.2)

        print("-" * 72)
        if divergentes:
            print(
                f"{divergentes} estação(ões) atrás da ANA. Se persistir no ciclo "
                "seguinte (15 min), investigue o log do worker."
            )
        else:
            print("Todas em dia: o banco tem tudo o que a ANA publicou.")
        if sem_dado:
            print(f"{sem_dado} estação(ões) sem dado na ANA hoje (problema na origem).")
    finally:
        await ana.close()


if __name__ == "__main__":
    asyncio.run(main())
