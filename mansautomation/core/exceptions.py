"""Domain exceptions."""

from __future__ import annotations


class MansAutomationError(Exception):
    """Base class for all application errors."""


class ConfigurationError(MansAutomationError):
    """Raised when configuration is invalid or missing."""


class ProfileError(MansAutomationError):
    """Raised for profile / dataset issues."""


class ProfileNotFoundError(ProfileError):
    """Raised when a profile cannot be located."""


class CryptoError(MansAutomationError):
    """Raised for encryption / decryption failures."""


class PluginError(MansAutomationError):
    """Raised by the plugin manager."""


class PluginLoadError(PluginError):
    """Raised when a plugin cannot be loaded."""


class AutomationError(MansAutomationError):
    """Raised by the automation runner."""


class WorkflowAbortedError(AutomationError):
    """Raised when a workflow is intentionally aborted."""


class HumanInterventionRequired(AutomationError):
    """Raised when CAPTCHA / waiting room / manual interaction is required."""

    def __init__(self, reason: str, *, url: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.url = url


class FormDetectionError(AutomationError):
    """Raised when the form detection engine fails."""


class BrowserError(AutomationError):
    """Raised for browser lifecycle issues."""
