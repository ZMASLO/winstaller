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
_stop_flag = threading.Event()

def _decode_output(raw):
    for encoding in ['utf-8', 'cp1250', 'cp852', 'iso-8859-2']:
        try:
            return raw.decode(encoding) if raw else ""
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace') if raw else ""

def _strip_ansi_bytes(data):
    result = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0x1b and (i + 1) < len(data):
            if data[i+1] == 0x5b:
                j = i + 2
                while j < len(data) and not(0x40 <= data[j] <= 0x7E):
                    j += 1
                i = j + 1 if j < len(data) else j
                continue
            else:
                i += 2
                continue
        result.append(data[i])
        i += 1
    return bytes(result)

def _read_stream(pipe, stream_obj):
    raw_buffer = b""

    while True:
        try:
            chunk = pipe.read(512)
        except OSError:
            break
        if not chunk:
            break
        raw_buffer += chunk

        raw_buffer = _strip_ansi_bytes(raw_buffer)

        parts = raw_buffer.split(b'\n')
        raw_buffer = parts[-1]

        for part in parts[:-1]:
            text = _decode_output(part)
            cr_parts = text.split('\r')
            if len(cr_parts) == 1:
                stripped = cr_parts[0].strip()
                if stripped:
                    stream_obj.write(stripped + "\n")
            else:
                for i, segment in enumerate(cr_parts):
                    stripped = segment.strip()
                    if not stripped:
                        continue
                    if i < len(cr_parts) - 1:
                        stream_obj.replace_last_line(stripped)
                    else:
                        stream_obj.write(stripped + "\n")

        time.sleep(0.01)

    remaining = _decode_output(raw_buffer).strip()
    if remaining:
        stream_obj.write(remaining + "\n")

def _stream_winget(proc):
    t_stdout = threading.Thread(target=_read_stream, args=(proc.stdout, sys.stdout))
    t_stderr = threading.Thread(target=_read_stream, args=(proc.stderr, sys.stderr))
    t_stdout.start()
    t_stderr.start()

    killed = False
    while proc.poll() is None:
        if _stop_flag.is_set():
            print("\n-- Przerwanie procesu przez uzytkownika...")
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            killed = True
            break
        time.sleep(0.05)

    t_stdout.join(timeout=3)
    t_stderr.join(timeout=3)
    return -1 if killed else proc.returncode

def kill_current_winget():
    _stop_flag.set()

def winget_install(name):
    _stop_flag.clear()
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
    _stop_flag.clear()
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