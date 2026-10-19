"""Configuration without credentials in source code or current-directory dependence."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    root: Path = PROJECT_ROOT
    api_key: str = field(default="", repr=False)
    model: str = "openrouter/free"
    embedding_backend: str = "hash"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    allow_model_download: bool = False
    transactions_path: Path = PROJECT_ROOT / "data/transactions.json"
    goals_path: Path = PROJECT_ROOT / "data/goals.json"
    fx_path: Path = PROJECT_ROOT / "data/fx_fallback.json"
    knowledge_dir: Path = PROJECT_ROOT / "knowledge"
    store_dir: Path = PROJECT_ROOT / "rag_store"
    database_path: Path | None = None
    demo_shared_ai: bool = False

    @property
    def db_path(self) -> Path:
        return self.database_path or self.root / "data/finance.sqlite3"


def load_settings(root: Path = PROJECT_ROOT, *, host_secrets=None) -> Settings:
    root = Path(root).resolve()
    secrets_file = root / ".streamlit/secrets.toml"
    secrets = dict(host_secrets or {})
    if secrets_file.exists():
        try:
            secrets.update(tomllib.loads(secrets_file.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ValueError("Could not read .streamlit/secrets.toml; check its TOML syntax.") from exc

    def value(name, default=""):
        return str(os.environ.get(name, secrets.get(name, default))).strip()

    def path(name, default):
        result = Path(value(name, default)).expanduser()
        return result.resolve() if result.is_absolute() else (root / result).resolve()

    backend = value("EMBEDDING_BACKEND", "hash")
    if backend not in {"hash", "sentence_transformers"}:
        raise ValueError("EMBEDDING_BACKEND must be hash or sentence_transformers.")
    model = value("OPENROUTER_MODEL", "openrouter/free")
    if not model:
        raise ValueError("OPENROUTER_MODEL cannot be empty.")
    return Settings(
        root=root, api_key=value("OPENROUTER_API_KEY"), model=model,
        embedding_backend=backend,
        embedding_model=value("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        allow_model_download=value("ALLOW_MODEL_DOWNLOAD", "false").lower() == "true",
        transactions_path=path("TRANSACTIONS_PATH", "data/transactions.json"),
        goals_path=path("GOALS_PATH", "data/goals.json"),
        fx_path=path("FX_FALLBACK_PATH", "data/fx_fallback.json"),
        knowledge_dir=path("KNOWLEDGE_DIR", "knowledge"),
        store_dir=path("RAG_STORE_DIR", "rag_store"),
        database_path=path("DATABASE_PATH", "data/finance.sqlite3"),
        demo_shared_ai=value("DEMO_SHARED_AI", "false").lower() == "true",
    )
