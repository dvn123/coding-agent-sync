from __future__ import annotations

import pytest

from coding_agents_sync.probes.cursor_desktop import (
    ProbeStatus,
    ProbeUnavailable,
    cleanup_probe,
    prepare_probe,
    verify_probe,
)


@pytest.mark.capability_case("cursor-desktop.rules")
@pytest.mark.capability_desktop
@pytest.mark.capability_live
def test_cursor_desktop_loads_an_owned_ancestor_rule() -> None:
    state = None
    result = None
    try:
        state = prepare_probe()
        result = verify_probe(state)
    except ProbeUnavailable as error:
        pytest.skip(f"unavailable: {error}")
    finally:
        if state is not None:
            cleanup_probe(state.run_id)
    assert result is not None
    assert result.status is ProbeStatus.LOADED, result.detail
