# models.py
from sqlalchemy import Column, String, JSON, TIMESTAMP, text
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Job(Base):
    __tablename__ = "jobs"
    id = Column(String, primary_key=True)
    payload = Column(JSON)
    status = Column(String, default="queued")
    result = Column(JSON)
    created = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    updated = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
