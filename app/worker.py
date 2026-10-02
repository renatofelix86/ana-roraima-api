import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import update

from app.ana_client import ANAClient
from app.config import settings
from app.db import SessionLocal, init_db
from app.historical import backfill_all
from app.models import CollectionLog
from app.services import (
    get_telemetered_station_codes,
    sync_rr_stations,
    sync_station_telemetry,
)
from app.timeutils import LOCAL_TZ, now_utc, today_local

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")

ana = ANAClient()


async def tracked(job_name: str, runner) -> None:
    """Executa um job registrando início, fim e resultado em collection_logs."""
    with SessionLocal() as db:
        log = CollectionLog(
            job=job_name, started_at=now_utc(), status="RUNNING", records=0
        )
        db.add(log)
        db.commit()
        log_id = log.id

        records, status, details = 0, "OK", None
        try:
            records = await runner(db) or 0
        except Exception as exc:
            db.rollback()
            status, details = "ERROR", repr(exc)[:4000]
            logger.exception("Falha no job %s.", job_name)

        # UPDATE direto em vez de mexer no objeto: depois de um rollback a
        # identidade da sessão pode estar expirada, e o registro do erro é
        # justamente o que não pode falhar.
        db.execute(
            update(CollectionLog)
            .where(CollectionLog.id == log_id)
            .values(
                status=status,
                records=records,
                details=details,
                finished_at=now_utc(),
            )
        )
        db.commit()


async def stations_job() -> None:
    async def run(db):
        count = await sync_rr_stations(ana, db, settings.ana_uf)
        logger.info("Inventário %s sincronizado: %s estações.", settings.ana_uf, count)
        return count

    await tracked("sync_stations_rr", run)


async def telemetry_job() -> None:
    async def run(db):
        codes = get_telemetered_station_codes(db, settings.ana_uf)
        if not codes:
            logger.warning(
                "Nenhuma estação telemétrica em %s. O inventário já rodou?",
                settings.ana_uf,
            )
            return 0

        logger.info("Coletando telemetria de %s estações.", len(codes))
        search_date = today_local().isoformat()
        total = 0

        for code in codes:
            try:
                total += await sync_station_telemetry(
                    ana,
                    db,
                    code,
                    search_date=search_date,
                    range_filter=settings.telemetry_range,
                )
            except Exception:
                db.rollback()
                logger.exception("Falha na estação %s; seguindo para a próxima.", code)

            # Evita rajadas agressivas contra a ANA.
            await asyncio.sleep(0.25)

        logger.info("Telemetria concluída: %s registros processados.", total)
        return total

    await tracked("sync_telemetry_rr", run)


async def backfill_job() -> None:
    async def run(db):
        return await backfill_all(ana, db, settings.ana_uf)

    await tracked("backfill_cotas", run)


async def main() -> None:
    init_db()

    scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)

    # O primeiro disparo precisa de um datetime *com fuso*.
    #
    # Passar datetime.now() ingênuo faz o APScheduler assumir que aquele
    # horário já está no fuso do scheduler (America/Boa_Vista). Como o
    # container roda em UTC, o "17:01" do relógio virava 17:01-04:00 = 21:01
    # UTC e o primeiro ciclo só rodava 4 horas depois -- em cada restart o
    # coletor ficava mudo esse tempo todo.
    agora = datetime.now(LOCAL_TZ)

    scheduler.add_job(
        stations_job,
        "interval",
        hours=settings.station_sync_interval_hours,
        id="stations_rr",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        next_run_time=agora,
    )

    scheduler.add_job(
        telemetry_job,
        "interval",
        minutes=settings.collect_interval_minutes,
        id="telemetry_rr",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        # Meio minuto depois do inventário: a telemetria depende da lista de
        # estações que o job anterior grava.
        next_run_time=agora + timedelta(seconds=30),
    )

    if settings.backfill_enabled:
        scheduler.add_job(
            backfill_job,
            "interval",
            hours=settings.backfill_interval_hours,
            id="backfill_cotas",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            # A primeira carga é longa (anos por estação). Começa depois que a
            # telemetria já preencheu o painel operacional.
            next_run_time=agora + timedelta(minutes=2),
        )

    scheduler.start()
    logger.info(
        "Worker iniciado. Telemetria a cada %s min (%s), inventário a cada %sh, "
        "histórico %s.",
        settings.collect_interval_minutes,
        settings.telemetry_range,
        settings.station_sync_interval_hours,
        "ligado" if settings.backfill_enabled else "desligado",
    )

    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        await ana.close()


if __name__ == "__main__":
    asyncio.run(main())
