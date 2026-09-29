"""Configuration checks before authentication is enabled."""


def validate_app_secret(secret: str) -> None:
    placeholders = {
        '',
        'local-dev-secret-change-me',
        'replace-with-a-long-random-secret',
    }
    if secret in placeholders or len(secret.encode('utf-8')) < 32:
        raise RuntimeError('APP_SECRET_KEY must be a unique random secret of at least 32 bytes')
