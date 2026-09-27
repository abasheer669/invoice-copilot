"""Postgres connections that run as one least-privilege role each.

The roles and their grants are defined in db/03_roles.sql.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, get_args

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from ap_agent.config import Settings, get_settings

Role = Literal["ap_reader", "ap_writer", "ap_runtime", "ap_ingest"]


@contextmanager
def connect(role: Role, settings: Settings | None = None) -> Iterator[psycopg.Connection[DictRow]]:
    """Open a connection acting as `role`; commits on success, rolls back on error."""
    if role not in get_args(Role):
        raise ValueError(f"unknown role: {role}")
    settings = settings or get_settings()
    with psycopg.connect(
        settings.database_url.get_secret_value(), row_factory=dict_row, connect_timeout=5
    ) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        yield conn
