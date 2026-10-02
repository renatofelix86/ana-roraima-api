from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import SOURCE_TELEMETRY, CollectionLog, Measurement, Station
from app.timeutils import LOCAL_TZ


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="API Hidrologia Roraima",
    version="0.2.0",
    description=(
        "API interna para dados hidrológicos coletados da ANA. "
        "Os horários são devolvidos em hora de Roraima (UTC-4)."
    ),
    lifespan=lifespan,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    if settings.app_api_key and x_api_key != settings.app_api_key:
        raise HTTPException(status_code=401, detail="API key inválida.")


def _local(moment: datetime | None):
    """Instante UTC do banco -> ISO em hora de Roraima."""
    if moment is None:
        return None
    return moment.astimezone(LOCAL_TZ).isoformat()


def _num(value):
    return float(value) if value is not None else None


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Saúde do serviço e frescor do dado.

    Vai além do 'ok': devolve a idade da última medição, que é o sinal que
    realmente importa para saber se a coleta parou.
    """
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Banco indisponível: {exc!r}")

    latest = db.scalar(
        select(func.max(Measurement.measured_at)).where(
            Measurement.source == SOURCE_TELEMETRY
        )
    )
    last_run = db.scalar(
        select(CollectionLog)
        .where(CollectionLog.job == "sync_telemetry_rr")
        .order_by(CollectionLog.id.desc())
        .limit(1)
    )

    idade_min = None
    if latest is not None:
        idade_min = round(
            (datetime.now(tz=latest.tzinfo) - latest).total_seconds() / 60.0, 1
        )

    return {
        "status": "ok",
        "ultima_medicao": _local(latest),
        "idade_minutos": idade_min,
        "ultima_coleta": {
            "status": last_run.status if last_run else None,
            "registros": last_run.records if last_run else None,
            "concluida_em": _local(last_run.finished_at) if last_run else None,
        },
    }


@app.get("/api/stations", dependencies=[Depends(require_api_key)])
def list_stations(
    telemetered: bool | None = Query(default=None),
    operating: bool | None = Query(default=None),
    db: Session = Depends(get_db),
):
    stmt = select(Station).where(Station.uf == settings.ana_uf.upper())

    if telemetered is not None:
        stmt = stmt.where(Station.telemetered.is_(telemetered))
    if operating is not None:
        stmt = stmt.where(Station.operating.is_(operating))

    stations = db.scalars(stmt.order_by(Station.name, Station.code)).all()

    return [
        {
            "code": s.code,
            "name": s.name,
            "river_name": s.river_name,
            "municipality_name": s.municipality_name,
            "basin_name": s.basin_name,
            "uf": s.uf,
            "latitude": _num(s.latitude),
            "longitude": _num(s.longitude),
            "operating": s.operating,
            "telemetered": s.telemetered,
            "telemetry_start": _local(s.telemetry_start),
            "operator_acronym": s.operator_acronym,
            "responsible_acronym": s.responsible_acronym,
        }
        for s in stations
    ]


def _measurement_payload(r: Measurement) -> dict:
    return {
        "measured_at": _local(r.measured_at),
        "measured_date": r.measured_date.isoformat(),
        "level_cm": _num(r.level_cm),
        "flow_m3s": _num(r.flow_m3s),
        "rainfall_mm": _num(r.rainfall_mm),
        "level_status": r.level_status,
        "flow_status": r.flow_status,
        "rainfall_status": r.rainfall_status,
        "source": r.source,
        "reading_kind": r.reading_kind,
        "consistency_level": r.consistency_level,
        "source_updated_at": _local(r.source_updated_at),
    }


@app.get("/api/stations/{station_code}/latest", dependencies=[Depends(require_api_key)])
def latest_measurement(station_code: str, db: Session = Depends(get_db)):
    station = db.get(Station, station_code)
    if not station:
        raise HTTPException(status_code=404, detail="Estação não encontrada.")

    measurement = db.scalar(
        select(Measurement)
        .where(
            Measurement.station_code == station_code,
            Measurement.source == SOURCE_TELEMETRY,
        )
        .order_by(Measurement.measured_at.desc())
        .limit(1)
    )

    if not measurement:
        raise HTTPException(
            status_code=404,
            detail="Ainda não existem medições telemétricas para esta estação.",
        )

    return {
        "station": {
            "code": station.code,
            "name": station.name,
            "river_name": station.river_name,
            "municipality_name": station.municipality_name,
        },
        "measurement": _measurement_payload(measurement),
    }


@app.get(
    "/api/stations/{station_code}/measurements",
    dependencies=[Depends(require_api_key)],
)
def station_measurements(
    station_code: str,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    if not db.get(Station, station_code):
        raise HTTPException(status_code=404, detail="Estação não encontrada.")

    stmt = select(Measurement).where(Measurement.station_code == station_code)

    # Datas sem fuso na query string são lidas como hora de Roraima, que é o
    # que quem chama a API tem em mãos.
    if start is not None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=LOCAL_TZ)
        stmt = stmt.where(Measurement.measured_at >= start)
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=LOCAL_TZ)
        stmt = stmt.where(Measurement.measured_at <= end)
    if source is not None:
        stmt = stmt.where(Measurement.source == source)

    rows = db.scalars(stmt.order_by(Measurement.measured_at.desc()).limit(limit)).all()
    return [_measurement_payload(r) for r in rows]


@app.get("/api/stations/{station_code}/daily", dependencies=[Depends(require_api_key)])
def station_daily_series(
    station_code: str,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=100000),
    db: Session = Depends(get_db),
):
    """Série diária consolidada (histórico da ANA + telemetria agregada).

    Lê a mesma view bi_serie_diaria que o Power BI consome, então API e
    dashboard nunca divergem.
    """
    if not db.get(Station, station_code):
        raise HTTPException(status_code=404, detail="Estação não encontrada.")

    sql = """
        SELECT data_medicao, nivel_medio_cm, nivel_min_cm, nivel_max_cm,
               vazao_media_m3s, chuva_total_mm, leituras_telemetricas, origem
        FROM bi_serie_diaria
        WHERE codigo_estacao = :code
          AND (:start IS NULL OR data_medicao >= :start)
          AND (:end   IS NULL OR data_medicao <= :end)
        ORDER BY data_medicao DESC
        LIMIT :limit
    """
    rows = db.execute(
        text(sql),
        {"code": station_code, "start": start, "end": end, "limit": limit},
    ).mappings()

    return [
        {
            "date": r["data_medicao"].isoformat(),
            "level_avg_cm": r["nivel_medio_cm"],
            "level_min_cm": r["nivel_min_cm"],
            "level_max_cm": r["nivel_max_cm"],
            "flow_avg_m3s": r["vazao_media_m3s"],
            "rainfall_mm": r["chuva_total_mm"],
            "telemetry_readings": r["leituras_telemetricas"],
            "source": r["origem"],
        }
        for r in rows
    ]
