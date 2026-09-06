from __future__ import annotations

import getpass
import os
import subprocess


KEYCHAIN_SERVICE = "Model Deck OpenAI API"


def get_openai_api_key() -> str | None:
    from_environment = os.getenv("OPENAI_API_KEY", "").strip()
    if from_environment:
        return from_environment
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            getpass.getuser(),
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    return result.stdout.strip() or None


def set_openai_api_key(value: str) -> None:
    key = value.strip()
    if not key:
        raise ValueError("API key cannot be empty")
    result = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            getpass.getuser(),
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
            key,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not store API key")


def delete_openai_api_key() -> bool:
    result = subprocess.run(
        [
            "/usr/bin/security",
            "delete-generic-password",
            "-a",
            getpass.getuser(),
            "-s",
            KEYCHAIN_SERVICE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0

