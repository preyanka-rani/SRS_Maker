"""Application settings. Secrets are SecretStr and come only from the environment."""
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-only-insecure-secret-change-me-0123456789"
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # core
    app_env: str = "development"
    app_secret_key: SecretStr = Field(default=SecretStr(_DEV_SECRET), min_length=1)
    jwt_ttl_minutes: int = 30
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://srs:srs@localhost:5432/srs"
    redis_url: str = "redis://localhost:6379/0"
    storage_root: str = "./storage"

    # celery (queues: default / llm / asr) -- eager mode lets everything run locally without Redis
    celery_task_always_eager: bool = False
    celery_llm_task_rate_limit: str = "10/m"
    celery_asr_task_rate_limit: str = "10/m"
    llm_worker_concurrency: int = 2
    asr_worker_concurrency: int = 2

    # limits
    max_upload_mb: int = 25
    max_attachment_mb: int = 25
    max_email_total_mb: int = 40
    max_attachments_per_email: int = 20

    # gemini
    gemini_api_key: SecretStr | None = None
    gemini_model_reasoning: str = ""
    gemini_model_fast: str = ""
    gemini_timeout_seconds: float = 120
    gemini_max_retries: int = 4
    gemini_max_rpm: int = 30
    llm_project_token_budget: int = 2_000_000
    feedback_min_confidence: float = 0.75

    # groq / audio
    groq_api_key: SecretStr | None = None
    groq_transcription_model: str = "whisper-large-v3"
    groq_max_file_mb: float = 24
    groq_timeout_seconds: float = 300
    groq_max_rpm: int = 20
    asr_project_audio_minutes_budget: float = 600
    max_audio_minutes: float = 90
    audio_chunk_max_seconds: float = 600
    audio_silence_noise_db: float = -35
    audio_silence_min_seconds: float = 0.4
    audio_silence_search_window_seconds: float = 120
    audio_forced_cut_overlap_seconds: float = 2
    asr_no_speech_threshold: float = 0.6
    asr_min_avg_logprob: float = -1.0
    asr_low_confidence_ratio: float = 0.3
    transcription_language_hint: str | None = None

    # template / srs
    template_path: str = "templates/srs_base_template.docx"
    template_manifest_path: str = "templates/srs_base_template.manifest.yaml"
    template_version: int = 1
    leakage_denylist_path: str = "templates/leakage_denylist.txt"
    srs_prepared_by: str = "Business Analyst"
    gotenberg_url: str | None = None

    # email
    imap_host: str | None = Field(default=None, validation_alias=AliasChoices("IMAP_SERVER", "IMAP_HOST"))
    imap_port: int = 993
    imap_username: str | None = Field(default=None, validation_alias=AliasChoices("IMAP_EMAIL", "IMAP_USERNAME"))
    imap_password: SecretStr | None = None
    imap_folder: str = "INBOX"
    imap_poll_interval_seconds: int = 60
    smtp_host: str | None = Field(default=None, validation_alias=AliasChoices("SMTP_SERVER", "SMTP_HOST"))
    smtp_port: int = 587
    smtp_username: str | None = Field(default=None, validation_alias=AliasChoices("SMTP_EMAIL", "SMTP_USERNAME"))
    smtp_password: SecretStr | None = None
    smtp_security: str = "starttls"
    smtp_from_address: str | None = None  # defaults to the SMTP login address
    smtp_from_name: str = "SRS Automation"
    email_message_id_domain: str = "srs.example.com"
    email_max_attachment_mb: int = 20
    outbox_stale_sending_minutes: int = 15
    outbox_max_attempts: int = 6
    trusted_authserv_ids: str = "mx.google.com"  # Gmail stamps Authentication-Results with this id
    require_sender_auth_for_approval: bool = True
    reply_requires_staff_review: bool = True
    pending_input_nudge_minutes: int = 10
    workflow_lock_retry_seconds: int = 15

    # mailbox intake: an email from an unknown sender opens a project and gets a reply
    auto_create_project_from_email: bool = True
    auto_consent_policy_reference: str | None = "mailbox-intake-default-policy"
    intake_keywords: str = ("srs,requirement,requirements,specification,software,project,system,"
                            "portal,application,app,website,platform,module,feature,development")
    imap_mark_seen: bool = True  # mark a mail as read once the system has taken it over
    imap_unseen_only: bool = True  # only unread mail is considered
    local_scheduler: bool = True  # in-process poller (no Redis/beat needed for local runs)
    webhook_shared_secret: SecretStr | None = None  # enables POST /webhooks/email/inbound

    @property
    def intake_keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in self.intake_keywords.split(",") if k.strip()]

    @property
    def mail_from_address(self) -> str | None:
        return self.smtp_from_address or self.smtp_username

    @property
    def mail_configured(self) -> bool:
        return bool(self.imap_host and self.imap_username and self.imap_password
                    and self.smtp_host and self.smtp_username and self.smtp_password)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_root)

    @property
    def trusted_authserv_set(self) -> frozenset[str]:
        return frozenset(v.strip().lower() for v in self.trusted_authserv_ids.split(",") if v.strip())

    def validate_for_startup(self) -> None:
        """Fail fast when production runs with a weak or placeholder secret."""
        secret = self.app_secret_key.get_secret_value()
        if self.is_production and (secret == _DEV_SECRET or len(secret) < MIN_SECRET_LENGTH):
            raise RuntimeError("APP_SECRET_KEY must be set (>= 32 chars) in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
