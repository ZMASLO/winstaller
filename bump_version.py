#!/usr/bin/env python3
"""Skrypt do zarządzania wersjami programu."""
import subprocess
import sys
import re
from datetime import datetime

def get_current_version():
    """Pobiera aktualną wersję z git tag."""
    try:
        return subprocess.check_output(['git', 'describe', '--tags', '--abbrev=0']).decode().strip()
    except:
        return "0.0.0"

def check_uncommitted_changes():
    """Sprawdza czy są niezacommitowane zmiany."""
    status = subprocess.check_output(['git', 'status', '--porcelain']).decode().strip()
    return bool(status)

def bump_version(current_version, bump_type):
    """Zwiększa numer wersji zgodnie z typem (major, minor, patch)."""
    # Usuń suffix -dev jeśli istnieje
    current_version = current_version.replace('-dev', '')
    
    major, minor, patch = map(int, current_version.split('.'))
    
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise ValueError("Nieprawidłowy typ aktualizacji wersji")

def create_changelog_entry(version, changes):
    """Tworzy wpis w CHANGELOG.md."""
    with open("CHANGELOG.md", "a") as f:
        f.write(f"\n## [{version}] - {datetime.now().strftime('%Y-%m-%d')}\n")
        for change in changes:
            f.write(f"- {change}\n")

def main():
    if len(sys.argv) < 2:
        print("Użycie: bump_version.py [major|minor|patch] ['opis zmian']")
        sys.exit(1)

    bump_type = sys.argv[1]
    if bump_type not in ["major", "minor", "patch"]:
        print("Typ aktualizacji musi być jednym z: major, minor, patch")
        sys.exit(1)

    # Sprawdź czy są niezacommitowane zmiany
    if check_uncommitted_changes():
        print("UWAGA: Wykryto niezacommitowane zmiany!")
        response = input("Czy chcesz kontynuować mimo to? (t/N): ")
        if response.lower() != 't':
            print("Przerwano aktualizację wersji. Najpierw zacommituj zmiany.")
            sys.exit(1)

    # Pobierz aktualną wersję
    current_version = get_current_version()
    new_version = bump_version(current_version, bump_type)
    
    # Pobierz opis zmian
    changes = []
    if len(sys.argv) > 2:
        changes = [sys.argv[2]]
    
    # Utwórz wpis w CHANGELOG.md
    create_changelog_entry(new_version, changes)
    
    # Utwórz nowy tag
    subprocess.run(['git', 'tag', '-a', new_version, '-m', f'Wersja {new_version}'])
    
    print(f"Zaktualizowano wersję z {current_version} do {new_version}")
    print("Nie zapomnij wypchnąć zmian i tagów na serwer:")
    print("git push && git push --tags")

if __name__ == "__main__":
    main() 