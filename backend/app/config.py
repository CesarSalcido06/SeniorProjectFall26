"""Settings, read from environment variables (and backend/.env if present)."""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    # Tiny .env reader so we don't need python-dotenv; real env vars win.
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BACKEND_DIR / ".env")

CATEGORIES = ("prop", "character", "creature")


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.getenv("DATA_DIR", BACKEND_DIR / "data"))

        # Meshy: "mock" runs offline with a generated placeholder model (no credits used).
        # "live" calls the real API and needs MESHY_API_KEY. Default: live if a key is set.
        self.meshy_api_key = os.getenv("MESHY_API_KEY", "")
        self.meshy_mode = os.getenv("MESHY_MODE", "live" if self.meshy_api_key else "mock")
        self.meshy_base_url = os.getenv("MESHY_BASE_URL", "https://api.meshy.ai")
        self.meshy_poll_seconds = float(os.getenv("MESHY_POLL_SECONDS", "5"))
        self.mock_seconds = float(os.getenv("MOCK_SECONDS", "6"))

        # Classifier: "manual" uses the category picked at upload (unknown -> prop).
        # "openai" calls any OpenAI-compatible vision endpoint (BenchAI, OpenAI, a local llama-server...).
        self.classifier = os.getenv("CLASSIFIER", "manual")
        self.classifier_url = os.getenv("CLASSIFIER_URL", "http://127.0.0.1:8085/v1")
        self.classifier_model = os.getenv("CLASSIFIER_MODEL", "vision")
        self.classifier_api_key = os.getenv("CLASSIFIER_API_KEY", "")
        self.classifier_timeout = float(os.getenv("CLASSIFIER_TIMEOUT", "180"))

        # Photo quality gate
        self.max_upload_mb = float(os.getenv("MAX_UPLOAD_MB", "15"))
        self.min_side = int(os.getenv("MIN_SIDE", "512"))
        self.max_side = int(os.getenv("MAX_SIDE", "2048"))
        self.blur_threshold = float(os.getenv("BLUR_THRESHOLD", "100"))
        self.dark_threshold = float(os.getenv("DARK_THRESHOLD", "30"))
        self.bright_threshold = float(os.getenv("BRIGHT_THRESHOLD", "235"))

        self.max_concurrent_jobs = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    def job_dir(self, job_id: str) -> Path:
        return self.data_dir / "jobs" / job_id
