
import keyring

from maajun.providers.base import ProviderType

SERVICE_NAME = "maajun"

class AuthManager:

    SUPPORTED_PROVIDERS: dict[str, str] = {
        p.value: f"{p.value}_api_key" for p in ProviderType
    }

    def __init__(self):
        self._cache = {}

    def get_api_key(self, provider: str) -> str | None:
        """Get API key for a provider"""
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            return None

        # Check cache first
        if provider in self._cache:
            return self._cache[provider]

        # Then keyring
        key = keyring.get_password(SERVICE_NAME, key_name)
        if key:
            self._cache[provider] = key
        return key

    def set_api_key(self, provider: str, key: str) -> None:
        """Set API key for a provider"""
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            raise ValueError(f"Unsupported provider: {provider}")

        key = key.strip()
        keyring.set_password(SERVICE_NAME, key_name, key)
        self._cache[provider] = key

    def has_api_key(self, provider: str) -> bool:
        """Check if provider has an API key set"""
        return self.get_api_key(provider) is not None

    def get_all_providers_with_keys(self) -> list[str]:
        """Get all providers that have API keys set"""
        return [
            provider for provider in self.SUPPORTED_PROVIDERS
            if self.has_api_key(provider)
        ]

    def clear_provider_key(self, provider: str) -> None:
        """Clear API key for a provider"""
        key_name = self.SUPPORTED_PROVIDERS.get(provider)
        if not key_name:
            return

        try:
            keyring.delete_password(SERVICE_NAME, key_name)
            if provider in self._cache:
                del self._cache[provider]
        except keyring.errors.PasswordDeleteError:
            pass

    def clear_all(self) -> None:
        """Clear all stored credentials"""
        for provider in self.SUPPORTED_PROVIDERS:
            self.clear_provider_key(provider)
        self._cache = {}
