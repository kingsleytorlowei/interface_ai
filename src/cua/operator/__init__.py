"""Mock operator console: shows intervention requests and signals resume/abort over
`cua.control`. The live browser window is the manual-control surface.
"""

from .console import add_desk_routes, create_console, serve_console

__all__ = ["add_desk_routes", "create_console", "serve_console"]
