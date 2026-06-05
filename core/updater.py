"""Moduł odpowiedzialny za aktualizacje programu."""
import os
import sys
import platform
import requests
import subprocess
from core.version import get_version
from gui.dialogs import ModernUpdateDialog, ModernProgressDialog, show_message

GITHUB_REPO = "ZMASLO/winstaller"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def get_latest_release_info():
    """Pobiera informacje o najnowszej wersji z GitHub."""
    try:
        response = requests.get(GITHUB_API_URL, timeout=10)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception:
        return None


def download_file(url, filename, progress_dialog=None):
    """Pobiera plik z podanego URL z obsługą postępu."""
    try:
        response = requests.get(url, stream=True, timeout=30)
        if response.status_code == 200:
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

    latest_version = release_info['tag_name'].lstrip('v')

    if latest_version == current_version:
        show_message(parent, "Używasz najnowszej wersji programu.")
        return False

    def on_update_confirmed():
        machine = platform.machine().lower()
        arch_suffix = "arm64" if machine == "arm64" else "x64"

        target_asset = None
        for asset in release_info['assets']:
            if asset['name'].endswith('.exe') and arch_suffix in asset['name']:
                target_asset = asset
                break

        if not target_asset:
            show_message(parent, f"Nie znaleziono pliku {arch_suffix} do pobrania.")
            return False

        exe_dir = os.path.dirname(sys.executable)
        new_exe = os.path.join(exe_dir, "winstaller_new.exe")
        update_script = os.path.join(exe_dir, "update.bat")
        old_exe_backup = sys.executable + ".old"

        progress_dialog = ModernProgressDialog(parent, "Aktualizacja")
        progress_dialog.update_progress(0, "Rozpoczynanie pobierania...")

        if download_file(target_asset['browser_download_url'], new_exe, progress_dialog):
            progress_dialog.update_progress(1, "Tworzenie skryptu aktualizacyjnego...")

            with open(update_script, 'w', encoding='utf-8') as f:
                f.write('@echo off\n')
                f.write('cd /d "%~dp0"\n')
                f.write('echo Aktualizacja w toku...\n')
                f.write('timeout /t 5 /nobreak >nul\n')
                f.write(f'ren "{sys.executable}" "winstaller.exe.old" 2>nul\n')
                f.write(f'move /y "winstaller_new.exe" "{os.path.basename(sys.executable)}"\n')
                f.write(f'start "" "{os.path.basename(sys.executable)}"\n')
                f.write('del "%~f0"\n')
                f.write(f'del "winstaller.exe.old" 2>nul\n')

            progress_dialog.update_progress(1, "Uruchamianie aktualizacji...")
            subprocess.Popen(
                ['cmd', '/c', update_script],
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
            sys.exit(0)
        else:
            progress_dialog.destroy()
            show_message(parent, "Błąd podczas pobierania aktualizacji.")

    dialog = ModernUpdateDialog(parent, current_version, latest_version, on_update_confirmed)
    dialog.wait_window()
    return False