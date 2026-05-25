"""Inspector plugin system package.

The concrete types (``InspectorManifest`` / ``InspectorRegistry`` /
``InspectorRunner`` / ``InspectorResult`` / ``InspectorError``) live in
submodules and are imported by callers explicitly. This package's
``__init__`` MUST NOT trigger any side-effecting work at import time —
specifically, ``build_registry_from_search_paths`` must never run from
here, so importing ``hostlens.inspectors`` stays cheap and predictable.
"""

from __future__ import annotations
