#!/usr/bin/env bash
#
# Gera o certificado TLS que o Postgres usa para aceitar conexões da internet.
#
#   ./scripts/setup-tls.sh
#
# Rode UMA vez na VPS, antes de subir o banco com a porta exposta. O par de
# chaves fica em postgres/certs/ e NÃO vai para o git.
#
# Certificado autoassinado, válido por 10 anos. Isso basta porque o Power BI e
# o pgAdmin usam o certificado para *cifrar* o tráfego, não para validar
# identidade (conectam com "Trust Server Certificate" / sslmode=require). O que
# importa é que a senha não trafegue em texto claro.
#
# Permissões: o Postgres recusa iniciar se o arquivo da chave for legível por
# outros. Ele aceita a chave pertencente ao root com modo 0600.

set -euo pipefail
cd "$(dirname "$0")/.."

# O Git Bash do Windows converte argumentos que começam com "/" em caminho
# do Windows, transformando "/CN=host" em "C:/Program Files/Git/CN=host".
# Inofensivo no Linux, essencial se alguém gerar o certificado no Windows.
export MSYS_NO_PATHCONV=1

DIR="postgres/certs"
CRT="${DIR}/server.crt"
KEY="${DIR}/server.key"
CN="${1:-hidrologia.cbm.rr.gov.br}"

mkdir -p "${DIR}"

if [ -f "${KEY}" ]; then
    echo "Certificado já existe em ${DIR}. Apague os arquivos para gerar outro."
    openssl x509 -in "${CRT}" -noout -subject -dates
    exit 0
fi

echo "Gerando certificado para CN=${CN} ..."
openssl req -new -x509 -days 3650 -nodes \
    -out "${CRT}" -keyout "${KEY}" \
    -subj "/CN=${CN}" \
    -addext "subjectAltName=DNS:${CN},DNS:localhost,IP:127.0.0.1"

chmod 600 "${KEY}"
chmod 644 "${CRT}"

# Em Linux, a chave precisa pertencer ao root (modo 600) para o Postgres
# aceitá-la montada de fora. Em Windows/Docker Desktop este chown falha e é
# inofensivo -- o mount não carrega permissões do host.
chown root:root "${KEY}" 2>/dev/null || true

echo "OK:"
openssl x509 -in "${CRT}" -noout -subject -dates
ls -l "${DIR}"
