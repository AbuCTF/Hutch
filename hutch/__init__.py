def _find_version():
    # a checkout reports its own pyproject.toml: the metadata an editable install wrote at install time goes stale
    import os
    import re
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")) as f:
            found = re.search(r'^version = "([^"]+)"', f.read(), re.M)
        if found:
            return found.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version
        return version("hutch")
    except Exception:
        return "0.0.0"


__version__ = _find_version()

from .artifacts import ArtifactStore  # noqa: F401
from .caido import CaidoClient, CaidoConfig, CaidoError  # noqa: F401
from .client import HutchClient, HutchError, SessionHandle, connect  # noqa: F401
from .context import (  # noqa: F401
    ConsoleEntry,
    Context,
    Diff,
    ErrorEntry,
    NavigationEntry,
    NetworkEntry,
    Snapshot,
)
from .fingerprint import Fingerprint, generate, generate_for_program  # noqa: F401
from .health import Alert, HealthMonitor  # noqa: F401
from .differ import ResponseDiff, diff_responses  # noqa: F401
from .pool import Pool  # noqa: F401
from .session import ProxyConfig, Session, SessionState  # noqa: F401
