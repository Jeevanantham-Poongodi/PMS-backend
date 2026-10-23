"""Dates in the hospital's configured time zone (constitution: single configured time zone)."""

from datetime import date

from psycopg import Cursor


def hospital_today(cur: Cursor, hospital_id: int) -> date:
    cur.execute(
        "select (now() at time zone time_zone)::date as today from public.hospitals where id = %s",
        (hospital_id,),
    )
    row = cur.fetchone()
    return row["today"] if row else date.today()
