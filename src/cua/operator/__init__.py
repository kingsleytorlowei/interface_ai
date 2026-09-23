"""Mock operator console: shows intervention requests and signals resume/abort over
`cua.control`. The live browser window is the manual-control surface.
"""

from .console import create_console, serve_console

__all__ = ["create_console", "serve_console"]
