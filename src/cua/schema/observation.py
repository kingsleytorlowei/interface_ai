"""A surface-neutral snapshot of what an operator would perceive: an accessibility tree (roles,
names, element refs) spanning all frames, plus the visible text. Produced by surface adapters,
consumed by the LLM (discovery) and by state classification (replay). Refs are only valid
until the next observation.
"""

from collections.abc import Iterator

from .common import Model


class UINode(Model):
    role: str
    name: str = ""
    ref: str | None = None
    text: str | None = None
    props: dict[str, str] = {}
    children: list["UINode"] = []

    def walk(self) -> Iterator["UINode"]:
        yield self
        for child in self.children:
            yield from child.walk()


class ElementInfo(Model):
    """What an operator would see of one resolved element; what policy judges an action by."""

    role: str
    name: str = ""
    tag: str | None = None
    input_type: str | None = None
    href: str | None = None  # absolute, for links


class Observation(Model):
    url: str
    title: str
    frame_urls: list[str]
    text: str
    tree: list[UINode]
    snapshot: str  # the adapter's native rendering of the tree, as shown to the LLM

    def nodes(self) -> Iterator[UINode]:
        for root in self.tree:
            yield from root.walk()

    def find_ref(self, ref: str) -> UINode | None:
        return next((n for n in self.nodes() if n.ref == ref), None)
