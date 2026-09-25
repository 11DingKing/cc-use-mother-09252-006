"""数据库连接：WAL、外键、忙碌超时；写会话以 BEGIN IMMEDIATE 串行化并发写者。

读、写使用两个引擎：写引擎的事务以 BEGIN IMMEDIATE 开启，使并发写操作
在 SQLite 上串行执行（配合 busy_timeout 等待而不是中途锁冲突），从而让
唯一约束与 CAS 更新成为并发安全的最终裁决者。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _make_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


@dataclass
class Database:
    read_engine: Engine
    write_engine: Engine
    read_factory: sessionmaker
    write_factory: sessionmaker

    def create_schema(self) -> None:
        Base.metadata.create_all(self.write_engine)

    def dispose(self) -> None:
        self.read_engine.dispose()
        self.write_engine.dispose()


def open_database(database_url: str) -> Database:
    """打开数据库并返回读/写会话工厂。数据库文件需为文件路径（内存库不支持读写分离）。"""
    read_engine = _make_engine(database_url)
    write_engine = _make_engine(database_url)
    if write_engine.dialect.name == "sqlite":

        @event.listens_for(write_engine, "begin")
        def _begin_immediate(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return Database(
        read_engine=read_engine,
        write_engine=write_engine,
        read_factory=sessionmaker(bind=read_engine, expire_on_commit=False),
        write_factory=sessionmaker(bind=write_engine, expire_on_commit=False),
    )
