-- Converte o schema original (timestamps ingênuos, sem série histórica) para o
-- modelo com timestamptz, dia-calendário local e distinção de tipo/consistência.
--
-- Idempotente: cada passo verifica o estado atual antes de agir, então rodar de
-- novo é inofensivo. Em banco novo o create_all já criou tudo certo e este
-- arquivo vira no-op.

-- 1. Colunas novas -----------------------------------------------------------

ALTER TABLE stations ADD COLUMN IF NOT EXISTS basin_name VARCHAR(255);
ALTER TABLE stations ADD COLUMN IF NOT EXISTS telemetry_end TIMESTAMPTZ;

ALTER TABLE measurements ADD COLUMN IF NOT EXISTS measured_date DATE;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS reading_kind VARCHAR(20);
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS consistency_level INTEGER;

-- 2. timestamp ingênuo -> timestamptz ----------------------------------------
--
-- Os valores antigos foram gravados como o texto cru da ANA, que é hora de
-- Brasília (UTC-3). O AT TIME ZONE abaixo reinterpreta cada valor nesse fuso e
-- guarda o instante correto em UTC.

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = 'timestamp without time zone'
          AND (table_name, column_name) IN (
                ('stations', 'telemetry_start'),
                ('stations', 'source_updated_at'),
                ('stations', 'synced_at'),
                ('measurements', 'measured_at'),
                ('measurements', 'source_updated_at'),
                ('measurements', 'imported_at'),
                ('collection_logs', 'started_at'),
                ('collection_logs', 'finished_at')
              )
    LOOP
        EXECUTE 'ALTER TABLE ' || quote_ident(r.table_name)
             || ' ALTER COLUMN ' || quote_ident(r.column_name)
             || ' TYPE TIMESTAMPTZ USING ' || quote_ident(r.column_name)
             || ' AT TIME ZONE ' || quote_literal('America/Sao_Paulo');
    END LOOP;
END $$;

-- 3. Preenche as colunas novas nas linhas legadas ----------------------------

UPDATE measurements
SET measured_date = (measured_at AT TIME ZONE 'America/Boa_Vista')::date
WHERE measured_date IS NULL;

UPDATE measurements SET reading_kind = 'TELEMETRIA' WHERE reading_kind IS NULL;
UPDATE measurements SET consistency_level = 0 WHERE consistency_level IS NULL;

ALTER TABLE measurements ALTER COLUMN measured_date SET NOT NULL;
ALTER TABLE measurements ALTER COLUMN reading_kind SET NOT NULL;
ALTER TABLE measurements ALTER COLUMN consistency_level SET NOT NULL;
ALTER TABLE measurements ALTER COLUMN reading_kind SET DEFAULT 'TELEMETRIA';
ALTER TABLE measurements ALTER COLUMN consistency_level SET DEFAULT 0;

-- 4. Chave natural ampliada --------------------------------------------------

ALTER TABLE measurements
    DROP CONSTRAINT IF EXISTS uq_measurement_station_time_source;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_measurement_natural_key'
    ) THEN
        ALTER TABLE measurements
            ADD CONSTRAINT uq_measurement_natural_key
            UNIQUE (station_code, measured_at, source, reading_kind, consistency_level);
    END IF;
END $$;

-- 5. Índices de consulta -----------------------------------------------------

CREATE INDEX IF NOT EXISTS ix_measurements_station_time
    ON measurements (station_code, measured_at);
CREATE INDEX IF NOT EXISTS ix_measurements_station_date
    ON measurements (station_code, measured_date);
CREATE INDEX IF NOT EXISTS ix_measurements_source
    ON measurements (source);
CREATE INDEX IF NOT EXISTS ix_measurements_reading_kind
    ON measurements (reading_kind);
