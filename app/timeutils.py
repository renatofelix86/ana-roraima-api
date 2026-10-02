"""Normalização de datas da ANA.

A ANA devolve datas como texto simples, sem qualquer indicação de fuso
(``"2026-09-09 13:16:50.07"``). Interpretar esse texto errado desloca toda a
série temporal, então a conversão fica centralizada aqui.

Regra adotada:

* **Telemetria** — carimbada pelo servidor da ANA em ``ANA_SOURCE_TZ``
  (UTC-3). Ver a justificativa em :mod:`app.config`.
* **Série histórica de cotas** — leituras manuais de régua feitas às 7h e 17h
  *locais* da estação, seguindo a convenção hidrológica brasileira. São
  interpretadas em ``LOCAL_TZ``.

Tudo é persistido em UTC (``timestamptz``). Para os dashboards, o que importa
é o dia-calendário de Roraima, exposto separadamente em ``measured_date``.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings

SOURCE_TZ = ZoneInfo(settings.ana_source_tz)
LOCAL_TZ = ZoneInfo(settings.local_tz)

_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ana_naive(value) -> datetime | None:
    """Lê o texto da ANA sem atribuir fuso algum."""
    if value in (None, "", "null", "NULL"):
        return None

    raw = str(value).strip().replace("T", " ")
    if raw.endswith("Z"):
        raw = raw[:-1]

    for fmt in _FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def to_utc(value, tz: ZoneInfo) -> datetime | None:
    """Converte o texto da ANA para UTC, assumindo que ele está em ``tz``."""
    naive = parse_ana_naive(value)
    if naive is None:
        return None
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def ana_timestamp_to_utc(value) -> datetime | None:
    """Carimbo de telemetria (fuso do servidor da ANA) para UTC."""
    return to_utc(value, SOURCE_TZ)


def local_naive_to_utc(naive: datetime) -> datetime:
    """Instante local de Roraima para UTC."""
    return naive.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def local_date_of(moment: datetime) -> date:
    """Dia-calendário em Roraima de um instante UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date()


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()
