"""Relational persistence models and connection pool."""
import os
from datetime import datetime,timezone
from sqlalchemy import create_engine,String,Text,DateTime,ForeignKey,Integer,text
from sqlalchemy.orm import DeclarativeBase,Mapped,mapped_column,sessionmaker

DATABASE_URL=os.getenv('DATABASE_URL','postgresql+psycopg://ragsearch:change-me@localhost:5432/ragsearch')
engine=create_engine(DATABASE_URL,pool_pre_ping=True,pool_size=int(os.getenv('DB_POOL_SIZE','20')),max_overflow=int(os.getenv('DB_MAX_OVERFLOW','20')))  # sized for the 24-thread search pool + 16-thread io pool, each opening its own Session
Db=sessionmaker(engine,expire_on_commit=False)
class Base(DeclarativeBase):pass
class User(Base):
 __tablename__='users';id:Mapped[int]=mapped_column(primary_key=True);username:Mapped[str]=mapped_column(String(100),unique=True,index=True);password_hash:Mapped[str]=mapped_column(String(300));role:Mapped[str]=mapped_column(String(20),default='reader')
class Doc(Base):
 __tablename__='documents';id:Mapped[str]=mapped_column(String(40),primary_key=True);title:Mapped[str]=mapped_column(String(500));filename:Mapped[str]=mapped_column(String(255));checksum:Mapped[str]=mapped_column(String(64));version:Mapped[int]=mapped_column(Integer,default=1);status:Mapped[str]=mapped_column(String(30),default='processing');owner:Mapped[str]=mapped_column(String(100));acl:Mapped[str]=mapped_column(Text,default='*');created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc));snapshot_path:Mapped[str]=mapped_column(Text);processing_started_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
class Chunk(Base):
 __tablename__='chunks';id:Mapped[str]=mapped_column(String(64),primary_key=True);doc_id:Mapped[str]=mapped_column(ForeignKey('documents.id',ondelete='CASCADE'),index=True);page:Mapped[int]=mapped_column(Integer);text:Mapped[str]=mapped_column(Text);token_estimate:Mapped[int]=mapped_column(Integer)
class Setting(Base):
 __tablename__='settings';key:Mapped[str]=mapped_column(String(100),primary_key=True);encrypted_value:Mapped[str]=mapped_column(Text)

def ensure_schema():
 """create_all does not alter existing tables, so additive columns are applied idempotently here (API startup, worker start, CLI)."""
 Base.metadata.create_all(engine)
 with engine.begin() as conn:
  conn.execute(text('ALTER TABLE documents ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ'))
  conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chunks_search_text ON chunks USING GIN (to_tsvector('english', text))"))
