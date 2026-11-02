"""Each source as one target receives it.

`resolve` applies a source's `only`, validates its `targets.<target>` block
against the target's native model for that kind, and checks its omits against
`limits`, so a lowering sees only the sources it emits, with typed native
fields.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .limits import required_omissions
from .output.plan import Diagnostic
from .sources import PermissionSource, StrictModel, TargetBlock, ToolName


class Bare(StrictModel):
    """The native model of a kind that takes no target fields."""


@dataclass(frozen=True, slots=True)
class Resolved[S, N: BaseModel]:
    source: S
    native: N
    block: TargetBlock

    @property
    def raw(self) -> Mapping[str, Any]:
        return self.block.raw


def applies_to(source: Any, target: str) -> bool:
    """Whether this portable artifact should emit (and owe omits) on `target`."""
    only = getattr(source, "only", ())
    return not only or target in only


def block(source: Any, target: str) -> TargetBlock:
    return source.targets.root.get(target, TargetBlock())


def source_path(source: Any) -> Path:
    """The file a diagnostic about `source` points at."""
    return source.paths[0] if isinstance(source, PermissionSource) else source.path


def omissions(
    source: Path, target: str, value: TargetBlock, required: Mapping[str, str]
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    for path in sorted(set(required) - set(value.omit)):
        diagnostics.append(
            Diagnostic(
                "error",
                f"{path} is unsupported on {target}: {required[path]} List it in "
                f"targets.{target}.omit",
                source,
            )
        )
    # An acknowledged omission is silent.
    for path in value.omit:
        if path not in required:
            diagnostics.append(
                Diagnostic(
                    "error",
                    f"{path} is not an omitted {target} capability",
                    source,
                )
            )
    return tuple(diagnostics)


def unhandled_target_block(
    source: Path, target: str, value: TargetBlock
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    if value.native:
        diagnostics.append(
            Diagnostic("error", f"{target} native fields are not valid here", source)
        )
    if value.raw:
        diagnostics.append(
            Diagnostic("error", f"{target} raw fields are not valid here", source)
        )
    return tuple(diagnostics)


def strict_native[T: BaseModel](
    source: Path, target: str, value: TargetBlock, model: type[T]
) -> tuple[T | None, tuple[Diagnostic, ...]]:
    try:
        return model.model_validate(value.native), ()
    except ValidationError as exc:
        return None, (
            Diagnostic("error", f"invalid {target} native fields: {exc}", source),
        )


def resolve[S, N: BaseModel](
    target: ToolName,
    sources: Iterable[S],
    native: type[N] = Bare,
    *,
    raw: bool = True,
) -> tuple[list[Resolved[S, N]], list[Diagnostic]]:
    """The sources `target` receives, each with its validated native fields.

    A source whose native fields fail validation is reported and left out.
    `raw: False` rejects raw fields for a kind whose output has no place for
    them; a `Bare` kind rejects both.
    """
    resolved: list[Resolved[S, N]] = []
    diagnostics: list[Diagnostic] = []
    bare = native is Bare
    for source in sources:
        if not applies_to(source, target):
            continue
        value = block(source, target)
        path = source_path(source)
        if bare:
            diagnostics.extend(unhandled_target_block(path, target, value))
        elif value.raw and not raw:
            diagnostics.append(
                Diagnostic("error", f"{target} raw fields are not valid here", path)
            )
        # A Bare kind already reported its fields, so it validates an empty block.
        checked = TargetBlock() if bare else value
        model, issues = strict_native(path, target, checked, native)
        diagnostics.extend(issues)
        diagnostics.extend(
            omissions(path, target, value, required_omissions(source, target))
        )
        if model is not None:
            resolved.append(Resolved(source, model, value))
    return resolved, diagnostics
