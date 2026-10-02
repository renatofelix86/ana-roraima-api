-- Executado antes de sql/bi_views.sql.
--
-- CREATE OR REPLACE VIEW falha quando o conjunto ou a ordem das colunas muda
-- ("cannot change name of view column"). Derrubar antes deixa a camada de BI
-- livremente editável: basta alterar bi_views.sql e reiniciar.
--
-- CASCADE porque bi_referencia_sazonal se apoia em bi_serie_diaria.

DROP VIEW IF EXISTS bi_status_backfill    CASCADE;
DROP VIEW IF EXISTS bi_status_coleta      CASCADE;
DROP VIEW IF EXISTS bi_dim_calendario     CASCADE;
DROP VIEW IF EXISTS bi_referencia_sazonal CASCADE;
DROP VIEW IF EXISTS bi_ultima_leitura     CASCADE;
DROP VIEW IF EXISTS bi_serie_diaria       CASCADE;
DROP VIEW IF EXISTS bi_fato_medicao       CASCADE;
DROP VIEW IF EXISTS bi_dim_estacao        CASCADE;
