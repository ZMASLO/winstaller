"""Moduł odpowiedzialny za aktualizacje programu."""
import os
import sys
import requests
import subprocess
from core.version import get_version
from gui.dialogs import ModernUpdateDialog, show_message

GITHUB_REPO = "ZMASLO/winstaller"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

def get_latest_release_info():
    """Pobiera informacje o najnowszej wersji z GitHub."""
    try:
        response = requests.get(GITHUB_API_URL)
        if response.status_code == 200:
            return response.json()
        return None
    except:
        return None

def download_file(url, filename):
    """Pobiera plik z podanego URL."""
    try:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return True
        return False
    except:
        return False

def check_for_updates(parent):
    """Sprawdza dostępność aktualizacji i pobiera nową wersję jeśli jest dostępna."""
    current_version = get_version().replace('-dev', '')
    release_info = get_latest_release_info()
    
    if not release_info:
        show_message(parent, "Nie udało się sprawdzić aktualizacji.")
        return False
    
    latest_version = release_info['tag_name']
    
    if latest_version == current_version:
        show_message(parent, "Używasz najnowszej wersji programu.")
        return False
    
    def on_update_confirmed():
        # Pobierz asset .exe
        for asset in release_info['assets']:
            if asset['name'].endswith('.exe'):
                new_exe = "winstaller_new.exe"
                update_script = "update.bat"
                
                if download_file(asset['browser_download_url'], new_exe):
                    # Utwórz skrypt bat do wykonania aktualizacji
                    with open(update_script, 'w') as f:
                        f.write('@echo off\n')
                        f.write('timeout /t 2 /nobreak\n')  # Poczekaj 2 sekundy
                        f.write(f'del "{sys.executable}"\n')  # Usuń stary plik
                        f.write(f'move /y "{new_exe}" "{sys.executable}"\n')  # Przenieś nowy plik
                        f.write(f'start "" "{sys.executable}"\n')  # Uruchom nową wersję
                        f.write(f'del "%~f0"\n')  # Usuń skrypt bat
                    
                    # Uruchom skrypt aktualizacyjny
                    subprocess.Popen(['cmd', '/c', update_script])
                    sys.exit(0)
                
                show_message(parent, "Błąd podczas pobierania aktualizacji.")
                return False
    
    dialog = ModernUpdateDialog(parent, current_version, latest_version, on_update_confirmed)
    dialog.wait_window()
    return False 