from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ana_base_url: str = "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas"
    ana_identifier: str
    ana_password: str
    ana_uf: str = "RR"

    database_url: str
    app_api_key: str = ""

    # Fuso em que a ANA carimba a telemetria.
    #
    # Verificado empiricamente em 2026-09-09: estações de RR (UTC-4), AC (UTC-5)
    # e SP (UTC-3) consultadas no mesmo instante retornaram todas o mesmo
    # horário de leitura (13:00-13:30), provando que a ANA usa um fuso único de
    # servidor e não a hora local da estação. Comparando com a hora real
    # (UTC 17:10 / Brasília 14:10 / Boa Vista 13:10), apenas UTC-3 é compatível:
    # UTC-4 colocaria a leitura no futuro e UTC deixaria os dados fora da
    # janela HORA_2 devolvida pela própria API.
    ana_source_tz: str = "America/Sao_Paulo"

    # Fuso de referência do negócio. Define o dia-calendário (measured_date)
    # usado nos dashboards e a leitura das réguas históricas de 7h/17h.
    local_tz: str = "America/Boa_Vista"

    # Coleta telemétrica.
    #
    # HORA_24 devolve ~93 registros por chamada contra ~5 do HORA_2. Como a
    # gravação é UPSERT, a janela larga faz a coleta se auto-curar: uma parada
    # de até 24h é recuperada sozinha no ciclo seguinte, sem furo permanente.
    telemetry_range: str = "HORA_24"
    collect_interval_minutes: int = 15
    station_sync_interval_hours: int = 24

    # Carga histórica (HidroSerieCotas).
    backfill_enabled: bool = True
    backfill_start_year: int = 1960
    backfill_request_delay: float = 0.35
    backfill_interval_hours: int = 24


settings = Settings()
