import logging
import sys
from rich.console import Console
from rich.logging import RichHandler

SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

def _success(self, message, *args, **kwargs):
    if self.isEnabledFor(SUCCESS):
        self._log(SUCCESS, message, args, **kwargs)

logging.Logger.success = _success

console = Console()
log = logging.getLogger("Kalm")
log.setLevel(logging.DEBUG)

handler = RichHandler(console=console, rich_tracebacks=True, markup=True)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(message)s", datefmt="[%X]")
handler.setFormatter(formatter)

if log.hasHandlers():
    log.handlers.clear()
log.addHandler(handler)
log.propagate = False
