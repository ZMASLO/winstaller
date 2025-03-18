"""Moduł zawierający informacje o wersji programu."""
import subprocess
import os

def get_version():
    return "1.1.2"

VERSION = get_version()

def get_version_info():
    """Zwraca pełne informacje o wersji."""
    return f"Winstaller {VERSION}" 