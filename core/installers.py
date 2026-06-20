import os
import re
import time
import subprocess
import requests
import zipfile
import shutil
import sys
import threading

_ansi_re = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def _decode_output(raw):
    for encoding in ['utf-8', 'cp1250', 'cp852', 'iso-8859-2']:
        try:
            return raw.decode(encoding) if raw else ""
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace') if raw else ""

def _read_stream(pipe, stream_obj):
    line_buffer = ""
    escape_mode = False
    
    while True:
        chunk = pipe.read(1)
        if not chunk:
            break
        
        byte_val = chunk[0]
        
        if byte_val == 0x1b:
            escape_mode = True
            continue
        
        if escape_mode:
            if 0x40 <= byte_val <= 0x7E or byte_val == 0x3F:
                escape_mode = False
            continue
        
        char = chr(byte_val) if byte_val < 128 else _decode_output(chunk)
        
        if char == '\n':
            clean = _ansi_re.sub('', line_buffer).strip()
            if clean:
                stream_obj.write(clean + "\n")
            line_buffer = ""
        elif char == '\r':
            clean = _ansi_re.sub('', line_buffer).strip()
            if clean:
                stream_obj.replace_last_line(clean)
                time.sleep(0.02)
            line_buffer = ""
        else:
            line_buffer += char
    
    clean = _ansi_re.sub('', line_buffer).strip()
    if clean:
        stream_obj.write(clean + "\n")

def _stream_winget(proc):
    t_stdout = threading.Thread(target=_read_stream, args=(proc.stdout, sys.stdout))
    t_stderr = threading.Thread(target=_read_stream, args=(proc.stderr, sys.stderr))
    t_stdout.start()
    t_stderr.start()
    proc.wait()
    t_stdout.join()
    t_stderr.join()
    return proc.returncode

def winget_install(name):
    print(f"Instalowanie {name}...")
    try:
        proc = subprocess.Popen(
            ["winget", "install", "-e", "--silent", "--accept-package-agreements", "--accept-source-agreements", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        returncode = _stream_winget(proc)
        if returncode != 0:
            print(f"Kod powrotu: {returncode}")
    except Exception as e:
        print(f"Wystąpił błąd podczas instalacji {name}: {str(e)}")

    print(f"\nZakończono instalację {name}\n")

def winget_uninstall(name):
    print(f"Odinstalowywanie {name}...")
    try:
        proc = subprocess.Popen(
            ["winget", "uninstall", "--silent", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        returncode = _stream_winget(proc)
        if returncode != 0:
            print(f"Kod powrotu: {returncode}")
    except Exception as e:
        print(f"Wystąpił błąd podczas odinstalowywania {name}: {str(e)}")
        
    print(f"\nZakończono odinstalowanie {name}\n")

def download_install(url, install_parameters):
    print(f"Pobieranie z {url}...")
    install_file = "install.exe"
    response = requests.get(url)
    if response.status_code == 200:
        with open(install_file, 'wb') as file:
            file.write(response.content)
        print("Pobrano plik instalacyjny")

        install = [os.path.join(os.getcwd(), install_file)] + install_parameters
        
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
        install_file = find_exe(os.path.join(os.getcwd(), "install_extracted"))
        if install_file:
            print(f"Znaleziono plik instalacyjny: {install_file}")

            install = [os.path.join(os.getcwd(), "install_extracted", install_file)] + install_parameters
            
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
    winget_install("Blizzard.BattleNet")

def install_hw_monitor():
    winget_install("CPUID.HWMonitor")

def install_7zip():
    winget_install("7zip.7zip")

def install_windows_terminal():
    winget_install("9N0DX20HK701")

def install_directx9():
    winget_install("Microsoft.DirectX")

def install_rivatuner():
    winget_install("Guru3D.RTSS")

def install_capframex():
    winget_install("CXWorld.CapFrameX")

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

def copy_benchmark_tools_x64():
    from core.system_utils import copy_directory_to_desktop
    copy_directory_to_desktop("BenchmarkTools")

def copy_benchmark_tools_arm():
    from core.system_utils import copy_directory_to_desktop
    copy_directory_to_desktop("BenchmarkToolsARM")

def copy_winstaller():
    from core.system_utils import copy_file_to_desktop
    winstaller_name = sys.executable
    copy_file_to_desktop(winstaller_name)

def uninstall_onedrive():
    winget_uninstall("Microsoft.OneDrive") 