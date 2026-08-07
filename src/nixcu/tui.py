"""Textual front end: drill down the dominator tree."""

from typing import final, override

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Tree
from textual.widgets.tree import TreeNode

from nixcu.dominators import DomNode, DominatorTree

__all__ = ["NixcuApp", "human_size"]

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def human_size(size: int) -> str:
    value = float(size)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


@final
class ClosureTree(Tree[str]):
    """Dominator tree, populated lazily as nodes are expanded.

    A closure has thousands of paths; building the whole widget tree up front
    would stall the mount for no benefit, since the interesting nodes are all
    within a few levels of the root.
    """

    def __init__(self, dom: DominatorTree) -> None:
        self.dom = dom
        super().__init__(self._label(dom.nodes[dom.root]), data=dom.root)
        self.show_root = True
        self.guide_depth = 3

    def _label(self, node: DomNode) -> str:
        info = self.dom.closure[node.key]
        parts = [f"[b]{human_size(node.exclusive_size)}[/b]", info.pname]
        if node.own_size != node.exclusive_size:
            parts.append(f"[dim](own {human_size(node.own_size)})[/dim]")
        if node.is_cycle:
            parts.append(f"[yellow](cycle of {len(node.members)})[/yellow]")
        return "  ".join(parts)

    @override
    def on_mount(self) -> None:
        _ = self.root.expand()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[str]) -> None:
        self._populate(event.node)

    def _populate(self, node: TreeNode[str]) -> None:
        if node.children or node.data is None:
            return
        for child in self.dom.kids(node.data):
            _ = node.add(
                self._label(child),
                data=child.key,
                allow_expand=bool(self.dom.children.get(child.key)),
            )


@final
class NixcuApp(App[None]):
    """Explore what a closure is actually made of."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "copy_path", "Copy path"),
    ]

    def __init__(
        self,
        dom: DominatorTree,
        root_path: str,
        inline: int | None = 15,
        elapsed: float | None = None,
    ) -> None:
        super().__init__()
        self.dom = dom
        self.root_path = root_path
        self.inline = inline
        self.elapsed = elapsed
        """Seconds spent querying and analysing, for the subtitle."""

    @override
    def compose(self) -> ComposeResult:
        yield Header()
        yield ClosureTree(self.dom)
        yield Footer()

    def on_mount(self) -> None:
        total = human_size(self.dom.total_size)
        self.title = "nixcu"
        subtitle = f"{self.root_path} — {total} over {len(self.dom)} paths"
        if self.elapsed is not None:
            subtitle += f" in {self.elapsed:.1f}s"
        self.sub_title = subtitle
        if self.inline is not None:
            self.screen.styles.height = (self.inline or 15) + 2

    def action_copy_path(self) -> None:
        """Yank the selected store path to the clipboard."""
        tree = self.query_one(ClosureTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        path = self.dom.closure[node.data].path(self.dom.closure.store_dir)
        self.copy_to_clipboard(path)
        self.notify(path, title="copied")
