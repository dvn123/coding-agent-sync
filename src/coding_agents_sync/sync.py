from __future__ import annotations

from pathlib import Path

from .artifacts import CodexConfigArtifact, NativeConfigArtifact, OpenCodeConfigArtifact
from .models import SyncContext
from .runtime_config import (
    GeneratedRuntimeConfig,
    apply_native_plan,
    prepare_native_plan,
)
from .sources import load_sources
from .translators import (
    ClaudeTranslator,
    CodexTranslator,
    CursorTranslator,
    OpenCodeTranslator,
)
from .writer import (
    artifact_drift,
    preflight_artifacts,
    publish_manifests,
    write_artifacts,
)


def run_sync(*, config_root: Path, home: Path, check: bool = False) -> tuple[Path, ...]:
    ctx = SyncContext(config_root=config_root, home=home)
    sources = load_sources(ctx.config_root)
    artifacts = [
        *ClaudeTranslator(ctx).translate(sources),
        *CursorTranslator(ctx).translate(sources),
        *OpenCodeTranslator(ctx).translate(sources),
        *CodexTranslator(ctx).translate(sources),
    ]
    opencode = next(
        item for item in artifacts if isinstance(item, OpenCodeConfigArtifact)
    )
    codex = next(item for item in artifacts if isinstance(item, CodexConfigArtifact))
    native = tuple(item for item in artifacts if isinstance(item, NativeConfigArtifact))
    portable = [
        item
        for item in artifacts
        if not isinstance(
            item,
            (OpenCodeConfigArtifact, CodexConfigArtifact, NativeConfigArtifact),
        )
    ]
    generated = GeneratedRuntimeConfig(
        tuple(opencode.instructions), codex.skill_paths, codex.agents, native
    )
    native_plan = prepare_native_plan(
        config_root=config_root, home=home, generated=generated
    )
    preflight_artifacts(portable)
    if check:
        return (*artifact_drift(portable), *native_plan.drift)
    write_artifacts(portable)
    apply_native_plan(native_plan)
    publish_manifests(portable)
    return ()
