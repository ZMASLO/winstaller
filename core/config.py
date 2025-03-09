from core.installers import *
from core.system_utils import *
from core.version import get_version_info

# Konfiguracja aplikacji
APP_CONFIG = {
    "title": get_version_info(),
    "window_size": "600x750",
    "window_alpha": 0.98,
    "sidebar_width": 200
}

# Struktura kategorii i checkboxów
CATEGORIES = {
    "Podstawowe programy": {
        "Google Chrome": install_google_chrome,
        "Telegram": install_telegram,
        "Messenger": install_messenger,
        "Discord": install_discord,
        "TeamSpeak3": install_ts3,
        "7-Zip": install_7zip,
        "Windows Terminal": install_windows_terminal
    },
    "Launchery": {
        "Steam": install_steam,
        "Epic Games Store": install_epic_games_store,
        "Ubisoft Connect": install_ubisoft_connect,
        "EA Desktop": install_ea_desktop,
        "Battle.net": install_battle_net
    },
    "Narzędzia diagnostyczne": {
        "HW Monitor": install_hw_monitor,
        "HW Info": install_hw_info,
        "CPU-Z": install_cpuz,
        "GPU-Z": install_gpuz,
        "DisplayCal": install_displaycal,
        "DisplayDriverUninstaller": install_displaydriveruninstaller,
        "NVCleanstall": install_nvcleanstall
    },
    "Narzędzia testowe": {
        "DirectX 9": install_directx9,
        "MSI Afterburner": install_msi_afterburner,
        "Rivatuner": install_rivatuner,
        "CapFrameX": install_capframex,
        "UL Procyon": install_ul_procyon
    },
    "Aplikacje profesjonalne": {
        "Davinci Resolve Studio": install_davinci_resolve_studio,
        "Creative Cloud": install_creativecloud,
        "Blender": install_blender,
        "LM Studio": install_lm_studio
    },
    "Konfiguracja systemu": {
        "Usuń bloatware z Windows": remove_bloat,
        "Ciemny motyw Windows": windows_dark_mode,
        "Jasny motyw Windows": windows_light_mode,
        "Zainstaluj OneDrive": install_onedrive,
        "Odinstaluj OneDrive": uninstall_onedrive
    },
    "Dodatkowe": {
        "Kopiuj BenchmarkTools na pulpit": copy_benchmark_tools,
        "Kopiuj winstaller na pulpit": copy_winstaller,
        "Instaluj lokalne oprogramowanie": install_local_software
    }
}

# Słownik funkcji jest generowany automatycznie z kategorii
CHECKBOX_FUNCTIONS = {name: func for category in CATEGORIES.values() for name, func in category.items()}

# Lista programów do instalacji przy benchmarku
BENCHMARK_PROGRAMS = [
    'HW Monitor',
    '7-Zip',
    'HW Info',
    'Google Chrome',
    'Steam',
    'Epic Games Store',
    'Ubisoft Connect',
    'DirectX 9',
    'Rivatuner',
    'CapFrameX',
    'Windows Terminal',
    'Kopiuj BenchmarkTools na pulpit',
    'Usuń bloatware z Windows',
    'CPU-Z',
    'GPU-Z',
    'Ciemny motyw Windows',
    'Blender',
    'Kopiuj winstaller na pulpit',
    'Odinstaluj OneDrive',
    'LM Studio',
    'UL Procyon',
    'DisplayDriverUninstaller',
] 