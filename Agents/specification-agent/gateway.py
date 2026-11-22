"""Connection settings shared by model and MCP requests."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

GATEWAY_URL = os.getenv("GOMODEL_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("GOMODEL_API_KEY") or os.getenv("GOMODEL_MASTER_KEY") or "local"
MODEL = os.getenv("GOMODEL_MODEL", "router")
