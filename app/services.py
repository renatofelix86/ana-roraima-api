import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.ana_client import ANAClient
from app.models import (
    CONSISTENCY_NA,
    KIND_TELEMETRY,
    SOURCE_TELEMETRY,
    Measurement,
    Station,
)
from app.timeutils import (
    LOCAL_TZ,
    ana_timestamp_to_utc,
    local_date_of,
    now_utc,
    to_utc,
)

logger = logging.getLogger(__name__)

# Chave natural de uma observação. Precisa bater com a UniqueConstraint
# uq_measurement_natural_key.
NATURAL_KEY = (
    "station_code",
    "measured_at",
    "source",
    "reading_kind",
    "consistency_level",
)

# Lotes grandes reduzem round-trips na carga histórica sem estourar o limite de
# parâmetros do protocolo do Postgres.
UPSERT_CHUNK = 500


def _first(item: dict[str, Any], *keys: str):
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _decimal(value):
    if value in (None, "", "null", "NULL"):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _int(value):
    if value in (None, "", "null", "NULL"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(value) -> bool:
    return str(value or "").strip().casefold() in {"1", "sim", "true", "s", "y"}


def upsert_measurements(db: Session, rows: list[dict[str, Any]]) -> int:
    """Grava observações em lote, sem duplicar.

    Duas proteções distintas:

    1. Deduplicação *dentro* do lote. O Postgres recusa um ON CONFLICT DO
       UPDATE que afete a mesma linha duas vezes no mesmo comando ("cannot
       affect row a second time"), e a ANA às vezes repete o mesmo carimbo na
       mesma resposta. A última ocorrência vence.
    2. ON CONFLICT contra o que já está gravado, o que torna a coleta
       idempotente: reprocessar a mesma janela não cria linha nova.
    """
    if not rows:
        return 0

    unique: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        unique[tuple(row[k] for k in NATURAL_KEY)] = row

    ordered = list(unique.values())
    total = 0

    for start in range(0, len(ordered), UPSERT_CHUNK):
        chunk = ordered[start : start + UPSERT_CHUNK]
        stmt = insert(Measurement).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_measurement_natural_key",
            set_={
                "level_cm": stmt.excluded.level_cm,
                "flow_m3s": stmt.excluded.flow_m3s,
                "rainfall_mm": stmt.excluded.rainfall_mm,
                "level_status": stmt.excluded.level_status,
                "flow_status": stmt.excluded.flow_status,
                "rainfall_status": stmt.excluded.rainfall_status,
                "measured_date": stmt.excluded.measured_date,
                "source_updated_at": stmt.excluded.source_updated_at,
                "imported_at": stmt.excluded.imported_at,
                "raw": stmt.excluded.raw,
            },
        )
        db.execute(stmt)
        total += len(chunk)

    return total


async def sync_rr_stations(ana: ANAClient, db: Session, uf: str = "RR") -> int:
    items = await ana.inventory(uf)
    count = 0
    now = now_utc()

    for item in items:
        station_type = str(_first(item, "Tipo_Estacao") or "")
        if station_type and "fluvi" not in station_type.casefold():
            continue

        code = str(_first(item, "codigoestacao", "Codigo_Estacao") or "").strip()
        if not code:
            continue

        # As datas do inventário são dias, não instantes: interpretadas em hora
        # local para não escorregarem um dia na exibição.
        telemetry_start = to_utc(
            _first(
                item,
                "Data_Periodo_Telemetrica_Inicio",
                "Data_Periodo_Telemetria_Inicio",
            ),
            LOCAL_TZ,
        )
        telemetry_end = to_utc(_first(item, "Data_Periodo_Telemetrica_Fim"), LOCAL_TZ)

        # Tipo_Estacao_Telemetrica é o flag atual da ANA. Usar apenas a
        # existência de Data_Periodo_Telemetrica_Inicio incluiria estações já
        # desativadas: em RR são 30 fluviométricas com data de início contra 23
        # de fato telemétricas hoje.
        telemetered = _flag(_first(item, "Tipo_Estacao_Telemetrica"))

        values = {
            "code": code,
            "name": _first(item, "Estacao_Nome", "Nome_Estacao"),
            "river_name": _first(item, "Rio_Nome", "Nome_Rio"),
            "municipality_name": _first(item, "Municipio_Nome"),
            "uf": _first(item, "UF_Estacao") or uf.upper(),
            "basin_name": _first(item, "Sub_Bacia_Nome"),
            "latitude": _decimal(_first(item, "Latitude")),
            "longitude": _decimal(_first(item, "Longitude")),
            "station_type": station_type or None,
            "operator_acronym": _first(item, "Operadora_Sigla"),
            "responsible_acronym": _first(item, "Responsavel_Sigla"),
            "operating": _flag(_first(item, "Operando")),
            "telemetered": telemetered,
            "telemetry_start": telemetry_start,
            "telemetry_end": telemetry_end,
            "source_updated_at": to_utc(
                _first(item, "Data_Ultima_Atualizacao"), LOCAL_TZ
            ),
            "synced_at": now,
            "raw": item,
        }

        stmt = insert(Station).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Station.code],
            set_={k: v for k, v in values.items() if k != "code"},
        )
        db.execute(stmt)
        count += 1

    db.commit()
    return count


async def sync_station_telemetry(
    ana: ANAClient,
    db: Session,
    station_code: str,
    search_date: str,
    range_filter: str,
) -> int:
    items = await ana.telemetry_adopted(
        station_code,
        search_date=search_date,
        range_filter=range_filter,
    )

    imported = now_utc()
    rows: list[dict[str, Any]] = []

    for item in items:
        measured_at = ana_timestamp_to_utc(_first(item, "Data_Hora_Medicao"))
        if not measured_at:
            logger.warning(
                "Carimbo ilegível na estação %s: %r",
                station_code,
                item.get("Data_Hora_Medicao"),
            )
            continue

        rows.append(
            {
                "station_code": str(
                    _first(item, "codigoestacao") or station_code
                ).strip(),
                "measured_at": measured_at,
                "measured_date": local_date_of(measured_at),
                "level_cm": _decimal(_first(item, "Cota_Adotada")),
                "flow_m3s": _decimal(_first(item, "Vazao_Adotada")),
                "rainfall_mm": _decimal(_first(item, "Chuva_Adotada")),
                "level_status": _int(_first(item, "Cota_Adotada_Status")),
                "flow_status": _int(_first(item, "Vazao_Adotada_Status")),
                "rainfall_status": _int(_first(item, "Chuva_Adotada_Status")),
                "source": SOURCE_TELEMETRY,
                "reading_kind": KIND_TELEMETRY,
                "consistency_level": CONSISTENCY_NA,
                "source_updated_at": ana_timestamp_to_utc(
                    _first(item, "Data_Atualizacao")
                ),
                "imported_at": imported,
                "raw": item,
            }
        )

    count = upsert_measurements(db, rows)
    db.commit()
    return count


def get_telemetered_station_codes(db: Session, uf: str = "RR") -> list[str]:
    return list(
        db.scalars(
            select(Station.code)
            .where(
                Station.uf == uf.upper(),
                Station.telemetered.is_(True),
                Station.operating.is_(True),
            )
            .order_by(Station.code)
        ).all()
    )


def get_historical_station_codes(db: Session, uf: str = "RR") -> list[str]:
    """Todas as fluviométricas do estado.

    A carga histórica não se limita às telemétricas: estações já desativadas
    guardam décadas de série de cotas, que é justamente o que dá contexto de
    cheia e seca aos dashboards.
    """
    return list(
        db.scalars(
            select(Station.code).where(Station.uf == uf.upper()).order_by(Station.code)
        ).all()
    )
