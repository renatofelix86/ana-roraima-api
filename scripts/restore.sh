#!/usr/bin/env bash
#
# Restaura um backup no banco.
#
#   ./scripts/restore.sh backups/hidrologia-20260909-183000.dump
#
# Ordem recomendada na VPS:
#   1. docker compose up -d db        # só o banco
#   2. ./scripts/restore.sh <arquivo>
#   3. docker compose up -d           # api e worker
#   4. docker compose run --rm api python -m app.bi_user
#
# Subir só o banco primeiro evita que o worker comece a coletar (e o init_db a
# criar tabelas) no meio da restauração.
#
# --clean --if-exists: derruba os objetos antigos antes de recriar, então
# restaurar por cima de um banco já inicializado funciona.

set -euo pipefail
cd "$(dirname "$0")/.."

FILE="${1:-}"
if [ -z "${FILE}" ] || [ ! -f "${FILE}" ]; then
    echo "uso: $0 <arquivo.dump>" >&2
    exit 1
fi

echo "ATENÇÃO: isto SOBRESCREVE o banco atual com o conteúdo de ${FILE}."
read -r -p "Digite 'sim' para continuar: " CONFIRMA
[ "${CONFIRMA}" = "sim" ] || { echo "Cancelado."; exit 1; }

echo "Restaurando ..."
# pg_restore devolve status != 0 por avisos inofensivos (DROP de objeto que
# ainda não existe, por exemplo), por isso o '|| true' com verificação depois.
docker compose exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges' \
  < "${FILE}" || true

echo
echo "Conferindo o que entrou:"
docker compose exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
   select (select count(*) from stations)    as estacoes,
          (select count(*) from measurements) as medicoes,
          (select min(measured_date) from measurements) as serie_desde,
          (select max(measured_date) from measurements) as serie_ate;"'

echo "Agora recrie o usuário de BI:"
echo "  docker compose run --rm api python -m app.bi_user"
