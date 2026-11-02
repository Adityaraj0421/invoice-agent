import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LEDGERLY_DATA_DIR"] = tempfile.mkdtemp(prefix="ledgerly-test-")
os.environ.pop("GEMINI_API_KEY", None)  # tests never call the API

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def base_url():
    from agent.serve import start_server

    url, server = start_server()
    yield url
    server.should_exit = True
