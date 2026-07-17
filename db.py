"""
db.py — Capa centralizada de base de datos (Postgres / Supabase).

Reemplaza las 7 conexiones sqlite3.connect('entrenanfolio.db') dispersas
por un solo lugar. Usa un pool de conexiones para que aguante varios
usuarios a la vez (algo que SQLite no hacía).

La connection string sale de los secrets de Streamlit:
    st.secrets["DATABASE_URL"]
con formato:
    postgresql://USER:PASSWORD@HOST:5432/postgres

IMPORTANTE sobre placeholders:
    SQLite usa  ?   para los parámetros.
    Postgres usa %s  (psycopg2).
Por eso todas las queries que pasaban tuplas con ? ahora usan %s.
"""

import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from contextlib import contextmanager


@st.cache_resource
def get_pool():
    """
    Un solo pool para toda la app (cache_resource = se crea una vez).
    minconn=1, maxconn=10 alcanza de sobra para vos y tus amigos.
    """
    return SimpleConnectionPool(
        1, 10,
        dsn=st.secrets["DATABASE_URL"],
    )


@contextmanager
def get_conn():
    """
    Pide una conexión al pool y la devuelve al terminar.
    Uso:
        with get_conn() as conn:
            ...
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()  # limpia la transacción abortada antes de devolver la conexión
        raise
    finally:
        pool.putconn(conn)


def run_query(sql, params=None):
    """
    Para SELECT que devuelven un DataFrame.
    Reemplaza:  pd.read_sql(query, conn)
    """
    with get_conn() as conn:
        return pd.read_sql(sql, conn, params=params)


def execute(sql, params=None):
    """
    Para INSERT / UPDATE / DELETE / CREATE.
    Reemplaza:  conn.execute(...) + conn.commit()
    Hace commit automático.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()


def fetchone(sql, params=None):
    """
    Para traer una sola fila (ej. validar login).
    Reemplaza:  cursor.execute(...) + cursor.fetchone()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()


@st.cache_resource
def _get_engine():
    from sqlalchemy import create_engine
    return create_engine(st.secrets["DATABASE_URL"])


def df_to_table(df, table, if_exists="replace"):
    """
    Reemplaza:  df.to_sql('master_tickers', conn, if_exists='replace')
    Usa SQLAlchemy por debajo porque pandas lo necesita para escribir.
    """
    df.to_sql(table, _get_engine(), if_exists=if_exists, index=False)
