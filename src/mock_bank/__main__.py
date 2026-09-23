import argparse

import uvicorn

from .app import create_app
from .variants import VARIANTS

parser = argparse.ArgumentParser(description="Run the CoreOne Teller mock core-banking app.")
parser.add_argument("--variant", choices=sorted(VARIANTS), default="pinnacle")
parser.add_argument("--port", type=int, default=8001)
args = parser.parse_args()
uvicorn.run(create_app(args.variant), host="127.0.0.1", port=args.port)
