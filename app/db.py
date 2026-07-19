# plain english explanation: 
# This code is responsible for setting up the database connection and session management for a Python application using SQLAlchemy. 
# Loads environment variables, creates a database engine, initializes the database schema, and provides a function to get a session for interacting with the database.

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base

# .env is loaded by app/__init__.py, before any app.* submodule runs.

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./permitting.db")

engine = create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
