"""Prepara os papéis do banco antes de expor a porta à internet.

    docker compose run --rm api python -m app.harden_db

Por que existe
--------------
O papel `hidrologia` inicializou o cluster, e o PostgreSQL **não permite**
remover o atributo SUPERUSER do usuário de bootstrap::

    permission denied to alter role
    DETAIL: The bootstrap user must have the SUPERUSER attribute.

Ou seja, ele será sempre superusuário -- e superusuário no Postgres significa
execução de comando no servidor (COPY FROM PROGRAM). Expor esse papel à
internet colocaria em risco a VPS inteira, não só os dados hidrológicos.

A solução é não expor esse papel. Este script cria `hidro_app`, um papel comum
que passa a ser dono dos objetos e a fazer todo o trabalho da aplicação. O
`pg_hba.conf` do projeto só aceita `hidro_app` e `powerbi_ro` por TCP, ambos
com TLS; `hidrologia` fica alcançável apenas pelo socket local, dentro do
container, para administração e emergências.

Depois de rodar, aponte o DATABASE_URL do .env para `hidro_app`.

Idempotente: rodar de novo apenas reaplica posse e permissões.
"""

import logging
import os
import sys

from sqlalchemy import text

from app.db import engine

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("harden_db")

APP_ROLE = os.getenv("PG_APP_USER", "hidro_app")


def main() -> None:
    password = os.getenv("PG_APP_PASSWORD", "")
    if len(password) < 16:
        sys.exit(
            "Defina PG_APP_PASSWORD no .env com ao menos 16 caracteres.\n"
            "Gere com: openssl rand -hex 24"
        )

    with engine.begin() as conn:
        atual = conn.execute(text("SELECT current_user")).scalar()
        if atual == APP_ROLE:
            sys.exit(
                f"Já conectado como {APP_ROLE}. Rode como o dono original "
                "(hidrologia) antes de trocar o DATABASE_URL."
            )

        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}
        ).scalar()

        # CREATEROLE (sem SUPERUSER) porque app.bi_user cria o powerbi_ro.
        # No PostgreSQL 16 um CREATEROLE não consegue conceder superusuário.
        verb = "ALTER ROLE " if exists else "CREATE ROLE "
        stmt = conn.execute(
            text(
                "SELECT :verb || quote_ident(:role)"
                " || ' LOGIN NOSUPERUSER CREATEROLE PASSWORD '"
                " || quote_literal(:pwd)"
            ),
            {"verb": verb, "role": APP_ROLE, "pwd": password},
        ).scalar()
        conn.exec_driver_sql(stmt)
        logger.info("Papel %s %s.", APP_ROLE, "atualizado" if exists else "criado")

        role_sql = f'"{APP_ROLE}"'
        conn.exec_driver_sql(f"GRANT USAGE, CREATE ON SCHEMA public TO {role_sql}")

        # No PostgreSQL 16, ter CREATEROLE não basta para mexer num papel que
        # outro criou: é preciso a opção ADMIN sobre ele. Sem isto,
        # app.bi_user falharia ao reaplicar a senha do powerbi_ro, que foi
        # criado pelo dono original.
        bi_role = os.getenv("BI_READONLY_USER", "powerbi_ro")
        if conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": bi_role}
        ).scalar():
            conn.exec_driver_sql(
                f'GRANT "{bi_role}" TO {role_sql} WITH ADMIN OPTION'
            )
            logger.info("Opção ADMIN sobre %s concedida a %s.", bi_role, APP_ROLE)

        # Migrações fazem ALTER TABLE e DROP VIEW, que exigem posse -- não
        # basta GRANT. Por isso a transferência de dono, objeto a objeto.
        movidos = 0
        for kind, sql in (
            ("TABLE", "SELECT tablename FROM pg_tables WHERE schemaname='public'"),
            ("VIEW", "SELECT viewname FROM pg_views WHERE schemaname='public'"),
            (
                "SEQUENCE",
                "SELECT sequencename FROM pg_sequences WHERE schemaname='public'",
            ),
        ):
            for (name,) in conn.execute(text(sql)).all():
                conn.exec_driver_sql(
                    f'ALTER {kind} public."{name}" OWNER TO {role_sql}'
                )
                movidos += 1
        logger.info("Posse transferida: %s objetos agora pertencem a %s.", movidos, APP_ROLE)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rolname, rolsuper, rolcreaterole FROM pg_roles"
                " WHERE rolcanlogin ORDER BY rolsuper DESC, rolname"
            )
        ).all()

    logger.info("Papéis com login:")
    for name, is_super, can_create in rows:
        alcance = "socket local apenas" if is_super else "TCP com TLS"
        logger.info(
            "  %-12s %-10s %s%s",
            name,
            "SUPERUSER" if is_super else "comum",
            alcance,
            " (createrole)" if can_create and not is_super else "",
        )

    logger.info("")
    logger.info("PRÓXIMO PASSO: no .env, troque o usuário do DATABASE_URL para %s", APP_ROLE)
    logger.info("  DATABASE_URL=postgresql+psycopg://%s:<senha>@db:5432/<banco>", APP_ROLE)
    logger.info("Depois: docker compose up -d")


if __name__ == "__main__":
    main()
