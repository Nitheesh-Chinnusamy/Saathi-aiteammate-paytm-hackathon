import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# Empty (not removed) so app.py's load_dotenv() cannot re-fill them from .env: tests always run in mock mode.
os.environ["SARVAM_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["MCP_TRANSPORT"] = "inprocess"

from saathi.core import logger  # noqa: E402

logger.QUIET_STDOUT = True


@pytest.fixture(autouse=True)
def world():
    from saathi.services import pipeline
    pipeline.reset_world()
    yield
