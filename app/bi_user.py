"""Cria o usuário somente-leitura que o Power BI usa.

    docker compose run --rm api python -m app.bi_user

A senha vem de BI_READONLY_PASSWORD no .env -- nunca de um .sql versionado.
O papel recebe SELECT apenas nas views bi_*, não nas tabelas: um relatório
comprometido não expõe a coluna raw (o JSON bruto da ANA) nem escreve nada.

Nota: nenhum SQL daqui usa '%'. O psycopg trata '%' como marcador de
parâmetro e recusa a query mesmo sem parâmetros, então em vez de format('%I')
usa-se quote_ident()/quote_literal(), que rodam no servidor e também evitam
concatenar nome de papel e senha crus.
"""

import logging
import os
import sys

from sqlalchemy import text

from app.db import engine

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("bi_user")

ROLE = os.getenv("BI_READONLY_USER", "powerbi_ro")


def main() -> None:
    password = os.getenv("BI_READONLY_PASSWORD", "")
    if len(password) < 12:
        sys.exit("Defina BI_READONLY_PASSWORD no .env com ao menos 12 caracteres.")

    with engine.begin() as conn:
        database = conn.execute(text("SELECT current_database()")).scalar()

        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}
        ).scalar()

        verb = "ALTER ROLE " if exists else "CREATE ROLE "
        stmt = conn.execute(
            text(
                "SELECT :verb || quote_ident(:role)"
                " || ' LOGIN PASSWORD ' || quote_literal(:pwd)"
            ),
            {"verb": verb, "role": ROLE, "pwd": password},
        ).scalar()
        conn.exec_driver_sql(stmt)
        logger.info("Papel %s %s.", ROLE, "atualizado" if exists else "criado")

        role_sql = f'"{ROLE}"'
        conn.exec_driver_sql(
            f'GRANT CONNECT ON DATABASE "{database}" TO {role_sql}'
        )
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role_sql}")
        # Zera qualquer acesso a tabela: a superfície é só a camada de views.
        conn.exec_driver_sql(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_sql}"
        )

        views = (
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.views "
                    "WHERE table_schema = 'public' "
                    "AND left(table_name, 3) = 'bi_' ORDER BY table_name"
                )
            )
            .scalars()
            .all()
        )

        for view in views:
            conn.exec_driver_sql(f'GRANT SELECT ON "{view}" TO {role_sql}')

        logger.info("SELECT concedido em %s views: %s", len(views), ", ".join(views))
        # Porta vista de fora: a publicada no host, não a 5432 interna.
        porta = os.getenv("DB_HOST_PORT", "5433")
        logger.info(
            "Power BI / pgAdmin -> host=<servidor> porta=%s banco=%s usuario=%s",
            porta,
            database,
            ROLE,
        )


if __name__ == "__main__":
    main()
