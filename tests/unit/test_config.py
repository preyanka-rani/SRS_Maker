import pytest

from app.core.config import _DEV_SECRET, Settings


def test_production_rejects_default_secret():
    with pytest.raises(RuntimeError):
        Settings(app_env="production", app_secret_key=_DEV_SECRET, _env_file=None).validate_for_startup()


def test_production_rejects_short_secret():
    with pytest.raises(RuntimeError):
        Settings(app_env="production", app_secret_key="short", _env_file=None).validate_for_startup()


def test_production_accepts_long_secret():
    Settings(app_env="production", app_secret_key="x" * 40, _env_file=None).validate_for_startup()


def test_development_allows_default_secret():
    Settings(app_env="development", app_secret_key=_DEV_SECRET, _env_file=None).validate_for_startup()
