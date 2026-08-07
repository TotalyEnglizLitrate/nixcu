"""Size attribution within a single closure.

``PathInfo.closure_size`` tells you what a path drags in, but shared
dependencies mean those numbers overlap wildly and cannot be compared. This
module answers the question that *is* comparable: if nothing in the closure
pulled this path in anymore, how many bytes would leave?

That is the dominator tree of the reference graph, rooted at the closure root.
A path's *exclusive size* is the total ``nar_size`` of everything it dominates
- everything reachable only by going through it.

Reference graphs contain cycles (multi-output derivations reference each
other), and a cycle has no meaningful dominance ordering, so strongly-connected
components are condensed to a single node first. Most components are singletons.
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import cached_property

from nixcu.closure import Closure

__all__ = ["DomNode", "DominatorTree"]


def _strongly_connected(
    nodes: Iterable[str], successors: Callable[[str], Iterable[str]]
) -> list[frozenset[str]]:
    """Tarjan's SCC, iterative so deep reference chains cannot blow the stack."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[frozenset[str]] = []
    counter = 0

    for start in nodes:
        if start in index:
            continue
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        work: list[tuple[str, Iterator[str]]] = [(start, iter(successors(start)))]

        while work:
            node, pending = work[-1]
            descended = False
            for succ in pending:
                if succ not in index:
                    index[succ] = low[succ] = counter
                    counter += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(successors(succ))))
                    descended = True
                    break
                if succ in on_stack:
                    low[node] = min(low[node], index[succ])
            if descended:
                continue

            _ = work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(frozenset(component))

    return components


@dataclass(frozen=True, slots=True)
class DomNode:
    """One node of the dominator tree.

    Usually a single store path; a set of them when they form a reference
    cycle and are therefore inseparable.
    """

    key: str
    """Representative member, chosen as the largest by ``nar_size``."""

    members: frozenset[str]
    own_size: int
    """``nar_size`` summed over ``members``."""

    exclusive_size: int
    """``own_size`` plus everything this node dominates."""

    parent: str | None
    """Key of the immediate dominator; ``None`` for the root."""

    @property
    def is_cycle(self) -> bool:
        return len(self.members) > 1


@dataclass
class DominatorTree:
    """Dominator tree over a closure's reference graph."""

    closure: Closure
    root: str
    """Key of the root node."""

    nodes: dict[str, DomNode] = field(default_factory=dict)
    children: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _key_of: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def build(cls, closure: Closure, root: str | None = None) -> "DominatorTree":
        """Build the tree for ``closure``, rooted at ``root``.

        ``root`` defaults to the closure's sole root; pass one explicitly when
        the closure has several.
        """
        if root is None:
            roots = closure.roots
            if len(roots) != 1:
                raise ValueError(
                    f"closure has {len(roots)} roots; pass one explicitly"
                )
            (root,) = roots
        if root not in closure:
            raise KeyError(root)

        live = closure.reachable(root)

        def deps(name: str) -> Iterable[str]:
            return (d for d in closure[name].dependencies if d in live)

        # Condense cycles: dominance is undefined inside an SCC.
        components = _strongly_connected(live, deps)
        key_of: dict[str, str] = {}
        members_of: dict[str, frozenset[str]] = {}
        for component in components:
            key = max(component, key=lambda n: (closure[n].nar_size, n))
            members_of[key] = component
            for member in component:
                key_of[member] = key

        root_key = key_of[root]
        own_size = {
            key: sum(closure[m].nar_size for m in members)
            for key, members in members_of.items()
        }

        succs: dict[str, frozenset[str]] = {}
        preds: dict[str, set[str]] = {key: set() for key in members_of}
        for key, members in members_of.items():
            out = {key_of[d] for m in members for d in deps(m)} - {key}
            succs[key] = frozenset(out)
            for target in out:
                preds[target].add(key)

        order = _reverse_postorder(root_key, succs)
        idom = _immediate_dominators(root_key, order, preds)

        children: dict[str, list[str]] = {key: [] for key in members_of}
        for key, parent in idom.items():
            if key != root_key:
                children[parent].append(key)

        exclusive = _subtree_sizes(root_key, children, own_size)

        tree = cls(
            closure=closure,
            root=root_key,
            nodes={
                key: DomNode(
                    key=key,
                    members=members_of[key],
                    own_size=own_size[key],
                    exclusive_size=exclusive[key],
                    parent=None if key == root_key else idom[key],
                )
                for key in idom
            },
            children={
                key: tuple(sorted(kids, key=lambda k: (-own_size[k], k)))
                for key, kids in children.items()
                if key in idom
            },
            _key_of=key_of,
        )
        return tree

    # -- access ----------------------------------------------------------

    def __getitem__(self, name: str) -> DomNode:
        """Look up by any member name, not just the representative key."""
        return self.nodes[self._key_of.get(name, name)]

    def __contains__(self, name: str) -> bool:
        return self._key_of.get(name, name) in self.nodes

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
            (n for n in self.nodes.values() if n.key != self.root),
            key=lambda n: (-n.exclusive_size, n.key),
        )
        return ranked[:limit]

    def kids(self, name: str) -> tuple[DomNode, ...]:
        """Dominator-tree children, fattest first by ``own_size``.

        Ordering by own rather than exclusive size keeps the near-weightless
        glue paths (``etc``, ``system-units``) from crowding the top, at the
        cost of sorting a small node that dominates a large subtree low.
        """
        key = self._key_of.get(name, name)
        return tuple(self.nodes[k] for k in self.children.get(key, ()))


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
