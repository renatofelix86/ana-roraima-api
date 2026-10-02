#!/usr/bin/env bash
#
# Copia para o Postgres o certificado Let's Encrypt que o Traefik já emite e
# renova para o mesmo domínio, e recarrega o banco SEM reiniciar.
#
#   ./scripts/sync-cert.sh
#
# Por que: o certificado autoassinado de scripts/setup-tls.sh cifra o tráfego,
# mas o Windows não confia nele -- e o Power BI (driver Npgsql) valida a cadeia
# pelo repositório do Windows. Resultado: "The remote certificate is invalid
# according to the validation procedure", sem opção de ignorar, porque o
# servidor recusa conexão sem TLS. Com o certificado do Let's Encrypt a cadeia
# fecha em ISRG Root X1, que o Windows e a nuvem da Microsoft reconhecem.
#
# Rode também por cron, diariamente: o Traefik renova o certificado ~30 dias
# antes de vencer, e este script só age quando o arquivo realmente mudou.
#
# Variáveis opcionais:
#   API_DOMAIN     domínio (padrão: o do .env)
#   ACME_RESOLVER  nome do resolver no Traefik (padrão: myresolver)
#   ACME_JSON      caminho do acme.json no host (padrão: detectado pelo
#                  volume /letsencrypt do container `traefik`)

set -euo pipefail
cd "$(dirname "$0")/.."

# Git Bash no Windows reescreve caminhos iniciados por "/"; desliga isso e
# converte os caminhos do host explicitamente (inofensivo no Linux).
export MSYS_NO_PATHCONV=1
hostpath() { if command -v cygpath > /dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi; }

DOMAIN="${API_DOMAIN:-$(grep -E '^API_DOMAIN=' .env 2>/dev/null | cut -d= -f2- || true)}"
RESOLVER="${ACME_RESOLVER:-myresolver}"
ACME="${ACME_JSON:-}"
IMAGE="${APP_IMAGE:-ana-roraima-api-api}"

[ -n "${DOMAIN}" ] || { echo "ERRO: defina API_DOMAIN no .env." >&2; exit 1; }

if [ -z "${ACME}" ]; then
    SRC=$(docker inspect traefik \
        -f '{{range .Mounts}}{{if eq .Destination "/letsencrypt"}}{{.Source}}{{end}}{{end}}' \
        2>/dev/null || true)
    [ -n "${SRC}" ] && ACME="${SRC}/acme.json"
fi
[ -f "${ACME}" ] || { echo "ERRO: acme.json não encontrado (${ACME:-?}). Informe ACME_JSON=/caminho/acme.json" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "${TMP}"' EXIT

# O parsing roda na imagem da própria aplicação, que já tem Python: nada a
# instalar na VPS (nem jq).
docker run --rm -i \
    -v "$(hostpath "${ACME}"):/acme.json:ro" \
    -v "$(hostpath "${TMP}"):/out" \
    "${IMAGE}" python - "${DOMAIN}" "${RESOLVER}" <<'PY'
import base64, json, ssl, sys

dominio, resolver = sys.argv[1], sys.argv[2]
with open("/acme.json") as f:
    dados = json.load(f)

if resolver not in dados:
    sys.exit(f"resolver '{resolver}' não existe no acme.json (há: {', '.join(dados) or 'nenhum'})")

for cert in dados[resolver].get("Certificates") or []:
    d = cert.get("domain") or {}
    if dominio == d.get("main") or dominio in (d.get("sans") or []):
        # O Traefik guarda a cadeia completa (folha + intermediário) em base64.
        with open("/out/server.crt", "wb") as f:
            f.write(base64.b64decode(cert["certificate"]))
        with open("/out/server.key", "wb") as f:
            f.write(base64.b64decode(cert["key"]))
        # Recusa antes de tocar em qualquer arquivo real: certificado ilegível
        # ou chave de outro par derrubaria o banco na próxima reinicialização.
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
                "/out/server.crt", "/out/server.key"
            )
        except (ssl.SSLError, OSError, ValueError) as exc:
            sys.exit(f"certificado/chave inválidos ou de pares diferentes: {exc}")
        break
else:
    sys.exit(f"nenhum certificado para {dominio} no resolver {resolver}")
PY

if ! openssl x509 -in "$(hostpath "${TMP}/server.crt")" -noout -checkend 86400 > /dev/null 2>&1; then
    echo "ERRO: o certificado extraído está vencido ou vence em menos de 24h. Nada foi alterado." >&2
    exit 1
fi

if cmp -s "${TMP}/server.crt" postgres/certs/server.crt 2>/dev/null; then
    echo "Certificado já está atualizado. Nada a fazer."
    exit 0
fi

mkdir -p postgres/certs
install -m 644 "${TMP}/server.crt" postgres/certs/server.crt
install -m 600 "${TMP}/server.key" postgres/certs/server.key
echo "Certificado gravado em postgres/certs/:"
openssl x509 -in postgres/certs/server.crt -noout -subject -issuer -enddate

# Recarga a quente: o container lê os arquivos de /certs-src só na partida
# (ver docker-compose.exposed.yml), então repetimos a cópia e pedimos ao
# Postgres para reler a configuração. Desde o PostgreSQL 10 o SIGHUP também
# recarrega os arquivos de TLS -- as conexões novas já saem com o certificado
# novo, e as abertas não caem.
if docker compose exec -T db test -d /certs-src 2> /dev/null; then
    docker compose exec -T db sh -ec '
        cp /certs-src/server.crt /run/certs/server.crt
        cp /certs-src/server.key /run/certs/server.key
        chown postgres:postgres /run/certs/server.crt /run/certs/server.key
        chmod 644 /run/certs/server.crt
        chmod 600 /run/certs/server.key
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select pg_reload_conf();" > /dev/null'
    echo "Postgres recarregado com o certificado novo (sem reiniciar)."
else
    echo "Banco não está no modo exposto; o certificado vale na próxima subida."
fi
