"""Configuration; secrets are never included in repr or command output."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    db_path: Path
    client_id: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)
    environment: str = "sandbox"
    redirect_uri: str | None = None


def load_config(env_file: Path | None = None) -> Config:
    base = env_file.resolve().parent if env_file is not None else Path.cwd()
    load_dotenv(env_file if env_file is not None else base / ".env", override=False)
    environment = os.getenv("PLAID_ENV", "sandbox").lower()
    if environment not in {"sandbox", "production"}:
        raise ValueError("PLAID_ENV must be sandbox or production")
    default_path = "~/.fin/sandbox.db" if environment == "sandbox" else "~/.fin/fin.db"
    db_path = Path(os.getenv("FIN_DB_PATH") or default_path).expanduser()
    if not db_path.is_absolute():
        db_path = base / db_path
    return Config(
        db_path.resolve(),
        os.getenv("PLAID_CLIENT_ID", ""), os.getenv("PLAID_SECRET", ""),
        environment, os.getenv("PLAID_REDIRECT_URI") or None,
    )
