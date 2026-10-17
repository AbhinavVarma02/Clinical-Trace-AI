"""Start the real offline API with disposable state for browser tests only."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="clinical-trace-e2e-") as scratch:
        os.environ.update({
            "CLINICAL_TRACE_SKIP_DOTENV": "1",
            "CLINICAL_TRACE_LLM_PROVIDER": "none",
            "LANGCHAIN_TRACING_V2": "false",
            "CLINICAL_TRACE_DB_PATH": str(Path(scratch) / "test.db"),
            # Check the honest model-unavailable branch without loading local artifacts.
            "MODEL_PATH": str(Path(scratch) / "absent.joblib"),
            "CLINICAL_TRACE_RATE_LIMIT_PER_MIN": "10000",
            "CLINICAL_TRACE_AGENT_RATE_LIMIT_PER_MIN": "1000",
        })
        import uvicorn

        uvicorn.run("api.main:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
