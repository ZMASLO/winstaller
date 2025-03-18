import os
import subprocess
import requests
import zipfile
import shutil
import sys

def winget_install(name):
    print(f"Instalowanie {name}...")
    try:
        # Próba z domyślnym kodowaniem systemowym
        result = subprocess.run(
            ["winget", "install", "-e", "--silent", "--accept-package-agreements", "--accept-source-agreements", name],
            capture_output=True,
            encoding=None  # Używamy None zamiast text=True, aby otrzymać bajty
        )
        
        # Próbujemy różne kodowania
        for encoding in ['utf-8', 'cp1250', 'cp852', 'iso-8859-2']:
            try:
                stdout = result.stdout.decode(encoding) if result.stdout else ""
                stderr = result.stderr.decode(encoding) if result.stderr else ""
                break
            except UnicodeDecodeError:
                continue
        else:
            # Jeśli żadne kodowanie nie zadziałało, użyj 'replace' aby zastąpić nieznane znaki
            stdout = result.stdout.decode('utf-8', errors='replace') if result.stdout else ""
            stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ""
        
        print(stdout)
        if stderr:
            print("Błędy:", stderr)
            
    except Exception as e:
        print(f"Wystąpił błąd podczas instalacji {name}: {str(e)}")
        
    print(f"Zakończono instalację {name}\n")

def winget_uninstall(name):
    print(f"Odinstalowywanie {name}...")
    try:
        # Próba z domyślnym kodowaniem systemowym
        result = subprocess.run(
            ["winget", "uninstall", "--silent", name],
            capture_output=True,
            encoding=None  # Używamy None zamiast text=True, aby otrzymać bajty
        )
        
        # Próbujemy różne kodowania
        for encoding in ['utf-8', 'cp1250', 'cp852', 'iso-8859-2']:
            try:
                stdout = result.stdout.decode(encoding) if result.stdout else ""
                stderr = result.stderr.decode(encoding) if result.stderr else ""
                break
            except UnicodeDecodeError:
                continue
        else:
            # Jeśli żadne kodowanie nie zadziałało, użyj 'replace' aby zastąpić nieznane znaki
            stdout = result.stdout.decode('utf-8', errors='replace') if result.stdout else ""
            stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ""
        
        print(stdout)
        if stderr:
            print("Błędy:", stderr)
            
    except Exception as e:
        print(f"Wystąpił błąd podczas odinstalowywania {name}: {str(e)}")
        
    print(f"Zakończono odinstalowanie {name}\n")

def download_install(url, install_parameters):
    print(f"Pobieranie z {url}...")
    install_file = "install.exe"
    response = requests.get(url)
    if response.status_code == 200:
        with open(install_file, 'wb') as file:
            file.write(response.content)
        print("Pobrano plik instalacyjny")

        install = [os.getcwd()+"\\"+install_file] + install_parameters
        
        print("Uruchamianie instalatora...")
        result = subprocess.run(install, capture_output=True, text=True)
        print(result.stdout)
        if result.stderr:
            print("Błędy:", result.stderr)

        os.remove(install_file)
        print("Zakończono instalację\n")
    else:
        raise Exception("Błąd pobierania "+str(response.status_code))

def download_unzip_install(url, install_parameters):
    print(f"Pobieranie z {url}...")
    zip_file = "install.zip"
    response = requests.get(url)
    if response.status_code == 200:
        with open(zip_file, 'wb') as file:
            file.write(response.content)
        print("Pobrano plik ZIP")

        print("Rozpakowywanie...")
        with zipfile.ZipFile("install.zip", 'r') as zip_ref:
            zip_ref.extractall("install_extracted")

        os.remove(zip_file)

        from core.system_utils import find_exe
        install_file = find_exe(os.getcwd()+"\\install_extracted")
        if install_file:
            print(f"Znaleziono plik instalacyjny: {install_file}")
            
            install = [os.getcwd()+"\\install_extracted\\"+install_file] + install_parameters
            
            print("Uruchamianie instalatora...")
            result = subprocess.run(install, capture_output=True, text=True)
            print(result.stdout)
            if result.stderr:
                print("Błędy:", result.stderr)

        shutil.rmtree("install_extracted")
        print("Zakończono instalację\n")
    else:
        raise Exception("Błąd pobierania "+str(response.status_code))

# Funkcje instalacyjne dla poszczególnych programów
def install_google_chrome():
    winget_install("Google.Chrome")

def install_telegram():
    winget_install("9NZTWSQNTD0S")

def install_messenger():
    winget_install("9WZDNCRF0083")

def install_discord():
    winget_install("Discord.Discord")

def install_ts3():
    winget_install("TeamSpeakSystems.TeamSpeakClient")

def install_steam():
    winget_install("Valve.Steam")
    steam_path = r'C:\Program Files (x86)\Steam\Steam.exe'
    subprocess.Popen([steam_path])

def install_epic_games_store():
    winget_install("EpicGames.EpicGamesLauncher")
    epic_path = r'C:\Program Files (x86)\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe'
    subprocess.Popen([epic_path])

def install_ubisoft_connect():
    winget_install("Ubisoft.Connect")

def install_ea_desktop():
    winget_install("ElectronicArts.EADesktop")

def install_battle_net():
    print("todo")

def install_hw_monitor():
    winget_install("CPUID.HWMonitor")

def install_7zip():
    winget_install("7zip.7zip")

def install_windows_terminal():
    winget_install("9N0DX20HK701")

def install_directx9():
    url="https://download.microsoft.com/download/1/7/1/1718CCC4-6315-4D8E-9543-8E28A4E18C4C/dxwebsetup.exe"
    download_install(url, ["/Q"])

def install_rivatuner():
    winget_install("Guru3D.RTSS")

def install_capframex():
    url="https://github.com/CXWorld/CapFrameX/releases/download/v1.7.4_release/release_1.7.4_installer.zip"
    download_unzip_install(url, ["/S"])

def install_hw_info():
    winget_install("REALiX.HWiNFO")

def install_nvcleanstall():
    winget_install("TechPowerUp.NVCleanstall")

def install_cpuz():
    winget_install("CPUID.CPU-Z")

def install_gpuz():
    winget_install("TechPowerUp.GPU-Z")

def install_displaycal():
    winget_install("FlorianHoech.DisplayCAL")

def install_msi_afterburner():
    winget_install("Guru3D.Afterburner")    

def install_creativecloud():
    winget_install("XPDLPKWG9SW2WD")

def install_local_software():
    subprocess.run([os.getcwd()+"\\RTSSSetup734.exe", "/S"])
    subprocess.run([os.getcwd()+"\\CapFrameXBootstrapper.exe", "/S"])

def install_davinci_resolve_studio():
    url="https://swr.cloud.blackmagicdesign.com/DaVinciResolve/v18.6.5/DaVinci_Resolve_Studio_18.6.5_Windows.zip?verify=1708895095-TEqnC2EHHPvdHDdozxSY6zGdK39AtvRBeavKupsCxz8%3D"
    download_unzip_install(url, ["/i", "/q", "/noreboot"])    

def install_lm_studio():
    winget_install("ElementLabs.LMStudio")

def install_ul_procyon():
    subprocess.run([os.getcwd()+"\\procyon\\procyon-setup.exe", "/silent"])

def install_blender():
    winget_install("BlenderFoundation.Blender")

def install_displaydriveruninstaller():
    winget_install("Wagnardsoft.DisplayDriverUninstaller")

def install_onedrive():
    winget_install("Microsoft.OneDrive")

def copy_benchmark_tools():
    from core.system_utils import copy_directory_to_desktop
    copy_directory_to_desktop("BenchmarkTools")

def copy_winstaller():
    from core.system_utils import copy_file_to_desktop
    winstaller_name = sys.executable
    copy_file_to_desktop(winstaller_name)

def uninstall_onedrive():
    winget_uninstall("Microsoft.OneDrive") 