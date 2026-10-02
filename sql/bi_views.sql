-- Camada de consumo do Power BI.
--
-- Reaplicada a cada start (CREATE OR REPLACE): editar este arquivo e reiniciar
-- já publica a nova versão.
--
-- Convenção: todo horário exposto aqui é *naive em hora de Roraima*
-- (measured_at AT TIME ZONE 'America/Boa_Vista'). O conector Npgsql entrega
-- timestamptz como DateTimeOffset e o Power BI converte para o fuso da máquina
-- que faz o refresh -- o que deslocaria o relatório ao publicar no Service.
-- Entregando naive local, o valor chega exatamente como foi medido. Quem
-- precisar do instante absoluto tem medido_em_utc ao lado.

CREATE OR REPLACE VIEW bi_dim_estacao AS
SELECT
    s.code                                   AS codigo_estacao,
    s.name                                   AS estacao,
    s.river_name                             AS rio,
    s.municipality_name                      AS municipio,
    s.uf,
    s.basin_name                             AS sub_bacia,
    s.latitude::float8                       AS latitude,
    s.longitude::float8                      AS longitude,
    s.station_type                           AS tipo_estacao,
    s.operator_acronym                       AS operadora,
    s.responsible_acronym                    AS responsavel,
    s.operating                              AS operando,
    s.telemetered                            AS telemetrica,
    (s.telemetry_start AT TIME ZONE 'America/Boa_Vista') AS telemetria_inicio,
    (s.telemetry_end   AT TIME ZONE 'America/Boa_Vista') AS telemetria_fim,
    (s.synced_at       AT TIME ZONE 'America/Boa_Vista') AS sincronizado_em
FROM stations s;

CREATE OR REPLACE VIEW bi_fato_medicao AS
SELECT
    m.station_code                                     AS codigo_estacao,
    (m.measured_at AT TIME ZONE 'America/Boa_Vista')   AS medido_em,
    m.measured_at                                      AS medido_em_utc,
    m.measured_date                                    AS data_medicao,
    EXTRACT(HOUR FROM (m.measured_at AT TIME ZONE 'America/Boa_Vista'))::int AS hora,
    m.level_cm::float8                                 AS nivel_cm,
    (m.level_cm / 100.0)::float8                       AS nivel_m,
    m.flow_m3s::float8                                 AS vazao_m3s,
    m.rainfall_mm::float8                              AS chuva_mm,
    m.source                                           AS origem,
    m.reading_kind                                     AS tipo_leitura,
    m.consistency_level                                AS nivel_consistencia,
    CASE m.consistency_level
        WHEN 2 THEN 'Consistido'
        WHEN 1 THEN 'Bruto'
        ELSE 'Telemetria'
    END                                                AS consistencia,
    m.level_status                                     AS status_nivel,
    (m.imported_at AT TIME ZONE 'America/Boa_Vista')   AS importado_em
FROM measurements m;

-- Série diária consolidada.
--
-- Uma linha por estação/dia. Quando a ANA já publicou a média diária da série
-- histórica, ela prevalece (é o valor curado da agência, com nível de
-- consistência); caso contrário, agrega-se a telemetria do dia.

CREATE OR REPLACE VIEW bi_serie_diaria AS
WITH telemetria AS (
    SELECT
        station_code,
        measured_date,
        avg(level_cm)    AS nivel_medio,
        min(level_cm)    AS nivel_min,
        max(level_cm)    AS nivel_max,
        avg(flow_m3s)    AS vazao_media,
        sum(rainfall_mm) AS chuva_total,
        count(*)         AS leituras
    FROM measurements
    WHERE source = 'ANA_TELEMETRIA' AND level_cm IS NOT NULL
    GROUP BY station_code, measured_date
),
historico AS (
    SELECT DISTINCT ON (station_code, measured_date)
        station_code,
        measured_date,
        level_cm AS nivel_medio,
        consistency_level
    FROM measurements
    WHERE source = 'ANA_COTAS'
      AND reading_kind = 'MEDIA_DIARIA'
      AND level_cm IS NOT NULL
    ORDER BY station_code, measured_date, consistency_level DESC
)
SELECT
    COALESCE(h.station_code, t.station_code)   AS codigo_estacao,
    COALESCE(h.measured_date, t.measured_date) AS data_medicao,
    COALESCE(h.nivel_medio, t.nivel_medio)::float8           AS nivel_medio_cm,
    (COALESCE(h.nivel_medio, t.nivel_medio) / 100.0)::float8 AS nivel_medio_m,
    t.nivel_min::float8                        AS nivel_min_cm,
    t.nivel_max::float8                        AS nivel_max_cm,
    t.vazao_media::float8                      AS vazao_media_m3s,
    t.chuva_total::float8                      AS chuva_total_mm,
    COALESCE(t.leituras, 0)                    AS leituras_telemetricas,
    CASE
        WHEN h.station_code IS NOT NULL AND h.consistency_level = 2 THEN 'Historico consistido'
        WHEN h.station_code IS NOT NULL THEN 'Historico bruto'
        ELSE 'Telemetria'
    END                                        AS origem
FROM historico h
FULL OUTER JOIN telemetria t
    ON t.station_code = h.station_code
   AND t.measured_date = h.measured_date;

CREATE OR REPLACE VIEW bi_ultima_leitura AS
SELECT DISTINCT ON (m.station_code)
    m.station_code                                   AS codigo_estacao,
    s.name                                           AS estacao,
    s.river_name                                     AS rio,
    s.municipality_name                              AS municipio,
    s.latitude::float8                               AS latitude,
    s.longitude::float8                              AS longitude,
    (m.measured_at AT TIME ZONE 'America/Boa_Vista') AS medido_em,
    m.level_cm::float8                               AS nivel_cm,
    (m.level_cm / 100.0)::float8                     AS nivel_m,
    m.flow_m3s::float8                               AS vazao_m3s,
    m.rainfall_mm::float8                            AS chuva_mm,
    (EXTRACT(EPOCH FROM (now() - m.measured_at)) / 60.0)::int AS atraso_minutos
FROM measurements m
JOIN stations s ON s.code = m.station_code
WHERE m.source = 'ANA_TELEMETRIA'
ORDER BY m.station_code, m.measured_at DESC;

-- Referência sazonal: base da faixa "o nível de hoje está acima ou abaixo do
-- normal para esta época do ano?".

CREATE OR REPLACE VIEW bi_referencia_sazonal AS
SELECT
    codigo_estacao,
    EXTRACT(MONTH FROM data_medicao)::int AS mes,
    EXTRACT(DAY   FROM data_medicao)::int AS dia,
    count(*)                                     AS anos_observados,
    avg(nivel_medio_cm)::float8                  AS nivel_medio_cm,
    min(nivel_medio_cm)::float8                  AS nivel_minimo_cm,
    max(nivel_medio_cm)::float8                  AS nivel_maximo_cm,
    percentile_cont(0.10) WITHIN GROUP (ORDER BY nivel_medio_cm)::float8 AS p10_cm,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY nivel_medio_cm)::float8 AS mediana_cm,
    percentile_cont(0.90) WITHIN GROUP (ORDER BY nivel_medio_cm)::float8 AS p90_cm
FROM bi_serie_diaria
WHERE nivel_medio_cm IS NOT NULL
GROUP BY codigo_estacao, mes, dia;

CREATE OR REPLACE VIEW bi_dim_calendario AS
WITH limites AS (
    SELECT
        COALESCE(min(measured_date), CURRENT_DATE) AS ini,
        GREATEST(COALESCE(max(measured_date), CURRENT_DATE), CURRENT_DATE) AS fim
    FROM measurements
),
dias AS (
    SELECT generate_series(ini, fim, INTERVAL '1 day')::date AS data FROM limites
)
SELECT
    data,
    EXTRACT(YEAR    FROM data)::int AS ano,
    EXTRACT(MONTH   FROM data)::int AS mes_numero,
    (ARRAY['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho',
           'Agosto','Setembro','Outubro','Novembro','Dezembro'])
        [EXTRACT(MONTH FROM data)::int] AS mes,
    EXTRACT(QUARTER FROM data)::int AS trimestre,
    EXTRACT(DAY     FROM data)::int AS dia,
    EXTRACT(DOY     FROM data)::int AS dia_do_ano,
    (ARRAY['Segunda','Terca','Quarta','Quinta','Sexta','Sabado','Domingo'])
        [EXTRACT(ISODOW FROM data)::int] AS dia_semana,
    to_char(data, 'YYYY-MM') AS ano_mes,
    -- Regime hidrologico de Roraima: cheia concentrada de maio a agosto.
    CASE WHEN EXTRACT(MONTH FROM data)::int BETWEEN 5 AND 8
         THEN 'Cheia' ELSE 'Seca' END AS periodo_hidrologico
FROM dias;

CREATE OR REPLACE VIEW bi_status_coleta AS
SELECT
    job,
    status,
    records                                        AS registros,
    (started_at  AT TIME ZONE 'America/Boa_Vista') AS iniciado_em,
    (finished_at AT TIME ZONE 'America/Boa_Vista') AS concluido_em,
    EXTRACT(EPOCH FROM (finished_at - started_at))::int AS duracao_segundos,
    details                                        AS detalhes
FROM collection_logs;

CREATE OR REPLACE VIEW bi_status_backfill AS
SELECT
    b.station_code    AS codigo_estacao,
    s.name            AS estacao,
    b.first_year_seen AS primeiro_ano,
    b.last_year_done  AS ultimo_ano_concluido,
    b.completed       AS concluido,
    b.records         AS registros,
    b.last_error      AS ultimo_erro,
    (b.updated_at AT TIME ZONE 'America/Boa_Vista') AS atualizado_em
FROM backfill_progress b
JOIN stations s ON s.code = b.station_code;
