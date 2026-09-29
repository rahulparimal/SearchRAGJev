"""Relational persistence models and connection pool."""
import os
from datetime import datetime,timezone
from sqlalchemy import create_engine,String,Text,DateTime,ForeignKey,Integer
from sqlalchemy.orm import DeclarativeBase,Mapped,mapped_column,sessionmaker

DATABASE_URL=os.getenv('DATABASE_URL','postgresql+psycopg://ragsearch:change-me@localhost:5432/ragsearch')
engine=create_engine(DATABASE_URL,pool_pre_ping=True,pool_size=10,max_overflow=10)
Db=sessionmaker(engine,expire_on_commit=False)
class Base(DeclarativeBase):pass
class User(Base):
 __tablename__='users';id:Mapped[int]=mapped_column(primary_key=True);username:Mapped[str]=mapped_column(String(100),unique=True,index=True);password_hash:Mapped[str]=mapped_column(String(300));role:Mapped[str]=mapped_column(String(20),default='reader')
class Doc(Base):
 __tablename__='documents';id:Mapped[str]=mapped_column(String(40),primary_key=True);title:Mapped[str]=mapped_column(String(500));filename:Mapped[str]=mapped_column(String(255));checksum:Mapped[str]=mapped_column(String(64));version:Mapped[int]=mapped_column(Integer,default=1);status:Mapped[str]=mapped_column(String(30),default='processing');owner:Mapped[str]=mapped_column(String(100));acl:Mapped[str]=mapped_column(Text,default='*');created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc));snapshot_path:Mapped[str]=mapped_column(Text)
class Chunk(Base):
 __tablename__='chunks';id:Mapped[str]=mapped_column(String(64),primary_key=True);doc_id:Mapped[str]=mapped_column(ForeignKey('documents.id',ondelete='CASCADE'),index=True);page:Mapped[int]=mapped_column(Integer);text:Mapped[str]=mapped_column(Text);token_estimate:Mapped[int]=mapped_column(Integer)
class Setting(Base):
 __tablename__='settings';key:Mapped[str]=mapped_column(String(100),primary_key=True);encrypted_value:Mapped[str]=mapped_column(Text)
