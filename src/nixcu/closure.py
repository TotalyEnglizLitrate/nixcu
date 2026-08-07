"""Data model for Nix store closures.

Wraps the JSON emitted by ``nix path-info --json --json-format 2``. Format 2
keys everything by *basename* and hoists ``storeDir`` to the top level, so the
absolute path of an entry is ``f"{closure.store_dir}/{name}"``.
"""

import datetime as dt
import json
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import Self, TypedDict, cast

SCHEMA_VERSION = 2
"""The ``--json-format`` we know how to read."""

HASH_LEN = 32
"""Length of the base32 hash that prefixes every store path basename."""


class RawContentAddress(TypedDict):
    """Wire shape of a ``ca`` object."""

    method: str
    hash: str


class RawPathInfo(TypedDict):
    """Wire shape of one ``info`` entry.

    ``storeDir`` and ``version`` are repeated per entry and redundant with the
    envelope. ``closureSize`` is only emitted under ``--closure-size``, which
    :meth:`Closure.from_store` always passes.
    """

    narHash: str
    narSize: int
    closureSize: int
    references: list[str]
    registrationTime: int
    signatures: list[str]
    ultimate: bool
    deriver: str | None
    ca: RawContentAddress | None
    storeDir: str
    version: int


class RawClosure(TypedDict):
    """Wire shape of the whole ``--json-format 2`` envelope."""

    version: int
    storeDir: str
    info: dict[str, RawPathInfo | None]


class SchemaError(ValueError):
    """Raised when nix hands us JSON we do not know how to read."""


class CAMethod(StrEnum):
    """Content-addressing method of a CA path."""

    FLAT = "flat"
    NAR  = "nar"
    TEXT = "text"
    GIT  = "git"


@dataclass(frozen=True, slots=True)
class ContentAddress:
    """The ``ca`` field of a content-addressed path."""

    method: CAMethod
    hash: str

    @classmethod
    def from_json(cls, raw: RawContentAddress) -> Self:
        return cls(method=CAMethod(raw["method"]), hash=raw["hash"])


@dataclass(frozen=True, slots=True)
class PathInfo:
    """One valid store path.

    ``name`` is the basename (``<hash>-<pname>``), not an absolute path;
    ``references`` and ``deriver`` are basenames too.
    """

    name: str
    nar_hash: str
    nar_size: int
    closure_size: int
    references: frozenset[str]
    registration_time: dt.datetime
    signatures: tuple[str, ...] = ()
    ultimate: bool = False
    deriver: str | None = None
    ca: ContentAddress | None = None

    @classmethod
    def from_json(cls, name: str, raw: RawPathInfo) -> Self:
        ca = raw["ca"]
        return cls(
            name=name,
            nar_hash=raw["narHash"],
            nar_size=raw["narSize"],
            closure_size=raw["closureSize"],
            references=frozenset(raw["references"]),
            registration_time=dt.datetime.fromtimestamp(
                raw["registrationTime"], dt.UTC
            ),
            signatures=tuple(raw["signatures"]),
            ultimate=raw["ultimate"],
            deriver=raw["deriver"],
            ca=ContentAddress.from_json(ca) if ca else None,
        )

    @property
    def hash_part(self) -> str:
        return self.name[:HASH_LEN]

    @property
    def pname(self) -> str:
        """Basename with the hash prefix stripped, e.g. ``zlib-1.3.2``."""
        return self.name[HASH_LEN + 1 :]

    @property
    def dependencies(self) -> frozenset[str]:
        """References excluding the self-reference nix records for many paths."""
        return self.references - {self.name}

    @property
    def is_substitutable(self) -> bool:
        """Whether a binary cache vouched for this path (it carries signatures)."""
        return bool(self.signatures)

    def path(self, store_dir: str) -> str:
        return f"{store_dir}/{self.name}"


@dataclass
class Closure:
    """A set of store paths and the reference edges between them.

    Nodes are keyed by basename. Edges may dangle: a path can reference
    something outside this closure when nix was not asked for it recursively.
    """

    store_dir: str
    paths: dict[str, PathInfo] = field(default_factory=dict)
    invalid: frozenset[str] = frozenset()
    """Paths nix reported as ``null`` — queried but not valid in the store."""

    @classmethod
    def from_json(cls, raw: object) -> Self:
        """Build from freshly decoded JSON, narrowing it to :class:`RawClosure`."""
        if isinstance(raw, list):
            raise SchemaError(
                "got --json-format 1 output (a list); pass --json-format 2"
            )
        if not isinstance(raw, dict):
            raise SchemaError(f"expected a JSON object, got {type(raw).__name__}")
        if (version := raw.get("version")) != SCHEMA_VERSION:
            raise SchemaError(f"unsupported json-format {version!r}")

        envelope = cast(RawClosure, raw)
        info = envelope["info"]
        try:
            paths = {
                name: PathInfo.from_json(name, entry)
                for name, entry in info.items()
                if entry is not None
            }
        except KeyError as e:
            raise SchemaError(
                f"entry missing field {e}; was nix run with --closure-size?"
            ) from e
        return cls(
            store_dir=envelope["storeDir"],
            paths=paths,
            invalid=frozenset(n for n, entry in info.items() if entry is None),
        )

    @classmethod
    def loads(cls, data: str | bytes) -> Self:
        return cls.from_json(json.loads(data))

    @classmethod
    def from_store(
        cls,
        *roots: str,
        recursive: bool = True,
        nix: str = "nix",
    ) -> Self:
        """Shell out to ``nix path-info`` for ``roots``.

        ``--closure-size`` is not optional: :attr:`PathInfo.closure_size` is a
        required field, so the query must always ask for it.
        """
        argv = [
            nix,
            "path-info",
            "--json",
            "--json-format",
            str(SCHEMA_VERSION),
            "--closure-size",
        ]
        if recursive:
            argv.append("--recursive")
        argv.extend(roots)
        out = subprocess.run(argv, capture_output=True, check=True).stdout
        return cls.loads(out)

    def __getitem__(self, name: str) -> PathInfo:
        return self.paths[name]

    def __contains__(self, name: str) -> bool:
        return name in self.paths

    def __iter__(self) -> Iterator[PathInfo]:
        return iter(self.paths.values())

    def __len__(self) -> int:
        return len(self.paths)

    @cached_property
    def referrers(self) -> dict[str, frozenset[str]]:
        """Reverse of the reference edges: name -> paths depending on it."""
        acc: dict[str, set[str]] = {name: set() for name in self.paths}
        for info in self.paths.values():
            for ref in info.dependencies:
                if ref in acc:
                    acc[ref].add(info.name)
        return {name: frozenset(refs) for name, refs in acc.items()}

    @cached_property
    def roots(self) -> frozenset[str]:
        """Paths nothing else in the closure depends on."""
        return frozenset(n for n, refs in self.referrers.items() if not refs)

    def reachable(self, *starts: str) -> frozenset[str]:
        """Transitive closure of ``starts``, including the starts themselves."""
        seen: set[str] = set()
        queue = [s for s in starts if s in self.paths]
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            queue.extend(r for r in self.paths[name].dependencies if r in self.paths)
        return frozenset(seen)

    def size_of(self, names: Iterable[str]) -> int:
        """On-disk bytes of the union of the closures of ``names``.

        Unlike summing ``closure_size``, shared dependencies are counted once.
        """
        return sum(self.paths[n].nar_size for n in self.reachable(*names))

    @cached_property
    def total_size(self) -> int:
        return sum(info.nar_size for info in self.paths.values())
