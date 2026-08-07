"""Data model for Nix store closures.

Supports three wire formats, auto-detected from the JSON's shape:

* ``nix path-info --json --json-format 2`` nix >= 2.33
* ``nix path-info --json`` nix < 2.33
* Lix's ``path-info --json``

All three are normalized into the same :class:`Closure`/:class:`PathInfo`
shape (basename-keyed, ``ca`` as :class:`ContentAddress`) so downstream code
never needs to care which tool produced the data.
"""

import datetime as dt
import json
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import Literal, Self, TypedDict, cast

from nixcu.progress import Reporter, silent

SCHEMA_VERSION = 2
"""The ``--json-format`` we prefer when we control the invocation."""

LOAD_PHASES = 3
"""Phases :meth:`Closure.from_store` reports."""

HASH_LEN = 32
"""Length of the base32 hash that prefixes every store path basename."""


class RawContentAddress(TypedDict):
    """Wire shape of a format-2 ``ca`` object."""

    method: str
    hash: str


class RawPathInfo(TypedDict):
    """Wire shape of one format-2 ``info`` entry.

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


class RawFormat1Entry(TypedDict, total=False):
    """Wire shape of one value in nix's ``--json-format 1`` object.

    Keyed by absolute store path at the top level (see
    :meth:`Closure._from_format1_json`); ``references`` and ``deriver`` here
    are themselves absolute paths, and ``ca``, when present, is a
    ``{method, hash}`` object just like format 2's.
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


class RawLixEntry(TypedDict, total=False):
    """Wire shape of one element of Lix's ``path-info --json`` list.

    Every field but ``path`` and ``valid`` is optional in principle (an
    invalid/unqueryable path may be reported with just those two), so this
    is ``total=False`` and :meth:`PathInfo.from_lix_json` defaults
    accordingly. Unlike format 1/2, ``ca`` is a colon-delimited *string*.
    """

    path: str
    valid: bool
    narHash: str
    narSize: int
    closureSize: int
    references: list[str]
    registrationTime: int
    signatures: list[str]
    ultimate: bool
    deriver: str | None
    ca: str | None


class SchemaError(ValueError):
    """Raised when nix hands us JSON we do not know how to read."""


class CAMethod(StrEnum):
    """Content-addressing method of a CA path."""

    FLAT = "flat"
    NAR = "nar"
    TEXT = "text"
    GIT = "git"


@dataclass(frozen=True, slots=True)
class ContentAddress:
    """The ``ca`` field of a content-addressed path."""

    method: CAMethod
    hash: str

    @classmethod
    def from_json(cls, raw: RawContentAddress) -> Self:
        return cls(method=CAMethod(raw["method"]), hash=raw["hash"])

    @classmethod
    def from_string(cls, raw: str) -> Self:
        """Parse the format-1 / lix wire form, e.g.

        ``"fixed:r:sha256:1k31qcvb..."`` (NAR-addressed),
        ``"fixed:flat:sha256:..."`` (flat-addressed), or
        ``"text:sha256:..."`` (text-addressed).

        The method sub-token (``r``/``flat``) is collapsed into
        :class:`CAMethod` and the remaining ``<algo>:<digest>`` is kept
        together in ``hash``, matching how format-2's ``ca.hash`` already
        embeds the algorithm.
        """
        parts = raw.split(":", 1)
        if len(parts) != 2:
            raise SchemaError(f"unparsable ca string {raw!r}")
        method_token, rest = parts
        if method_token == "fixed":
            sub, rest = rest.split(":", 1)
            method = CAMethod.NAR if sub == "r" else CAMethod.FLAT
        else:
            method = CAMethod(method_token)
        return cls(method=method, hash=rest)


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

    @classmethod
    def from_format1_json(cls, store_dir: str, name: str, raw: RawFormat1Entry) -> Self:
        """Build from one value of a ``--json-format 1`` object.

        ``name`` is the basename already extracted from the top-level key
        by the caller (see :meth:`Closure._from_format1_json`);
        ``references``/``deriver`` inside ``raw`` are still absolute paths
        and get stripped down here to match format 2's basename convention.
        ``ca`` is already ``{method, hash}``-shaped, same as format 2.
        """
        prefix = store_dir + "/"

        def strip(p: str) -> str:
            return p[len(prefix) :] if p.startswith(prefix) else p

        ca = raw.get("ca")
        deriver = raw.get("deriver")
        return cls(
            name=name,
            nar_hash=raw.get("narHash", ""),
            nar_size=raw.get("narSize", 0),
            closure_size=raw.get("closureSize", raw.get("narSize", 0)),
            references=frozenset(strip(r) for r in raw.get("references", [])),
            registration_time=dt.datetime.fromtimestamp(
                raw.get("registrationTime", 0), dt.UTC
            ),
            signatures=tuple(raw.get("signatures", [])),
            ultimate=raw.get("ultimate", False),
            deriver=strip(deriver) if deriver else None,
            ca=ContentAddress.from_json(ca) if ca else None,
        )

    @classmethod
    def from_lix_json(cls, store_dir: str, raw: RawLixEntry) -> Self:
        """Build from one element of Lix's ``path-info --json`` list.

        ``raw["path"]`` is absolute; it's stripped of ``store_dir`` to get
        the basename, and ``references``/``deriver`` (also absolute in this
        format) are stripped the same way. ``ca``, unlike format 1/2, is a
        colon-delimited string and goes through :meth:`ContentAddress.from_string`.
        """
        prefix = store_dir + "/"
        abs_path = raw["path"]
        if not abs_path.startswith(prefix):
            raise SchemaError(f"path {abs_path!r} is not under storeDir {store_dir!r}")
        name = abs_path[len(prefix) :]

        def strip(p: str) -> str:
            return p[len(prefix) :] if p.startswith(prefix) else p

        ca = raw.get("ca")
        deriver = raw.get("deriver")
        return cls(
            name=name,
            nar_hash=raw.get("narHash", ""),
            nar_size=raw.get("narSize", 0),
            closure_size=raw.get("closureSize", raw.get("narSize", 0)),
            references=frozenset(strip(r) for r in raw.get("references", [])),
            registration_time=dt.datetime.fromtimestamp(
                raw.get("registrationTime", 0), dt.UTC
            ),
            signatures=tuple(raw.get("signatures", [])),
            ultimate=raw.get("ultimate", False),
            deriver=strip(deriver) if deriver else None,
            ca=ContentAddress.from_string(ca) if ca else None,
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
    """Paths nix reported as ``null`` / ``valid: false`` — queried but not
    valid in the store."""

    @classmethod
    def from_json(
        cls, raw: object, report: Reporter = silent, *, store_dir: str = "/nix/store"
    ) -> Self:
        """Build from freshly decoded JSON, auto-detecting the wire format.

        * A JSON *object* with a top-level ``"info"`` key is
          ``--json-format 2``: an envelope carrying ``storeDir`` and an
          ``info`` object keyed by basename.
        * A JSON *object* without an ``"info"`` key is ``--json-format 1``:
          keyed directly by *absolute store path*, values ``null`` for
          invalid paths or entries shaped like format 2's (each repeating
          its own ``storeDir``/``version``).
        * A JSON *array* is Lix's ``path-info --json``: a flat list of
          entries with absolute ``path``, a ``valid`` flag, and ``ca`` as a
          string rather than an object.

        ``store_dir`` is used as a fallback for the array (Lix) shape, which
        carries no ``storeDir`` anywhere in the payload; format 1 and 2 both
        supply their own and ignore the parameter.
        """
        if isinstance(raw, list):
            return cls._from_lix_json(raw, report, store_dir=store_dir)
        if not isinstance(raw, dict):
            raise SchemaError(
                f"expected a JSON object or array, got {type(raw).__name__}"
            )

        if "info" in raw:
            return cls._from_format2_json(raw, report)
        return cls._from_format1_json(raw, report)

    @classmethod
    def _from_format2_json(cls, raw: dict, report: Reporter) -> Self:
        if (version := raw.get("version")) != SCHEMA_VERSION:
            raise SchemaError(f"unsupported json-format {version!r}")

        envelope = cast(RawClosure, raw)
        info = envelope["info"]
        report.phase(f"reading {len(info)} store paths", len(info))
        paths: dict[str, PathInfo] = {}
        invalid: set[str] = set()
        try:
            for name, entry in info.items():
                if entry is None:
                    invalid.add(name)
                else:
                    paths[name] = PathInfo.from_json(name, entry)
                report.advance()
        except KeyError as e:
            raise SchemaError(
                f"entry missing field {e}; was nix run with --closure-size?"
            ) from e
        return cls(
            store_dir=envelope["storeDir"],
            paths=paths,
            invalid=frozenset(invalid),
        )

    @classmethod
    def _from_format1_json(cls, raw: dict, report: Reporter) -> Self:
        """Build from a ``--json-format 1`` object: keyed by absolute path,
        no envelope, ``null`` for invalid entries. Each present entry
        repeats its own ``storeDir``, which is used to derive the basename
        (falling back to splitting on the last ``/`` if the map is empty or
        every entry is ``null``, though that edge case shouldn't arise in
        practice since format 1 always includes at least the queried root).
        """
        entries = cast(dict[str, RawPathInfo | None], raw)
        store_dir = next(
            (e["storeDir"] for e in entries.values() if e is not None),
            None,
        )
        if store_dir is None:
            # every entry invalid (or map empty): fall back to parsing keys
            any_key = next(iter(entries), None)
            store_dir = any_key.rsplit("/", 1)[0] if any_key else "/nix/store"

        prefix = store_dir + "/"
        report.phase(f"reading {len(entries)} store paths", len(entries))
        paths: dict[str, PathInfo] = {}
        invalid: set[str] = set()
        try:
            for abs_path, entry in entries.items():
                name = (
                    abs_path[len(prefix) :] if abs_path.startswith(prefix) else abs_path
                )
                if entry is None:
                    invalid.add(name)
                else:
                    paths[name] = PathInfo.from_format1_json(store_dir, name, entry)
                report.advance()
        except KeyError as e:
            raise SchemaError(
                f"entry missing field {e}; was nix run with --closure-size?"
            ) from e
        return cls(store_dir=store_dir, paths=paths, invalid=frozenset(invalid))

    @classmethod
    def _from_lix_json(
        cls, raw: list[object], report: Reporter, *, store_dir: str
    ) -> Self:
        entries = cast(list[RawLixEntry], raw)
        prefix = store_dir + "/"
        report.phase(f"reading {len(entries)} store paths", len(entries))
        paths: dict[str, PathInfo] = {}
        invalid: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or "path" not in entry:
                raise SchemaError(f"malformed lix path-info entry: {entry!r}")
            abs_path = entry["path"]
            name = abs_path[len(prefix) :] if abs_path.startswith(prefix) else abs_path
            if not entry.get("valid", True):
                invalid.add(name)
            else:
                try:
                    paths[name] = PathInfo.from_lix_json(store_dir, entry)
                except KeyError as e:
                    raise SchemaError(f"entry missing field {e}") from e
            report.advance()
        return cls(store_dir=store_dir, paths=paths, invalid=frozenset(invalid))

    @classmethod
    def loads(
        cls,
        data: str | bytes,
        report: Reporter = silent,
        *,
        store_dir: str = "/nix/store",
    ) -> Self:
        report.phase("decoding json")
        return cls.from_json(json.loads(data), report, store_dir=store_dir)

    @classmethod
    def _detect_json_format(cls, nix: str) -> int | None:
        """Probe ``{nix} --version`` to decide which ``--json-format`` to
        request.

        Lix's version banner looks like ``"nix (Lix, like Nix) 2.95.2"``;
        real Nix's looks like ``"nix (Nix) 2.34.8"``. Lix doesn't understand
        ``--json-format`` at all (it only emits its own flat-list shape), so
        this returns ``None`` for it and skips the flag entirely; for real
        Nix it returns :data:`SCHEMA_VERSION` to request format 2.
        """
        out = subprocess.run(
            [nix, "--version"], capture_output=True, check=True, text=True
        ).stdout.splitlines()[0]
        major, minor, _ = tuple(map(int, out.split()[-1].split(".")))
        if "Lix" in out:
            return None
        if (2, 33) > (major, minor):
            return None

        return SCHEMA_VERSION

    @classmethod
    def from_store(
        cls,
        *roots: str,
        recursive: bool = True,
        nix: str = "nix",
        json_format: int | None | Literal["auto"] = "auto",
        store_dir: str = "/nix/store",
        report: Reporter = silent,
    ) -> Self:
        """Shell out to ``nix path-info`` for ``roots``.

        ``--closure-size`` is not optional: :attr:`PathInfo.closure_size` is a
        required field, so the query must always ask for it.

        ``json_format`` defaults to ``"auto"``, which runs ``{nix} --version``
        first (see :meth:`_detect_json_format`) and picks format 2 for real
        Nix or omits the flag for Lix, since Lix doesn't understand
        ``--json-format`` and only emits its own flat-list shape. Pass
        ``None``/``1``/``2`` explicitly to skip the probe and force a
        particular shape. ``store_dir`` is only consulted for the Lix shape,
        since nix formats 1 and 2 both carry their own ``storeDir`` per
        entry/envelope.
        """
        if json_format == "auto":
            json_format = cls._detect_json_format(nix)

        argv = [nix, "path-info", "--json", "--closure-size"]
        if json_format is not None:
            argv.extend(["--json-format", str(json_format)])
        if recursive:
            argv.append("--recursive")
        argv.extend(roots)
        report.phase("querying nix")
        out = subprocess.run(argv, capture_output=True, check=True).stdout
        return cls.loads(out, report, store_dir=store_dir)

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
