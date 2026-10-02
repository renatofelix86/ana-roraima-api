#!/usr/bin/env bash
#
# Backup do banco para um arquivo em backups/.
#
#   ./scripts/backup.sh
#
# O pg_dump roda DENTRO do container, usando as ferramentas do PostgreSQL 16
# da própria imagem. Isso importa: um dump gerado por um pg_dump 17 (o que vem
# com o pgAdmin no Windows) pode não restaurar num servidor 16.
#
# Formato custom (-Fc): comprimido e restaurável com pg_restore.
# --no-owner e --no-privileges: o dump não carrega dono nem GRANTs, então
# restaurar não exige que o papel powerbi_ro já exista no destino. Ele é
# recriado depois com `python -m app.bi_user`.

set -euo pipefail
cd "$(dirname "$0")/.."

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="backups/hidrologia-${STAMP}.dump"

echo "Gerando ${OUT} ..."
docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' \
  > "${OUT}"

if [ ! -s "${OUT}" ]; then
    echo "ERRO: o dump saiu vazio. O container 'db' está de pé?" >&2
    rm -f "${OUT}"
    exit 1
fi

SIZE=$(du -h "${OUT}" | cut -f1)
echo "OK: ${OUT} (${SIZE})"

# Confere que o arquivo é um dump válido antes de você confiar nele.
docker compose exec -T db sh -c 'pg_restore --list' < "${OUT}" > /dev/null \
    && echo "Verificado: o arquivo abre como dump válido."
