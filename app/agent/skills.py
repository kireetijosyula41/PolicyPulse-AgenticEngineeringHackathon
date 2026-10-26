from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field


class SkillLoadError(ValueError):
    pass


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=300)
    activation_triggers: list[str] = Field(min_length=1, max_length=20)
    counterexamples: list[str] = Field(default_factory=list, max_length=20)
    required_tools: list[str] = Field(min_length=1, max_length=20)


@dataclass(frozen=True)
class LoadedSkill:
    metadata: SkillMetadata
    instructions: str
    path: Path


class SkillLoader:
    """Discover and load only trusted, allowlisted application skill packages."""

    _MAX_METADATA_BYTES = 16_384
    _MAX_INSTRUCTION_BYTES = 64_000
    _CLASSIFIER_TIMEOUT_SECONDS = 0.5
    _UNSAFE_CONTENT = (
        r"ignore\s+(?:all\s+)?(?:prior|previous)\s+instructions",
        r"bypass\s+(?:the\s+)?authorization",
        r"disable\s+(?:the\s+)?authorization",
        r"reveal\s+(?:api\s+keys|secrets|credentials)",
        r"execute\s+arbitrary\s+(?:code|commands)",
        r"change\s+the\s+policy\s+classification",
    )

    def __init__(self, root: Path, available_tools: set[str] | None = None) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.exists() or not self.root.is_dir() or self.root.is_symlink():
            raise SkillLoadError(f"Skill root is not an allowlisted directory: {self.root}")
        if available_tools is None:
            from .registry import TOOL_SPECS

            available_tools = {spec.name for spec in TOOL_SPECS}
        self.available_tools = frozenset(available_tools)

    def discover(self) -> list[SkillMetadata]:
        metadata: list[SkillMetadata] = []
        for candidate in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not candidate.is_dir():
                continue
            if candidate.is_symlink():
                raise SkillLoadError(f"Symlinked skill directory is not permitted: {candidate.name}")
            skill_file = candidate / "SKILL.md"
            if not skill_file.is_file() or skill_file.is_symlink():
                continue
            item = self._read_metadata(skill_file)
            if item.name != candidate.name:
                raise SkillLoadError(f"Skill metadata name does not match directory: {candidate.name}")
            self._validate_required_tools(item)
            metadata.append(item)
        return metadata

    def metadata(self, name: str) -> SkillMetadata:
        for item in self.discover():
            if item.name == name:
                return item
        raise SkillLoadError(f"Skill is not discoverable: {name}")

    def load(self, name: str) -> LoadedSkill:
        item = self.metadata(name)
        path = self._skill_path(name)
        raw = path.read_bytes()
        if len(raw) > self._MAX_INSTRUCTION_BYTES:
            raise SkillLoadError(f"Skill instructions exceed the size limit: {name}")
        text = raw.decode("utf-8")
        self._validate_content(text, item)
        body = self._body(text)
        if not body.strip():
            raise SkillLoadError(f"Skill instructions are empty: {name}")
        return LoadedSkill(metadata=item, instructions=body, path=path)

    def select(
        self,
        trigger: str,
        *,
        explicit_skill: str | None = None,
        classifier: Callable[[list[SkillMetadata], str], list[str]] | None = None,
        max_skills: int = 3,
    ) -> list[LoadedSkill]:
        if max_skills < 1 or max_skills > 3:
            raise SkillLoadError("max_skills must be between 1 and 3")
        metadata = self.discover()
        by_name = {item.name: item for item in metadata}
        if explicit_skill:
            if explicit_skill not in by_name:
                raise SkillLoadError(f"Requested skill is not available: {explicit_skill}")
            return [self.load(explicit_skill)]

        selected_names: list[str]
        if classifier is not None:
            selected_names = self._classify(classifier, metadata, trigger, max_skills)
        else:
            normalized = trigger.lower()
            selected_names = [
                item.name
                for item in metadata
                if any(term.lower() in normalized for term in item.activation_triggers)
                and not any(term.lower() in normalized for term in item.counterexamples)
            ][:max_skills]
        selected: list[LoadedSkill] = []
        for name in selected_names:
            if name not in by_name:
                raise SkillLoadError(f"Skill selector returned an unknown skill: {name}")
            selected.append(self.load(name))
        return selected

    def _classify(self, classifier, metadata, trigger, max_skills) -> list[str]:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(classifier, metadata, trigger)
        try:
            names = future.result(timeout=self._CLASSIFIER_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            raise SkillLoadError("Skill classifier exceeded its bounded time budget") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if not isinstance(names, list) or len(names) > max_skills or any(not isinstance(name, str) for name in names):
            raise SkillLoadError("Skill classifier returned an invalid or unbounded selection")
        return names

    def _skill_path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise SkillLoadError("Invalid skill name")
        path = (self.root / name / "SKILL.md").resolve()
        if path.parent.parent != self.root or not path.is_file() or path.is_symlink():
            raise SkillLoadError(f"Skill path is outside the allowlisted root: {name}")
        return path

    def _read_metadata(self, path: Path) -> SkillMetadata:
        raw = path.read_bytes()[: self._MAX_METADATA_BYTES]
        if len(raw) == self._MAX_METADATA_BYTES and b"\n---" not in raw:
            raise SkillLoadError(f"Skill metadata is too large or unterminated: {path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLoadError(f"Skill metadata is not UTF-8: {path}") from exc
        values = _parse_front_matter(text)
        try:
            return SkillMetadata.model_validate(values)
        except Exception as exc:
            raise SkillLoadError(f"Invalid skill metadata: {path}") from exc

    def _validate_required_tools(self, item: SkillMetadata) -> None:
        missing = set(item.required_tools) - self.available_tools
        if missing:
            raise SkillLoadError(f"Skill {item.name} requires unavailable tools: {sorted(missing)}")

    def _validate_content(self, text: str, metadata: SkillMetadata) -> None:
        lowered = text.lower()
        for pattern in self._UNSAFE_CONTENT:
            if re.search(pattern, lowered):
                raise SkillLoadError(f"Skill {metadata.name} contains an unsafe instruction pattern")
        if metadata.name not in text:
            raise SkillLoadError(f"Skill body does not identify its own skill: {metadata.name}")

    @staticmethod
    def _body(text: str) -> str:
        if not text.startswith("---"):
            raise SkillLoadError("SKILL.md must begin with YAML-style front matter")
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            raise SkillLoadError("SKILL.md front matter is unterminated")
        return parts[1].lstrip("\n")


def _parse_front_matter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise SkillLoadError("SKILL.md must begin with front matter")
    marker = text.find("\n---", 4)
    if marker < 0:
        raise SkillLoadError("SKILL.md front matter is unterminated")
    lines = text[4:marker].splitlines()
    values: dict[str, Any] = {}
    current_list: str | None = None
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("  - ") and current_list:
            values[current_list].append(line[4:].strip().strip('"\''))
            continue
        if ":" not in line:
            raise SkillLoadError("Invalid front matter line")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value:
            values[key] = raw_value.strip('"\'')
            current_list = None
        else:
            values[key] = []
            current_list = key
    return values

