"""Encrypted application provider configuration."""
import os,hashlib,base64
from cryptography.fernet import Fernet
from database import Setting

def key_fernet():return Fernet(base64.urlsafe_b64encode(hashlib.sha256(os.getenv('APP_SECRET_KEY','local-dev-secret-change-me').encode()).digest()))
def read_setting(db,key,env):
 row=db.get(Setting,key)
 return key_fernet().decrypt(row.encrypted_value.encode()).decode() if row else os.getenv(env,'')
