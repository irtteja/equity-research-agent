class NSESessionError(Exception):
    """Raised when the NSE session cookie cannot be obtained or refreshed."""


class NSEFileUnavailableError(Exception):
    """Raised on HTTP 404 — weekend, holiday, or date outside archive window."""
    def __init__(self, url: str):
        super().__init__(f"NSE file not available: {url}")
        self.url = url


class NSEDataError(Exception):
    """Raised when NSE returns an unexpected payload (empty, malformed, etc.)."""


class BrainNotConfiguredError(Exception):
    """Raised when ask() / analyze() are called without a Claude API key."""
    def __init__(self):
        super().__init__(
            "claude_api_key is required to use ask() / analyze() / market_summary(). "
            "Pass it to NSE(download_folder, claude_api_key='sk-ant-...')"
        )
