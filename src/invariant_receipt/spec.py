"""Loading and validating ``invariants.yaml`` (spec §2).

Validation is strict on purpose. An unknown key is refused rather than ignored:
a misspelt ``matchs:`` that was silently dropped would turn a pinned failure
oracle into "any failure will do", which is exactly the kind of quiet weakening
this tool exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

RUNNERS = ("auto", "pytest", "nextest", "cargo")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
RUST_TARGET_RE = re.compile(r"^(lib|(test|bin):[A-Za-z0-9_-]+)$")

TOP_KEYS = {"invariants", "project", "runner"}
INVARIANT_KEYS = {"invariant", "description", "project", "runner", "test", "target", "mutations"}
MUTATION_KEYS = {"id", "description", "file", "find", "replace", "patch", "expect"}
EXPECT_KEYS = {"outcome", "matches"}


class SpecError(ValueError):
    """The invariants file cannot be used as written."""


@dataclass(frozen=True)
class Expect:
    outcome: str
    matches: str | None


@dataclass(frozen=True)
class Mutation:
    id: str
    description: str | None
    expect: Expect
    file: str | None = None
    find: str | None = None
    replace: str | None = None
    patch: str | None = None  # relative to the YAML file's directory
    patch_path: Path | None = None  # resolved absolute path of the patch

    @property
    def kind(self) -> str:
        return "patch" if self.patch is not None else "find_replace"


@dataclass(frozen=True)
class Invariant:
    name: str
    description: str | None
    project: str  # directory relative to the YAML file, as written
    project_dir: Path  # resolved absolute directory
    runner: str
    test: str
    target: str | None
    mutations: list[Mutation] = field(default_factory=list)


def _require_str(value, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{where} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise SpecError(f"{where} must not be empty")
    return value


def _check_keys(mapping: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise SpecError(
            f"{where}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}"
        )


def _relative_inside(path_text: str, where: str) -> str:
    p = PurePosixPath(path_text)
    if p.is_absolute() or ".." in p.parts or path_text.startswith("~"):
        raise SpecError(
            f"{where}: {path_text!r} must be a relative path that stays inside the project"
        )
    return path_text


def _load_expect(raw, where: str) -> Expect:
    if not isinstance(raw, dict):
        raise SpecError(f"{where}.expect must be a mapping with 'outcome' (and optional 'matches')")
    _check_keys(raw, EXPECT_KEYS, f"{where}.expect")
    outcome = raw.get("outcome")
    if outcome != "fail":
        raise SpecError(
            f"{where}.expect.outcome must be 'fail' (the named test must be seen failing)"
        )
    matches = raw.get("matches")
    if matches is not None:
        _require_str(matches, f"{where}.expect.matches")
        try:
            re.compile(matches)
        except re.error as exc:
            raise SpecError(
                f"{where}.expect.matches is not a valid regular expression: {exc}"
            ) from exc
    return Expect(outcome=outcome, matches=matches)


def _load_mutation(raw, where: str, yaml_dir: Path) -> Mutation:
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be a mapping")
    _check_keys(raw, MUTATION_KEYS, where)
    mid = _require_str(raw.get("id"), f"{where}.id")
    if not ID_RE.match(mid):
        raise SpecError(f"{where}.id {mid!r} must match {ID_RE.pattern}")
    where = f"mutation {mid!r}"
    if "expect" not in raw:
        raise SpecError(f"{where}: 'expect' is required — it is the failure oracle")
    expect = _load_expect(raw["expect"], where)
    description = raw.get("description")
    if description is not None:
        _require_str(description, f"{where}.description")
    has_fr = any(k in raw for k in ("file", "find", "replace"))
    has_patch = "patch" in raw
    if has_fr == has_patch:
        raise SpecError(f"{where}: give either file+find+replace, or patch — exactly one form")
    if has_patch:
        patch = _relative_inside(_require_str(raw["patch"], f"{where}.patch"), f"{where}.patch")
        patch_path = (yaml_dir / patch).resolve()
        if not patch_path.is_file():
            raise SpecError(f"{where}.patch: {patch_path} does not exist")
        return Mutation(
            id=mid, description=description, expect=expect, patch=patch, patch_path=patch_path
        )
    for key in ("file", "find", "replace"):
        if key not in raw:
            raise SpecError(f"{where}: find/replace mutations need 'file', 'find' and 'replace'")
    file = _relative_inside(_require_str(raw["file"], f"{where}.file"), f"{where}.file")
    find = _require_str(raw["find"], f"{where}.find")
    replace = _require_str(raw["replace"], f"{where}.replace", allow_empty=True)
    return Mutation(
        id=mid, description=description, expect=expect, file=file, find=find, replace=replace
    )


def _load_invariant(raw, defaults: dict, yaml_dir: Path) -> Invariant:
    if not isinstance(raw, dict):
        raise SpecError("each invariant must be a mapping")
    _check_keys(raw, INVARIANT_KEYS, "invariant")
    name = _require_str(raw.get("invariant"), "invariant")
    if not ID_RE.match(name):
        raise SpecError(f"invariant name {name!r} must match {ID_RE.pattern}")
    where = f"invariant {name!r}"
    description = raw.get("description")
    if description is not None:
        _require_str(description, f"{where}.description")
    project = raw.get("project", defaults.get("project", "."))
    project = _relative_inside(_require_str(project, f"{where}.project"), f"{where}.project")
    project_dir = (yaml_dir / project).resolve()
    if not project_dir.is_dir():
        raise SpecError(f"{where}.project: {project_dir} is not a directory")
    runner = raw.get("runner", defaults.get("runner", "auto"))
    if runner not in RUNNERS:
        raise SpecError(f"{where}.runner must be one of {', '.join(RUNNERS)}")
    test = _require_str(raw.get("test"), f"{where}.test")
    target = raw.get("target")
    if target is not None:
        _require_str(target, f"{where}.target")
        if not RUST_TARGET_RE.match(target):
            raise SpecError(f"{where}.target must be 'lib', 'test:NAME' or 'bin:NAME'")
    muts_raw = raw.get("mutations")
    if not isinstance(muts_raw, list) or not muts_raw:
        raise SpecError(
            f"{where}: 'mutations' must be a non-empty list — mutations are always explicit"
        )
    mutations = [
        _load_mutation(m, f"{where} mutation #{i + 1}", yaml_dir) for i, m in enumerate(muts_raw)
    ]
    ids = [m.id for m in mutations]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SpecError(f"{where}: duplicate mutation id(s) {', '.join(dupes)}")
    return Invariant(
        name=name,
        description=description,
        project=project,
        project_dir=project_dir,
        runner=runner,
        test=test,
        target=target,
        mutations=mutations,
    )


def load(path: Path) -> list[Invariant]:
    path = Path(path).resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path}: top level must be a mapping")
    yaml_dir = path.parent
    if "invariants" in data:
        _check_keys(data, TOP_KEYS, "top level")
        items = data["invariants"]
        if not isinstance(items, list) or not items:
            raise SpecError("'invariants' must be a non-empty list")
        defaults = {k: data[k] for k in ("project", "runner") if k in data}
        invariants = [_load_invariant(item, defaults, yaml_dir) for item in items]
    else:
        invariants = [_load_invariant(data, {}, yaml_dir)]
    names = [i.name for i in invariants]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SpecError(f"duplicate invariant name(s): {', '.join(dupes)}")
    return invariants
