from __future__ import annotations

import pytest

from positioning_agent_tools.engine import EngineTools, Paths


@pytest.fixture(scope="session")
def paths(tmp_path_factory) -> Paths:
    p = Paths.from_env()
    p.workdir = tmp_path_factory.mktemp("ipe-work")
    if not (p.build / "tools" / "ipe-replay").exists():
        pytest.skip(f"engine not built at {p.build}; set IPE_ROOT/IPE_BUILD", allow_module_level=True)
    return p


@pytest.fixture(scope="session")
def tools(paths) -> EngineTools:
    return EngineTools(paths)
