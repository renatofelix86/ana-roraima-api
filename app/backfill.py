"""Carga histórica sob demanda.

    docker compose run --rm worker python -m app.backfill
    docker compose run --rm worker python -m app.backfill --station 14710000
    docker compose run --rm worker python -m app.backfill --pending

É retomável: interromper com Ctrl+C e rodar de novo continua do último ano
concluído de cada estação (ver tabela backfill_progress).
"""

import argparse
import asyncio
import logging

from app.ana_client import ANAClient
from app.config import settings
from app.db import SessionLocal, init_db
from app.historical import backfill_all, backfill_station
from app.models import Station
from app.services import sync_rr_stations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("backfill")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Carga histórica de cotas da ANA.")
    parser.add_argument("--uf", default=settings.ana_uf)
    parser.add_argument("--station", help="Carrega apenas este código de estação.")
    parser.add_argument(
        "--pending",
        action="store_true",
        help="Pula estações já marcadas como concluídas.",
    )
    parser.add_argument(
        "--skip-inventory",
        action="store_true",
        help="Não sincroniza o inventário antes de carregar.",
    )
    args = parser.parse_args()

    init_db()
    ana = ANAClient()

    try:
        with SessionLocal() as db:
            if not args.skip_inventory:
                # O inventário traz Data_Periodo_Escala_Inicio, que define o
                # ano em que a carga começa. Sem ele, varreríamos décadas
                # vazias em cada estação.
                total = await sync_rr_stations(ana, db, args.uf)
                logger.info("Inventário sincronizado: %s estações.", total)

            if args.station:
                station = db.get(Station, args.station)
                if station is None:
                    raise SystemExit(f"Estação {args.station} não encontrada.")
                written = await backfill_station(ana, db, station)
                logger.info("%s: %s observações.", args.station, written)
            else:
                await backfill_all(ana, db, args.uf, only_pending=args.pending)
    finally:
        await ana.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrompido. O progresso por estação está salvo.")
