from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.timeutils import now_utc

# Origem do dado.
SOURCE_TELEMETRY = "ANA_TELEMETRIA"
SOURCE_SERIES = "ANA_COTAS"

# Natureza da observação.
KIND_TELEMETRY = "TELEMETRIA"  # leitura automática, ~15 em 15 min
KIND_DAILY_MEAN = "MEDIA_DIARIA"  # média do dia na série histórica
KIND_GAUGE_READING = "LEITURA_REGUA"  # leitura manual de régua (7h / 17h)

# Nível de consistência da ANA. 0 = não se aplica (telemetria bruta).
CONSISTENCY_NA = 0
CONSISTENCY_RAW = 1
CONSISTENCY_REVIEWED = 2


class Station(Base):
    __tablename__ = "stations"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    river_name: Mapped[str | None] = mapped_column(String(255))
    municipality_name: Mapped[str | None] = mapped_column(String(255))
    uf: Mapped[str | None] = mapped_column(String(2), index=True)
    basin_name: Mapped[str | None] = mapped_column(String(255))

    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))

    station_type: Mapped[str | None] = mapped_column(String(80))
    operator_acronym: Mapped[str | None] = mapped_column(String(80))
    responsible_acronym: Mapped[str | None] = mapped_column(String(80))

    operating: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    telemetered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    telemetry_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telemetry_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    raw: Mapped[dict | None] = mapped_column(JSON)

    measurements: Mapped[list["Measurement"]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (
        # A chave natural precisa dos cinco campos: a mesma estação, no mesmo
        # instante, pode ter uma leitura telemétrica, uma média diária e uma
        # leitura de régua, e a série histórica ainda vem em duas versões
        # (bruta e consistida). Nenhuma delas pode sobrescrever a outra.
        UniqueConstraint(
            "station_code",
            "measured_at",
            "source",
            "reading_kind",
            "consistency_level",
            name="uq_measurement_natural_key",
        ),
        Index("ix_measurements_station_time", "station_code", "measured_at"),
        Index("ix_measurements_station_date", "station_code", "measured_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_code: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("stations.code", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Instante da medição em UTC.
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    # Dia-calendário em Roraima. Existe para o Power BI agregar por dia sem
    # depender de conversão de fuso no relatório.
    measured_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)

    level_cm: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    flow_m3s: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    rainfall_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    level_status: Mapped[int | None] = mapped_column(Integer)
    flow_status: Mapped[int | None] = mapped_column(Integer)
    rainfall_status: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[str] = mapped_column(
        String(40), default=SOURCE_TELEMETRY, nullable=False, index=True
    )
    reading_kind: Mapped[str] = mapped_column(
        String(20), default=KIND_TELEMETRY, nullable=False, index=True
    )
    consistency_level: Mapped[int] = mapped_column(
        Integer, default=CONSISTENCY_NA, nullable=False
    )

    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    raw: Mapped[dict | None] = mapped_column(JSON)

    station: Mapped[Station] = relationship(back_populates="measurements")


class CollectionLog(Base):
    __tablename__ = "collection_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(80), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), index=True)
    records: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[str | None] = mapped_column(Text)


class BackfillProgress(Base):
    """Checkpoint da carga histórica, por estação.

    Permite retomar de onde parou depois de uma queda, um deploy ou um
    ``docker compose down`` no meio de uma carga de várias horas.
    """

    __tablename__ = "backfill_progress"

    station_code: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("stations.code", ondelete="CASCADE"),
        primary_key=True,
    )
    # Último ano concluído com sucesso. A retomada começa no ano seguinte.
    last_year_done: Mapped[int | None] = mapped_column(Integer)
    first_year_seen: Mapped[int | None] = mapped_column(Integer)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    records: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
