"""Size attribution within a single closure.

``PathInfo.closure_size`` tells you what a path drags in, but shared
dependencies mean those numbers overlap wildly and cannot be compared. This
module answers the question that *is* comparable: if nothing in the closure
pulled this path in anymore, how many bytes would leave?

That is the dominator tree of the reference graph, rooted at the closure root.
A path's *exclusive size* is the total ``nar_size`` of everything it dominates
- everything reachable only by going through it.

The graph is taken to be acyclic, which nix guarantees.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cached_property

from nixcu.closure import Closure
from nixcu.progress import Reporter, silent

__all__ = ["DomNode", "DominatorTree"]

BUILD_PHASES = 3
"""Phases :meth:`DominatorTree.build` reports."""


@dataclass(frozen=True, slots=True)
class DomNode:
    """One store path, and what it accounts for."""

    name: str
    """Basename of the store path, as in :class:`~nixcu.closure.PathInfo`."""

    own_size: int
    """The path's own ``nar_size``."""

    exclusive_size: int
    """``own_size`` plus everything this path dominates."""

    parent: str | None
    """Name of the immediate dominator; ``None`` for the root."""


@dataclass
class DominatorTree:
    """Dominator tree over a closure's reference graph."""

    closure: Closure
    root: str
    """Key of the root node."""

    nodes: dict[str, DomNode] = field(default_factory=dict)
    children: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        closure: Closure,
        root: str | None = None,
        report: Reporter = silent,
    ) -> "DominatorTree":
        """Build the tree for ``closure``, rooted at ``root``.

        ``root`` defaults to the closure's sole root; pass one explicitly when
        the closure has several.
        """
        if root is None:
            roots = closure.roots
            if len(roots) != 1:
                raise ValueError(f"closure has {len(roots)} roots; pass one explicitly")
            (root,) = roots
        if root not in closure:
            raise KeyError(root)

        live = closure.reachable(root)

        def deps(name: str) -> Iterable[str]:
            return (d for d in closure[name].dependencies if d in live)

        report.phase(f"indexing {len(live)} paths")
        own_size = {name: closure[name].nar_size for name in live}
        succs: dict[str, frozenset[str]] = {}
        preds: dict[str, set[str]] = {name: set() for name in live}
        for name in live:
            out = frozenset(deps(name))
            succs[name] = out
            for target in out:
                preds[target].add(name)

        report.phase("computing dominators")
        order = _reverse_postorder(root, succs)
        _assert_acyclic(order, succs)
        idom = _immediate_dominators(root, order, preds)

        children: dict[str, list[str]] = {name: [] for name in live}
        for name, parent in idom.items():
            if name != root:
                children[parent].append(name)

        report.phase("attributing sizes")
        exclusive = _subtree_sizes(root, children, own_size)

        return cls(
            closure=closure,
            root=root,
            nodes={
                name: DomNode(
                    name=name,
                    own_size=own_size[name],
                    exclusive_size=exclusive[name],
                    parent=None if name == root else idom[name],
                )
                for name in idom
            },
            children={
                name: tuple(sorted(kids, key=lambda k: (-own_size[k], k)))
                for name, kids in children.items()
                if name in idom
            },
        )

    def __getitem__(self, name: str) -> DomNode:
        return self.nodes[name]

    def __contains__(self, name: str) -> bool:
        return name in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[DomNode]:
        return iter(self.nodes.values())

    @cached_property
    def total_size(self) -> int:
        """Bytes in the whole tree; equals the root's ``exclusive_size``."""
        return self.nodes[self.root].exclusive_size

    def biggest(self, limit: int = 20) -> list[DomNode]:
        """Nodes ranked by what dropping them would actually free."""
        ranked = sorted(
            (n for n in self.nodes.values() if n.name != self.root),
            key=lambda n: (-n.exclusive_size, n.name),
        )
        return ranked[:limit]

    def kids(self, name: str) -> tuple[DomNode, ...]:
        """Dominator-tree children, fattest first by ``own_size``.

        Ordering by own rather than exclusive size keeps the near-weightless
        glue paths (``etc``, ``system-units``) from crowding the top, at the
        cost of sorting a small node that dominates a large subtree low.
        """
        return tuple(self.nodes[k] for k in self.children.get(name, ()))


def _reverse_postorder(root: str, succs: dict[str, frozenset[str]]) -> list[str]:
    """Reverse postorder from ``root``, iterative."""
    postorder: list[str] = []
    seen = {root}
    work: list[tuple[str, Iterator[str]]] = [(root, iter(sorted(succs[root])))]
    while work:
        node, pending = work[-1]
        for succ in pending:
            if succ not in seen:
                seen.add(succ)
                work.append((succ, iter(sorted(succs[succ]))))
                break
        else:
            _ = work.pop()
            postorder.append(node)
    postorder.reverse()
    return postorder


def _assert_acyclic(order: list[str], succs: dict[str, frozenset[str]]) -> None:
    """Confirm ``order`` really is topological, which it is iff there is no cycle."""
    rank = {name: i for i, name in enumerate(order)}
    for name in order:
        for succ in succs[name]:
            if rank[succ] <= rank[name]:
                raise ValueError(
                    f"reference cycle through {name!r} -> {succ!r}; nix should never emit one, so this closure is malformed"
                )


def _immediate_dominators(
    root: str, order: list[str], preds: dict[str, set[str]]
) -> dict[str, str]:
    """Cooper-Harvey-Kennedy iterative dominators.

    Simpler than Lengauer-Tarjan and fast enough here: reference DAGs are
    shallow, so this converges in a couple of passes.
    """
    rank = {node: i for i, node in enumerate(order)}
    idom: dict[str, str] = {root: root}

    def intersect(a: str, b: str) -> str:
        while a != b:
            while rank[a] > rank[b]:
                a = idom[a]
            while rank[b] > rank[a]:
                b = idom[b]
        return a

    changed = True
    while changed:
        changed = False
        for node in order:
            if node == root:
                continue
            candidate: str | None = None
            for pred in preds[node]:
                if pred not in idom or pred not in rank:
                    continue
                candidate = pred if candidate is None else intersect(pred, candidate)
            if candidate is not None and idom.get(node) != candidate:
                idom[node] = candidate
                changed = True
    return idom


def _subtree_sizes(
    root: str, children: dict[str, list[str]], own_size: dict[str, int]
) -> dict[str, int]:
    """Accumulate ``own_size`` up the dominator tree, iteratively."""
    total: dict[str, int] = {}
    work: list[tuple[str, bool]] = [(root, False)]
    while work:
        node, expanded = work.pop()
        if expanded:
            total[node] = own_size[node] + sum(
                total[kid] for kid in children.get(node, ())
            )
            continue
        work.append((node, True))
        for kid in children.get(node, ()):
            work.append((kid, False))
    return total
