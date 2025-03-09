import sys
import ctypes
import subprocess
import ctypes
from gui.app import ModernApp
from gui.dialogs import show_message
from core.system_utils import is_admin, check_winget_installed
from core.config import CATEGORIES

# Słownik funkcji jest generowany automatycznie z kategorii
checkbox_function = {name: func for category in CATEGORIES.values() for name, func in category.items()}

if __name__ == "__main__":
    if not is_admin():
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, sys.argv[0], None, 1)
        sys.exit()

    app = ModernApp()
    
    if not check_winget_installed():
        show_message(app, "Instalowanie winget!")
        try:
            subprocess.run(["powershell", "-Command", "Add-AppxPackage https://github.com/microsoft/winget-cli/releases/latest/download/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle"])
        except Exception as e:
            show_message(app, "Problem podczas instalacji winget "+str(e))

    # Generowanie interfejsu z kategorii
    for category_name, items in CATEGORIES.items():
        app.create_category_label(category_name)
        for item_name in items:
            app.create_checkbox(item_name)
    
    app.mainloop()
