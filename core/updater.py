"""Moduł odpowiedzialny za aktualizacje programu."""
import os
import sys
import requests
import subprocess
from core.version import get_version
from gui.dialogs import ModernUpdateDialog, ModernProgressDialog, show_message

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

def download_file(url, filename, progress_dialog=None):
    """Pobiera plik z podanego URL z obsługą postępu."""
    try:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            # Pobierz całkowity rozmiar pliku
            total_size = int(response.headers.get('content-length', 0))
            block_size = 8192
            downloaded = 0
            
            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=block_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_dialog and total_size:
                            progress = downloaded / total_size
                            progress_dialog.update_progress(
                                progress,
                                f"Pobieranie: {downloaded}/{total_size} bajtów"
                            )
            return True
        return False
    except Exception as e:
        if progress_dialog:
            progress_dialog.update_progress(0, f"Błąd: {str(e)}")
        return False

def check_for_updates(parent):
    """Sprawdza dostępność aktualizacji i pobiera nową wersję jeśli jest dostępna."""
    current_version = get_version()
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
                
                # Utwórz okno postępu
                progress_dialog = ModernProgressDialog(parent, "Aktualizacja")
                progress_dialog.update_progress(0, "Rozpoczynanie pobierania...")
                
                if download_file(asset['browser_download_url'], new_exe, progress_dialog):
                    progress_dialog.update_progress(1, "Tworzenie skryptu aktualizacyjnego...")
                    # Utwórz skrypt bat do wykonania aktualizacji
                    with open(update_script, 'w', encoding='utf-8') as f:
                        f.write('@echo off\n')
                        f.write('echo Aktualizacja w toku...\n')
                        f.write('timeout /t 2 /nobreak\n')  # Poczekaj 2 sekundy
                        f.write(f'del "{sys.executable}"\n')  # Usuń stary plik
                        f.write(f'move /y "{new_exe}" "{sys.executable}"\n')  # Przenieś nowy plik
                        f.write(f'start "" "{sys.executable}"\n')  # Uruchom nową wersję
                        f.write(f'del "%~f0"\n')  # Usuń skrypt bat
                    
                    progress_dialog.update_progress(1, "Uruchamianie aktualizacji...")
                    # Uruchom skrypt aktualizacyjny
                    subprocess.Popen(['cmd', '/c', update_script], creationflags=subprocess.CREATE_NEW_CONSOLE)
                    sys.exit(0)
                else:
                    progress_dialog.destroy()
                    show_message(parent, "Błąd podczas pobierania aktualizacji.")
                    return False

    dialog = ModernUpdateDialog(parent, current_version, latest_version, on_update_confirmed)
    dialog.wait_window()
    return False 