"""Tool domains. Importing this package registers every tool in the registry.

Each domain module self-registers at import time (its ``_register()`` runs).
Adding a domain = create ``prufa_mcp/tools/<domain>.py`` and add one import
line here. Order is cosmetic (it sets the tools/list order).
"""

from __future__ import annotations

from prufa_mcp.tools import audit  # noqa: F401
from prufa_mcp.tools import workspace  # noqa: F401
from prufa_mcp.tools import billing  # noqa: F401
from prufa_mcp.tools import flows  # noqa: F401
from prufa_mcp.tools import monitors  # noqa: F401
from prufa_mcp.tools import gremlin  # noqa: F401
from prufa_mcp.tools import discovery  # noqa: F401
