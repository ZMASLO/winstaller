"""Moduł zawierający informacje o wersji programu."""
import subprocess
import os

def get_version():
    """Pobiera wersję programu z git tag lub zwraca domyślną wersję."""
    try:
        # Próba pobrania najnowszego taga
        version = subprocess.check_output(['git', 'describe', '--tags', '--abbrev=0']).decode().strip()
        
        # Sprawdzenie czy są niezacommitowane zmiany
        status = subprocess.check_output(['git', 'status', '--porcelain']).decode().strip()
        
        if status:
            # Jeśli są zmiany, dodaj '-dev' do wersji
            version += '-dev'
            
        return version
    except:
        # Jeśli nie ma tagów lub nie jesteśmy w repozytorium git
        return "1.0.0"

def get_commit_hash():
    """Pobiera skrócony hash ostatniego commita."""
    try:
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode().strip()
    except:
        return "unknown"

VERSION = get_version()
COMMIT_HASH = get_commit_hash()

def get_version_info():
    """Zwraca pełne informacje o wersji."""
    return f"Winstaller {VERSION} ({COMMIT_HASH})" 