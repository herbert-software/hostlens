"""Inspector plugin system package.

M1 (add-inspector-plugin-system): empty placeholder during Group 1 scaffold.

Group 1 only creates skeleton files; the concrete types
(``InspectorManifest`` / ``InspectorRegistry`` / ``InspectorRunner`` /
``InspectorResult`` / ``InspectorError``) are added by subsequent groups.
This module must NOT trigger any side-effecting work at import time —
specifically, ``build_registry_from_search_paths`` must never run from
here.
"""

from __future__ import annotations
