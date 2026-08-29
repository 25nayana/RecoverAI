"""
Database connection setup for RecoverAI.
Uses SQLite for zero-config local/demo running. Swap DATABASE_URL for a
Postgres URL (e.g. postgresql://user:pass@host/db) to run the same code
against Postgres in production, per the architecture diagram.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./recoverai.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
