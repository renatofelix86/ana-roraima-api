#!/usr/bin/env bash
#
# Levanta o que já está em uso na VPS antes de subir este projeto.
#
#   ./scripts/check-ports.sh
#
# Não depende do repositório: dá para copiar só este arquivo para a VPS.

# Lista as portas em escuta. Usa ss no Linux e cai para `netstat -an`, que é o
# denominador comum -- as flags -tlnp não existem no netstat do Windows e o
# script chegaria a responder "livre" para uma porta ocupada.
listening() {
    if command -v ss > /dev/null 2>&1; then
        ss -tln 2>/dev/null
    else
        netstat -an 2>/dev/null | grep -iE "listen"
    fi
}

ocupada() {
    listening | grep -qE "[:.]${1}[[:space:]]"
}

echo "=================================================="
echo " 1. CONTAINERS E PORTAS PUBLICADAS"
echo "=================================================="
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null \
    || echo "  (docker inacessível para este usuário -- tente com sudo)"

echo
echo "=================================================="
echo " 2. STACKS COMPOSE JÁ EXISTENTES"
echo "=================================================="
docker compose ls -a 2>/dev/null || echo "  (indisponível)"

echo
echo "=================================================="
echo " 3. VOLUMES (colisão de nome)"
echo "=================================================="
docker volume ls 2>/dev/null | grep -iE "roraima|hidrolog|postgres" \
    || echo "  nenhum volume com nome parecido -- sem colisão"

echo
echo "=================================================="
echo " 4. PORTAS EM ESCUTA NO HOST (Docker ou não)"
echo "=================================================="
if command -v ss > /dev/null 2>&1; then
    ss -tlnp 2>/dev/null || ss -tln 2>/dev/null
else
    listening
fi

echo
echo "=================================================="
echo " 5. AS PORTAS QUE ESTE PROJETO QUER"
echo "=================================================="
for p in 8000 5433; do
    if ocupada "${p}"; then
        echo "  ${p}  OCUPADA  <- precisa trocar"
    else
        echo "  ${p}  livre"
    fi
done

echo
echo "  Alternativas livres, se precisar trocar:"
LIVRES=""
for p in 8001 8010 8081 8090 5434 5435 5440 5443; do
    ocupada "${p}" || LIVRES="${LIVRES} ${p}"
done
echo "   ${LIVRES:-  (nenhuma das testadas está livre)}"
