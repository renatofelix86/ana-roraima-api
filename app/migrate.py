"""Migrações de schema sem dependência extra.

O ``create_all`` do SQLAlchemy cria tabelas que não existem, mas nunca altera
uma tabela existente. Como o schema mudou (timestamptz, dia-calendário local,
tipo de leitura e nível de consistência), bancos já criados precisam de um
passo explícito -- é isto aqui.

Arquivos em ``migrations/`` rodam uma única vez, em ordem de nome, e ficam
registrados em ``schema_migrations``. Já ``sql/bi_views.sql`` é reaplicado a
cada start, porque é tudo ``CREATE OR REPLACE``: assim, mexer nas views do
Power BI é só editar o arquivo e reiniciar.
"""

import logging
import os
from pathlib import Path

from sqlalchemy import text

from app.db import engine

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
DROP_VIEWS_FILE = ROOT / "sql" / "00_drop_views.sql"
VIEWS_FILE = ROOT / "sql" / "bi_views.sql"


def _ensure_registry(conn) -> set[str]:
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    rows = conn.execute(text("SELECT version FROM schema_migrations"))
    return {r[0] for r in rows}


def run_migrations() -> None:
    if not MIGRATIONS_DIR.is_dir():
        logger.warning("Diretório de migrações não encontrado: %s", MIGRATIONS_DIR)
        applied_files: list[Path] = []
    else:
        applied_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    with engine.begin() as conn:
        done = _ensure_registry(conn)

        for path in applied_files:
            if path.name in done:
                continue
            logger.info("Aplicando migração %s", path.name)
            # exec_driver_sql envia o SQL cru ao psycopg: o arquivo tem blocos
            # DO $$ ... $$ e casts ::tipo, que não devem passar pelo parser de
            # bind params do SQLAlchemy.
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": path.name},
            )
            logger.info("Migração %s aplicada.", path.name)


def apply_bi_views() -> None:
    if not VIEWS_FILE.is_file():
        logger.warning("Arquivo de views não encontrado: %s", VIEWS_FILE)
        return

    with engine.begin() as conn:
        if DROP_VIEWS_FILE.is_file():
            conn.exec_driver_sql(DROP_VIEWS_FILE.read_text(encoding="utf-8"))
        conn.exec_driver_sql(VIEWS_FILE.read_text(encoding="utf-8"))
        _regrant_bi_role(conn)
    logger.info("Views de BI atualizadas.")


def _regrant_bi_role(conn) -> None:
    """Devolve ao papel do Power BI o SELECT nas views recém-recriadas.

    As views são derrubadas e recriadas a cada subida da api e do worker, e
    um GRANT pertence ao objeto: some junto com ele. Sem isto, o powerbi_ro
    perdia o acesso a cada restart -- e como o PostgreSQL esconde do catálogo
    o que o usuário não pode ler, o Power BI mostrava o banco vazio, sem erro.

    Roda na mesma transação da recriação, então não existe janela em que as
    views estejam de pé sem permissão. Se o papel ainda não existe (antes do
    primeiro app.bi_user), não faz nada.

    Concede só nas views bi_*, nunca nas tabelas: ALTER DEFAULT PRIVILEGES
    resolveria com menos código, mas valeria também para tabelas novas e
    exporia a coluna raw.
    """
    role = os.getenv("BI_READONLY_USER", "powerbi_ro")
    if not conn.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).scalar():
        return

    role_sql = '"' + role.replace('"', '""') + '"'
    conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role_sql}")
    views = (
        conn.execute(
            text(
                "SELECT viewname FROM pg_views"
                " WHERE schemaname = 'public' AND left(viewname, 3) = 'bi_'"
            )
        )
        .scalars()
        .all()
    )
    for view in views:
        conn.exec_driver_sql(f'GRANT SELECT ON public."{view}" TO {role_sql}')
    logger.info("SELECT reaplicado a %s em %s views.", role, len(views))

def migrate() -> None:
    run_migrations()
    apply_bi_views()
