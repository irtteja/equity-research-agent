"""
nselib — NSE India data library with Claude-powered intelligence.
"""

from .nse        import NSE
from .brain      import Brain
from .guardian   import Guardian
from .core       import NSESession
from .exceptions import (
    NSESessionError,
    NSEFileUnavailableError,
    NSEDataError,
    BrainNotConfiguredError,
)
from .constants  import INDEX, SEGMENT, SERIES, INSTRUMENT, OPTION_TYPE, DEAL_TYPE, DEPT

__version__ = "0.1.0"

__all__ = [
    "NSE",
    "Brain",
    "NSESession",
    "NSESessionError",
    "NSEFileUnavailableError",
    "NSEDataError",
    "BrainNotConfiguredError",
    "INDEX",
    "SEGMENT",
    "SERIES",
    "INSTRUMENT",
    "OPTION_TYPE",
    "DEAL_TYPE",
    "DEPT",
]
