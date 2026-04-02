#!/usr/bin/env python3
"""Skrypt do zarządzania wersjami programu."""
import subprocess
import sys
import re
from datetime import datetime

def get_current_version():
    """Pobiera aktualną wersję z git tag (bez prefiksu v)."""
    try:
        tag = subprocess.check_output(
            ['git', 'describe', '--tags', '--abbrev=0'],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        return tag.lstrip('v')
    except subprocess.CalledProcessError:
        with open("core/version.py", "r", encoding="utf-8") as f:
            content = f.read()
            match = re.search(r'return "(\d+\.\d+\.\d+)"', content)
            if match:
                return match.group(1)
        return "0.0.0"

def check_uncommitted_changes():
    """Sprawdza czy są niezacommitowane zmiany."""
    try:
        status = subprocess.check_output(['git', 'status', '--porcelain']).decode().strip()
        return bool(status)
    except subprocess.CalledProcessError:
        return False

def bump_version(current_version, bump_type):
    """Zwiększa numer wersji zgodnie z typem (major, minor, patch)."""
    current_version = current_version.lstrip('v').replace('-dev', '')
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
    with open("CHANGELOG.md", "a", encoding="utf-8") as f:
        f.write(f"\n## [{version}] - {datetime.now().strftime('%Y-%m-%d')}\n")
        for change in changes:
            f.write(f"- {change}\n")

def update_version_file(new_version):
    """Aktualizuje wersję w pliku core/version.py."""
    with open("core/version.py", "r", encoding="utf-8") as f:
        content = f.read()

    updated_content = re.sub(
        r'return "\d+\.\d+\.\d+"',
        f'return "{new_version}"',
        content
    )

    with open("core/version.py", "w", encoding="utf-8") as f:
        f.write(updated_content)

def run(cmd, check=True):
    result = subprocess.run(cmd, check=check)
    return result.returncode == 0

def main():
    if len(sys.argv) < 2:
        print("Użycie: bump_version.py [major|minor|patch] ['opis zmian']")
        sys.exit(1)

    bump_type = sys.argv[1]
    if bump_type not in ["major", "minor", "patch"]:
        print("Typ aktualizacji musi być jednym z: major, minor, patch")
        sys.exit(1)

    if check_uncommitted_changes():
        print("UWAGA: Wykryto niezacommitowane zmiany!")
        response = input("Czy chcesz kontynuować mimo to? (t/N): ")
        if response.lower() != 't':
            print("Przerwano. Najpierw zacommituj zmiany.")
            sys.exit(1)

    current_version = get_current_version()
    new_version = bump_version(current_version, bump_type)
    tag = f"v{new_version}"

    changes = [sys.argv[2]] if len(sys.argv) > 2 else []

    # 1. Aktualizuj pliki
    update_version_file(new_version)
    create_changelog_entry(new_version, changes)
    print(f"Zaktualizowano core/version.py i CHANGELOG.md do {new_version}")

    # 2. Commituj zmiany
    run(['git', 'add', 'core/version.py', 'CHANGELOG.md'])
    run(['git', 'commit', '-m', f'bump version to {new_version}'])

    # 3. Utwórz tag z prefiksem v
    run(['git', 'tag', '-a', tag, '-m', f'Wersja {new_version}'])
    print(f"Utworzono tag {tag}")

    print(f"\nAby uruchomić GitHub Actions, wykonaj:")
    print(f"  git push origin main && git push origin {tag}")

if __name__ == "__main__":
    main()
