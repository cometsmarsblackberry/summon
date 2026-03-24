"""Abstract cloud provider interface for multi-provider support."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class CloudInstanceData:
    """Provider-agnostic instance data returned by cloud APIs."""
    id: str
    region: str
    plan: str
    main_ip: str
    status: str
    power_status: str
    date_created: str


class CloudProviderError(Exception):
    """Cloud provider API error."""
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class CloudProvider(ABC):
    """Abstract base class for cloud providers."""

    @abstractmethod
    async def create_instance(
        self,
        region: str,
        label: str,
        user_data: str,
        hostname: Optional[str] = None,
        plan: Optional[str] = None,
    ) -> CloudInstanceData:
        """Create a new cloud instance."""
        ...

    @abstractmethod
    async def get_instance(self, instance_id: str) -> CloudInstanceData:
        """Get instance details by ID."""
        ...

    @abstractmethod
    async def destroy_instance(self, instance_id: str, region: Optional[str] = None) -> None:
        """Destroy/delete an instance.

        Args:
            instance_id: Provider instance UUID
            region: Optional provider region hint (avoids scanning in multi-region providers)
        """
        ...

    @abstractmethod
    async def list_instances(self, label_prefix: Optional[str] = None) -> list[CloudInstanceData]:
        """List instances, optionally filtered by label prefix."""
        ...


def get_cloud_client(provider_code: str = "vultr") -> Optional[CloudProvider]:
    """Get a cloud provider client by provider code.

    Args:
        provider_code: The provider identifier (e.g., 'vultr')

    Returns:
        CloudProvider instance if configured, None otherwise
    """
    if provider_code == "vultr":
        from app.services.vultr import get_vultr_client
        return get_vultr_client()
    elif provider_code == "gcore":
        from app.services.gcore import get_gcore_client
        return get_gcore_client()
    elif provider_code == "onidel":
        from app.services.onidel import get_onidel_client
        return get_onidel_client()
    return None


def any_cloud_configured() -> bool:
    """Check if any cloud provider is configured."""
    from app.config import get_settings
    settings = get_settings()
    return settings.vultr_configured or settings.gcore_configured or settings.onidel_configured
