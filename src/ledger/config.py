import os
from typing import Final


def _required_environment_variable(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError as error:
        raise RuntimeError(f"Environment variable {name} must be set.") from error


DATABASE_URL: Final[str] = _required_environment_variable("LEDGER_DATABASE_URL")
