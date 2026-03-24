"""Application configuration using pydantic-settings."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    # Database - use absolute path for Docker volume mount
    database_url: str = "sqlite+aiosqlite:////data/reserve.db"
    
    # Security
    secret_key: str = "change-me-to-a-random-secret-key"
    
    # Cloud Providers
    vultr_api_key: str = ""
    gcore_api_key: str = ""
    gcore_project_id: int = 0
    onidel_api_key: str = ""
    onidel_team_id: str = ""
    
    # Steam
    steam_api_key: str = ""
    
    # hCaptcha (optional for Phase 1)
    hcaptcha_site_key: str = ""
    hcaptcha_secret_key: str = ""
    
    # Base URL for callbacks
    base_url: str = "http://localhost:8000"

    # API docs exposure. Defaults to enabled on localhost and disabled elsewhere.
    expose_api_docs: bool | None = None
    
    # Admin Steam IDs (comma-separated)
    admin_steam_ids: str = ""
    
    # Beta mode - only admins can use the site
    beta_mode: bool = True

    # Reverse proxy handling
    trusted_proxy_cidrs: str = "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    
    # Branding
    site_name: str = "Summon"
    logo_url: str = ""
    favicon_url: str = ""
    og_image_url: str = ""
    discord_url: str = ""
    steam_group_url: str = ""
    contact_email: str = ""
    rules_url: str = ""
    login_image_url: str = ""
    login_image_wide_url: str = ""
    custom_config_prefixes: str = ""

    # TF2 Server settings
    fastdl_url: str = "https://fastdl.serveme.tf/"
    tf2_hostname_format: str = "{site_name} #{number} | {location_city}"

    # Agent heartbeat interval (system stats update frequency)
    agent_heartbeat_interval_sec: int = 10
    
    # Reservation defaults
    max_duration_hours: int = 4  # Max reservation duration in hours
    auto_end_minutes: int = 30  # Auto-end server after this many minutes empty

    # Rate limiting
    rate_limit_per_user_hour: int = 3  # Max reservations per user per hour
    rate_limit_admin_per_hour: int = 10  # Max reservations per admin per hour (more lax)
    rate_limit_failed_multiplier: int = 2  # Failed reservations count as N towards limit
    rate_limit_site_provisioning_max: int = 10  # Max concurrent provisioning reservations
    rate_limit_per_user_day: int = 10
    rate_limit_admin_per_day: int = 50
    rate_limit_sitewide_per_hour: int = 30
    rate_limit_sitewide_per_day: int = 150
    daily_hours_limit: int = 12  # Max reservation hours per user per day (0 = disabled)

    # Provisioning retry
    max_provision_attempts: int = 3

    # Circuit breaker - halt provisioning when too many failures occur
    circuit_breaker_window_minutes: int = 15  # Look at failures in this window
    circuit_breaker_threshold: int = 5  # Trip after this many failures in the window
    circuit_breaker_cooldown_minutes: int = 10  # Stay tripped until this long after last failure
    
    # Internal API key for plugin→backend communication
    internal_api_key: str = ""
    allow_legacy_internal_api_key: bool = False
    allow_legacy_agent_query_token: bool = False

    # TF2 API keys (passed through to container for log/demo uploads)
    demos_tf_apikey: str = ""
    logs_tf_apikey: str = ""

    # IPinfo token for geolocation (optional — ping submissions work without it)
    ipinfo_token: str = ""

    # SSH public key for debugging access to cloud instances (optional)
    ssh_pubkey: str = ""

    # S3 storage for server logs (optional — Gcore S3 compatible)
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = ""
    s3_region: str = ""

    # Logging
    log_dir: str = "/data/logs"
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB per log file
    log_backup_count: int = 5  # Keep 5 rotated files
    
    @property
    def admin_steam_id_list(self) -> list[str]:
        """Get list of admin Steam IDs."""
        return [sid.strip() for sid in self.admin_steam_ids.split(",") if sid.strip()]

    @property
    def trusted_proxy_cidr_list(self) -> list[str]:
        """Get list of trusted reverse-proxy CIDRs."""
        return [cidr.strip() for cidr in self.trusted_proxy_cidrs.split(",") if cidr.strip()]
    
    @property
    def hcaptcha_configured(self) -> bool:
        """Check if hCaptcha is configured."""
        return bool(self.hcaptcha_site_key) and bool(self.hcaptcha_secret_key)

    @property
    def vultr_configured(self) -> bool:
        """Check if Vultr API is configured."""
        return bool(self.vultr_api_key)

    @property
    def gcore_configured(self) -> bool:
        """Check if Gcore API is configured."""
        return bool(self.gcore_api_key) and bool(self.gcore_project_id)

    @property
    def onidel_configured(self) -> bool:
        """Check if Onidel API is configured."""
        return bool(self.onidel_api_key) and bool(self.onidel_team_id)

    @property
    def cloud_configured(self) -> bool:
        """Check if any cloud provider is configured."""
        return self.vultr_configured or self.gcore_configured or self.onidel_configured
    
    @property
    def steam_configured(self) -> bool:
        """Check if Steam API is configured."""
        return bool(self.steam_api_key)

    @property
    def api_docs_enabled(self) -> bool:
        """Return whether FastAPI docs should be exposed."""
        if self.expose_api_docs is not None:
            return self.expose_api_docs
        return self.base_url.startswith("http://localhost")


_DEFAULT_SECRET_KEY = "change-me-to-a-random-secret-key"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    s = Settings()
    if s.secret_key == _DEFAULT_SECRET_KEY:
        import warnings
        warnings.warn(
            "SECURITY: Running with the default SECRET_KEY. "
            "Set a random SECRET_KEY in your .env file. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\"",
            stacklevel=2,
        )
        # In production (non-localhost base_url), refuse to start
        if not s.base_url.startswith("http://localhost"):
            raise SystemExit(
                "FATAL: Refusing to start with default SECRET_KEY in production. "
                "Set SECRET_KEY in your .env file."
            )
    return s
