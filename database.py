import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# PostgreSQL connection string (Reads from environment variable or falls back to standard Neon URL)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_XKCm9AqhEdI6@ep-green-rain-axjo16e3.c-4.us-east-2.aws.neon.tech/neondb?sslmode=require"
)

# Initialize engine using DATABASE_URL
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Single source of truth for Base
Base = declarative_base()


def get_db():
    """Dependency for obtaining a database session in FastAPI endpoints."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all database tables on application startup."""
    import models 
    Base.metadata.create_all(bind=engine)