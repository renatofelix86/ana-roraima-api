"""Carga da série histórica de cotas (HidroSerieCotas/v1).

Formato real da ANA, verificado em 2026-09-09 -- ele **não** é uma lista de
leituras. Cada item é um *mês inteiro em uma linha*, com as colunas
``Cota_01`` .. ``Cota_31``:

    {"codigoestacao": "14710000",
     "Data_Hora_Dado": "2024-01-01 07:00:00.0",
     "Mediadiaria": "0", "nivelconsistencia": "1",
     "Cota_01": "128.0", "Cota_01_Status": "1", ... "Cota_31": "81.0"}

A parte de hora de ``Data_Hora_Dado`` identifica a série, não o dia:

===============  ============  =========================================
Data_Hora_Dado   Mediadiaria   significado
===============  ============  =========================================
``... 00:00``    ``1``         média diária do mês
``... 07:00``    ``0``         leitura de régua das 7h
``... 17:00``    ``0``         leitura de régua das 17h
===============  ============  =========================================

Ou seja, um único mês chega em até 3 linhas por nível de consistência
(1=bruto, 2=consistido), e cada linha vira até 31 observações. Um ano completo
devolveu 36 itens = 12 meses x 3 séries.

As leituras de 7h/17h são horários *locais* de observação (convenção
hidrológica brasileira), diferente da telemetria, que vem carimbada pelo
servidor da ANA. Por isso aqui se usa LOCAL_TZ.
"""

import asyncio
import logging
from calendar import monthrange
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.ana_client import ANAClient
from app.config import settings
from app.models import (
    CONSISTENCY_RAW,
    KIND_DAILY_MEAN,
    KIND_GAUGE_READING,
    SOURCE_SERIES,
    BackfillProgress,
    Station,
)
from app.services import _decimal, _int, upsert_measurements
from app.timeutils import local_naive_to_utc, now_utc, parse_ana_naive, today_local

logger = logging.getLogger(__name__)

# Campos do inventário que marcam o início de alguma série na estação. O menor
# deles é um bom ponto de partida para a carga, evitando varrer décadas vazias.
START_HINT_FIELDS = (
    "Data_Periodo_Escala_Inicio",
    "Data_Periodo_Registrador_Nivel_Inicio",
    "Data_Periodo_Telemetrica_Inicio",
    "Data_Periodo_Desc_liquida_Inicio",
    "Data_Periodo_Desc_Liquida_Inicio",
)


def resolve_start_year(station: Station) -> int:
    """Primeiro ano que vale a pena consultar para esta estação."""
    raw = station.raw or {}
    years: list[int] = []

    for field in START_HINT_FIELDS:
        moment = parse_ana_naive(raw.get(field))
        if moment is not None:
            years.append(moment.year)

    if years:
        return max(min(years), settings.backfill_start_year)
    return settings.backfill_start_year


def explode_month_row(item: dict[str, Any], fallback_code: str) -> list[dict[str, Any]]:
    """Transforma uma linha mensal da ANA em observações diárias."""
    stamp = parse_ana_naive(item.get("Data_Hora_Dado"))
    if stamp is None:
        logger.warning("Data_Hora_Dado ilegível: %r", item.get("Data_Hora_Dado"))
        return []

    code = str(item.get("codigoestacao") or fallback_code).strip()
    consistency = _int(item.get("nivelconsistencia")) or CONSISTENCY_RAW
    is_daily_mean = str(item.get("Mediadiaria") or "").strip() == "1"
    kind = KIND_DAILY_MEAN if is_daily_mean else KIND_GAUGE_READING

    updated_at = parse_ana_naive(item.get("Data_Ultima_Alteracao"))
    source_updated_at = (
        local_naive_to_utc(updated_at) if updated_at is not None else None
    )

    imported = now_utc()
    days_in_month = monthrange(stamp.year, stamp.month)[1]
    rows: list[dict[str, Any]] = []

    for day in range(1, days_in_month + 1):
        level = _decimal(item.get(f"Cota_{day:02d}"))
        if level is None:
            continue

        measured_date = date(stamp.year, stamp.month, day)
        # A hora vem do cabeçalho da linha (00h média, 07h/17h régua) e é hora
        # local de observação.
        local_moment = datetime(
            stamp.year, stamp.month, day, stamp.hour, stamp.minute
        )

        rows.append(
            {
                "station_code": code,
                "measured_at": local_naive_to_utc(local_moment),
                "measured_date": measured_date,
                "level_cm": level,
                "flow_m3s": None,
                "rainfall_mm": None,
                "level_status": _int(item.get(f"Cota_{day:02d}_Status")),
                "flow_status": None,
                "rainfall_status": None,
                "source": SOURCE_SERIES,
                "reading_kind": kind,
                "consistency_level": consistency,
                "source_updated_at": source_updated_at,
                "imported_at": imported,
                # Guardar o mês inteiro em cada uma das 31 linhas multiplicaria
                # o banco por ~30. Só o essencial para rastrear a origem.
                "raw": {
                    "Data_Hora_Dado": item.get("Data_Hora_Dado"),
                    "Mediadiaria": item.get("Mediadiaria"),
                    "nivelconsistencia": item.get("nivelconsistencia"),
                    "Tipo_Medicao_Cotas": item.get("Tipo_Medicao_Cotas"),
                    "dia": day,
                },
            }
        )

    return rows


async def backfill_station(
    ana: ANAClient,
    db: Session,
    station: Station,
    *,
    end_year: int | None = None,
    delay: float | None = None,
) -> int:
    """Carrega a série de uma estação, ano a ano, com checkpoint.

    O limite da ANA é de 366 dias por requisição, então a janela natural é o
    ano civil. Cada ano é gravado e comitado antes de ir para o próximo: uma
    queda no meio de uma carga de horas só custa o ano em andamento.
    """
    code = station.code
    end_year = end_year or today_local().year
    delay = settings.backfill_request_delay if delay is None else delay

    progress = db.get(BackfillProgress, code)
    if progress is None:
        progress = BackfillProgress(station_code=code, records=0, completed=False)
        db.add(progress)
        db.commit()

    start_year = resolve_start_year(station)
    if progress.last_year_done is not None:
        # Refaz o último ano concluído: se ele for o ano corrente, é assim que
        # os meses novos entram; se for antigo, custa uma requisição só.
        start_year = max(start_year, progress.last_year_done)

    if progress.first_year_seen is None:
        progress.first_year_seen = start_year

    total = 0
    for year in range(start_year, end_year + 1):
        try:
            payload = await ana.historical_levels_raw(
                code, f"{year}-01-01", f"{year}-12-31"
            )
        except Exception as exc:
            # Falha de rede ou da ANA: para nesta estação sem marcar o ano como
            # concluído, para a próxima execução retomar exatamente daqui.
            db.rollback()
            progress = db.get(BackfillProgress, code)
            progress.last_error = f"{year}: {exc!r}"[:2000]
            progress.updated_at = now_utc()
            db.commit()
            logger.warning("Estação %s parou em %s: %r", code, year, exc)
            return total

        rows: list[dict[str, Any]] = []
        for item in payload.get("items") or []:
            rows.extend(explode_month_row(item, code))

        written = upsert_measurements(db, rows)
        total += written

        progress.last_year_done = year
        progress.records = (progress.records or 0) + written
        progress.last_error = None
        progress.updated_at = now_utc()
        db.commit()

        if written:
            logger.info("  %s/%s: %s observações", code, year, written)

        await asyncio.sleep(delay)

    progress.completed = True
    progress.updated_at = now_utc()
    db.commit()
    return total


async def backfill_all(
    ana: ANAClient,
    db: Session,
    uf: str | None = None,
    *,
    only_pending: bool = False,
) -> int:
    """Percorre todas as fluviométricas do estado."""
    uf = (uf or settings.ana_uf).upper()
    stations = (
        db.query(Station).filter(Station.uf == uf).order_by(Station.code).all()
    )

    if only_pending:
        stations = [
            s
            for s in stations
            if not (
                (p := db.get(BackfillProgress, s.code)) is not None and p.completed
            )
        ]

    logger.info("Carga histórica: %s estações em %s.", len(stations), uf)
    total = 0

    for index, station in enumerate(stations, start=1):
        logger.info(
            "[%s/%s] %s - %s (desde %s)",
            index,
            len(stations),
            station.code,
            station.name,
            resolve_start_year(station),
        )
        try:
            total += await backfill_station(ana, db, station)
        except Exception:
            db.rollback()
            logger.exception("Falha inesperada na estação %s.", station.code)

    logger.info("Carga histórica concluída: %s observações gravadas.", total)
    return total
