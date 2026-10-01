#!/usr/bin/env python3
"""Health check module - verifies connections to all required services."""
import os
import sys
import time
from typing import Dict, Tuple

# Load .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg
import redis
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer


def check_postgres() -> Tuple[bool, str]:
    """Check PostgreSQL connection."""
    try:
        database_url = os.getenv('DATABASE_URL', 'postgresql+psycopg://ragsearch:change-me@localhost:5432/ragsearch')
        # Convert SQLAlchemy URL to psycopg format
        if database_url.startswith('postgresql+psycopg://'):
            database_url = database_url.replace('postgresql+psycopg://', 'postgresql://')
        
        conn = psycopg.connect(database_url, connect_timeout=5)
        conn.execute("SELECT 1")
        conn.close()
        return True, "PostgreSQL: Connected"
    except Exception as e:
        return False, f"PostgreSQL: Failed - {e}"


def check_redis() -> Tuple[bool, str]:
    """Check Redis connection."""
    try:
        redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
        client = redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
        client.ping()
        client.close()
        return True, "Redis: Connected"
    except Exception as e:
        return False, f"Redis: Failed - {e}"


def check_qdrant() -> Tuple[bool, str]:
    """Check Qdrant connection."""
    try:
        qdrant_url = os.getenv('QDRANT_URL', 'http://localhost:6333')
        client = QdrantClient(url=qdrant_url, timeout=5)
        collections = client.get_collections()
        client.close()
        return True, f"Qdrant: Connected ({len(collections.collections)} collections)"
    except Exception as e:
        return False, f"Qdrant: Failed - {e}"


def check_embedding_model() -> Tuple[bool, str]:
    """Check embedding model loads correctly."""
    try:
        model_name = os.getenv('EMBEDDING_MODEL', 'sentence-transformers/all-MiniLM-L6-v2')
        dimension = int(os.getenv('EMBEDDING_DIMENSION', '384'))
        model = SentenceTransformer(model_name)
        actual_dim = model.get_sentence_embedding_dimension()
        if actual_dim != dimension:
            return False, f"Embedding Model: Dimension mismatch - expected {dimension}, got {actual_dim}"
        return True, f"Embedding Model: Loaded ({model_name}, {actual_dim} dim)"
    except Exception as e:
        return False, f"Embedding Model: Failed - {e}"


def check_app_secret() -> Tuple[bool, str]:
    """Validate APP_SECRET_KEY."""
    try:
        from security import validate_app_secret
        secret = os.getenv('APP_SECRET_KEY', '')
        validate_app_secret(secret)
        return True, "APP_SECRET_KEY: Valid"
    except Exception as e:
        return False, f"APP_SECRET_KEY: Invalid - {e}"


def check_llm_config() -> Tuple[bool, str]:
    """Check LLM configuration (optional)."""
    base_url = os.getenv('LLM_BASE_URL', '')
    api_key = os.getenv('LLM_API_KEY', '')
    model = os.getenv('LLM_MODEL', '')
    
    if not base_url or not model:
        return True, "LLM: Not configured (optional)"
    
    if not api_key:
        return True, f"LLM: Configured ({model}) - no API key set"
    
    return True, f"LLM: Configured ({model})"


def check_jev_config() -> Tuple[bool, str]:
    """Check Jev configuration (optional)."""
    api_key = os.getenv('TYPESAFE_API_KEY', '')
    rerank_enabled = os.getenv('JEV_RERANK_ENABLED', 'false').lower() == 'true'
    chunk_profile_enabled = os.getenv('JEV_CHUNK_PROFILE_ENABLED', 'false').lower() == 'true'
    
    if not api_key:
        return True, "Jev: Not configured (optional)"
    
    features = []
    if rerank_enabled:
        features.append("rerank")
    if chunk_profile_enabled:
        features.append("chunk_profile")
    
    return True, f"Jev: Configured ({', '.join(features) if features else 'no features enabled'})"


def run_all_checks() -> Dict[str, Tuple[bool, str]]:
    """Run all health checks."""
    checks = {
        "APP_SECRET_KEY": check_app_secret(),
        "PostgreSQL": check_postgres(),
        "Redis": check_redis(),
        "Qdrant": check_qdrant(),
        "Embedding Model": check_embedding_model(),
        "LLM Config": check_llm_config(),
        "Jev Config": check_jev_config(),
    }
    return checks


def main():
    """Main entry point."""
    print("=" * 60)
    print("RAGSearch Jev - Service Health Checks")
    print("=" * 60)
    
    checks = run_all_checks()
    
    all_passed = True
    for name, (passed, message) in checks.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}  {message}")
        if not passed:
            all_passed = False
    
    print("=" * 60)
    if all_passed:
        print("✓ All required services are healthy!")
        sys.exit(0)
    else:
        print("✗ Some required services failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()