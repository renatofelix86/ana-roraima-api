from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def init_db() -> None:
    # Import necessário para registrar os models no metadata.
    from app import models  # noqa: F401
    from app.migrate import migrate

    # create_all cobre a instalação nova; migrate ajusta bancos já existentes
    # e (re)publica as views consumidas pelo Power BI.
    Base.metadata.create_all(bind=engine)
    migrate()
