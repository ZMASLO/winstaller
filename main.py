import os
import sys
import shutil
import ctypes
import subprocess
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import winreg
import time

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def check_winget_installed():
    try:
        subprocess.check_output(["winget", "-v"])
        return True
    except:
        return False

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
    subprocess.run([os.getcwd()+"\\dx9\\DXSETUP.exe", "/silent"])

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

def copy_benchmark_tools():
    copy_directory_to_desktop("BenchmarkTools")

def copy_winstaller():
    copy_file_to_desktop("winstaller.exe")

def windows_light_mode():
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    value_name = "AppsUseLightTheme"

    try:
        # Otwórz klucz rejestru
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, value_name)
        # Zamknij klucz rejestru
        winreg.CloseKey(key)

    except Exception as e:
        show_message(f"Wystąpił błąd podczas usuwania wartości {value_name} z klucza {key_path}: {e}")

def windows_dark_mode():
    # Ustawianie ścieżki rejestru
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize")
    winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, 0)
    # Zamknij klucz rejestru
    winreg.CloseKey(key)

def uninstall_onedrive():
    key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\OneDrive")
    winreg.SetValueEx(key, "DisableFileSyncNGSC", 0, winreg.REG_DWORD, 1)

    subprocess.run(["taskkill", "/F", "/IM", "OneDrive.exe"])

    time.sleep(2)

    onedrive = os.path.join(os.environ['SYSTEMROOT'], "SysWOW64", "OneDriveSetup.exe")
    if not os.path.exists(onedrive):
        onedrive = os.path.join(os.environ['SYSTEMROOT'], "System32", "OneDriveSetup.exe")

    subprocess.run([onedrive, "/uninstall"], check=True)

    time.sleep(2)

    subprocess.run(["taskkill", "/F", "/IM", "explorer.exe"])

    time.sleep(2)

    shutil.rmtree(os.path.join(os.environ['USERPROFILE'], "OneDrive"), ignore_errors=True)
    shutil.rmtree(os.path.join(os.environ['LOCALAPPDATA'], "Microsoft", "OneDrive"), ignore_errors=True)
    shutil.rmtree(os.path.join(os.environ['PROGRAMDATA'], "Microsoft OneDrive"), ignore_errors=True)
    shutil.rmtree(os.path.join(os.environ['SYSTEMDRIVE'], "OneDriveTemp"), ignore_errors=True)

    try:
        winreg.DeleteKey(winreg.HKEY_CLASSES_ROOT, r"CLSID\{018D5C66-4533-4307-9B53-224DE2ED1FE6}")
    except OSError:
        pass

    try:
        winreg.DeleteKey(winreg.HKEY_CLASSES_ROOT, r"Wow6432Node\CLSID\{018D5C66-4533-4307-9B53-224DE2ED1FE6}")
    except OSError:
        pass
    
    winreg.CloseKey(key)
    subprocess.run(["explorer.exe"])

def install_onedrive():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\OneDrive", 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, "DisableFileSyncNGSC")
    except: 
        pass

    os.system(os.path.join(os.environ["systemroot"], "SysWOW64", "OneDriveSetup.exe"))

def reboot_to_bios():
    subprocess.run(["shutdown.exe", "-t", "0", "-r", "-fw"])

def reboot_to_advanced_startup():
    subprocess.run(["shutdown.exe", "-t", "0", "-r", "-o"])

def install_blender():
    winget_install("BlenderFoundation.Blender")

def test_box():
    print("TEST")

def remove_bloat():
    bloatware = [
        #Unnecessary Windows 10 AppX Apps
        "Microsoft.3DBuilder",
        "Microsoft.Microsoft3DViewer",
        "Microsoft.AppConnector",
        "Microsoft.BingFinance",
        "Microsoft.BingNews",
        "Microsoft.BingSports",
        "Microsoft.BingTranslator",
        "Microsoft.BingWeather",
        "Microsoft.BingFoodAndDrink",
        "Microsoft.BingHealthAndFitness",
        "Microsoft.BingTravel",
        "Microsoft.MinecraftUWP",
        # "Microsoft.GamingServices",
        # "Microsoft.WindowsReadingList",
        "Microsoft.GetHelp",
        "Microsoft.Getstarted",
        "Microsoft.Messaging",
        "Microsoft.Microsoft3DViewer",
        "Microsoft.MicrosoftSolitaireCollection",
        "Microsoft.NetworkSpeedTest",
        "Microsoft.News",
        "Microsoft.Office.Lens",
        "Microsoft.Office.Sway",
        "Microsoft.Office.OneNote",
        "Microsoft.OneConnect",
        "Microsoft.People",
        "Microsoft.Print3D",
        "Microsoft.SkypeApp",
        "Microsoft.Wallet",
        "Microsoft.Whiteboard",
        # "Microsoft.WindowsAlarms",
        "microsoft.windowscommunicationsapps",
        "Microsoft.WindowsFeedbackHub",
        "Microsoft.WindowsMaps",
        "Microsoft.WindowsPhone",
        "Microsoft.WindowsSoundRecorder",
        "Microsoft.ConnectivityStore",
        "Microsoft.CommsPhone",
        # "Microsoft.ScreenSketch",
        "Microsoft.ZuneMusic",
        "Microsoft.ZuneVideo",
        "Microsoft.YourPhone",
        "Microsoft.Getstarted",
        "Microsoft.MicrosoftOfficeHub",

        #Sponsored Windows 10 AppX Apps
        #Add sponsored/featured apps to remove in the "*AppName*" format
        "*EclipseManager*",
        "*ActiproSoftwareLLC*",
        "*AdobeSystemsIncorporated.AdobePhotoshopExpress*",
        "*Duolingo-LearnLanguagesforFree*",
        "*PandoraMediaInc*",
        "*CandyCrush*",
        "*BubbleWitch3Saga*",
        "*Wunderlist*",
        "*Flipboard*",
        "*Twitter*",
        "*Facebook*",
        "*Royal Revolt*",
        "*Sway*",
        "*Speed Test*",
        "*Dolby*",
        "*Viber*",
        "*ACGMediaPlayer*",
        "*Netflix*",
        "*OneCalendar*",
        "*LinkedInforWindows*",
        "*HiddenCityMysteryofShadows*",
        "*Hulu*",
        "*HiddenCity*",
        "*AdobePhotoshopExpress*",
        "*HotspotShieldFreeVPN*",
        "*Whatsapp*",
        "*Disney*",
        "*Instagram*",
        "*EPSN*",
        "*Prime Video*",

        #Optional: Typically not removed but you can if you need to for some reason
        "*Microsoft.Advertising.Xaml*",
        #"*Microsoft.MSPaint*",
        #"*Microsoft.MicrosoftStickyNotes*",
        #"*Microsoft.Windows.Photos*",
        #"*Microsoft.WindowsCalculator*",
        #"*Microsoft.WindowsStore*",
    ]
    for bloat in bloatware:
        subprocess.run(["powershell.exe", "-Command", "Get-AppxPackage *"+bloat+"* | Remove-AppxPackage"])

checkboxes = []
checkbox_function = {
    "Google Chrome": install_google_chrome,
    "Telegram": install_telegram,
    "Messenger": install_messenger,
    "Discord": install_discord,
    "TeamSpeak3": install_ts3,
    "Steam": install_steam,
    "Epic Games Store": install_epic_games_store,
    "Ubisoft Connect": install_ubisoft_connect,
    "EA Desktop": install_ea_desktop,
    "Battle.net": install_battle_net,
    "Windows Terminal": install_windows_terminal,
    "DirectX 9": install_directx9,
    "HW Monitor": install_hw_monitor,
    "HW Info": install_hw_info,
    "7-Zip": install_7zip,
    "NVCleanstall": install_nvcleanstall,
    "CPU-Z": install_cpuz,
    "GPU-Z": install_gpuz,
    "Blender": install_blender,
    "Davinci Resolve": install_7zip,
    "Creative Cloud": install_creativecloud,
    "DisplayCal": install_displaycal,
    "MSI Afterburner": install_msi_afterburner,
    "Kopiuj BenchmarkTools na pulpit": copy_benchmark_tools,
    "Kopiuj winstaller na pulpit": copy_winstaller,
    "Instaluj lokalne oprogramowanie": install_local_software,
    "Usuń bloatware z Windows": remove_bloat,
    "Ciemny motyw Windows": windows_dark_mode,
    "Jasny motyw Windows": windows_light_mode,
    "Odinstaluj OneDrive": uninstall_onedrive,
    "Instaluj OneDrive": install_onedrive,
    "Restart i uruchom BIOS": reboot_to_bios,
    "Restart i zaawansowane uruchamianie": reboot_to_advanced_startup,
    "TEST BOX": test_box,
    
    }
    

def create_checkbox(name, frame):
    var = tk.BooleanVar()
    checkbox= tk.Checkbutton(frame, text=name, variable=var)
    checkbox.pack()
    checkbox_data =  {"var": var, "checkbox": checkbox}
    checkboxes.append(checkbox_data)

def count_checkboxes_checked():
    counter = 0
    for checkbox in checkboxes:
        if checkbox["var"].get():
            counter = counter + 1
    
    return counter

def uncheck_all_checkboxes():
    for checkbox in checkboxes:
        if checkbox["var"].get():
            checkbox['checkbox'].deselect()

def check_checkbox(name):
    for checkbox in checkboxes:
        if checkbox['checkbox'].cget("text") == name:
            checkbox['checkbox'].select()

def copy_file_to_desktop(file_name):
    user_home = os.path.expanduser("~")
    dest_path = os.path.join(user_home, "Desktop")
    shutil.copy(file_name, dest_path)

def copy_directory_to_desktop(file_name):
    user_home = os.path.expanduser("~")
    dest_path = os.path.join(user_home, "Desktop", file_name)
    shutil.copytree(file_name, dest_path)
    
def winget_install(name):
    subprocess.run(["winget", "install", "-e", "--silent" ,"--accept-package-agreements", "--accept-source-agreements" , name])


def show_message(message):
    # Funkcja wyświetlająca okno dialogowe z informacją
    messagebox.showinfo("Informacja", message) 


def start_installation():
    progress_bar["value"] = 0  # resetowanie paska postępu
    
    if count_checkboxes_checked():
        progress_bar_single_task_percentage = 100/count_checkboxes_checked()

    for checkbox in checkboxes:
        #jeśli checkbox jest zaznaczony
        if checkbox["var"].get():
            current_task_label["text"] = checkbox['checkbox'].cget("text") #aktualizacja labelki
            current_task_label.update()
            #wywołanie funkcji przypisanej w słowniku checkbox_function
            try:
                checkbox_function[checkbox['checkbox'].cget("text")]()
            except Exception as e:
                show_message("Problem podczas instalacji "+checkbox['checkbox'].cget("text")+"\n"+str(e))
            #aktualizacja paska postępu
            progress_bar["value"] = progress_bar["value"] + progress_bar_single_task_percentage
            progress_bar.update()
            checkbox['checkbox'].deselect()
            
    
    current_task_label["text"] = "Brak zadań."
    current_task_label.update()
    show_message("Zakończono instalację!")
    

def start_benchmark():
    checkboxes_to_check =[
        'HW Monitor',
        '7-Zip',
        'HW Info',
        'Google Chrome',
        'Steam',
        'Epic Games Store',
        'Ubisoft Connect',
        'DirectX 9',
        'Windows Terminal',
        'Kopiuj BenchmarkTools na pulpit',
        'Instaluj lokalne oprogramowanie',
        'Usuń bloatware z Windows',
        'CPU-Z',
        'GPU-Z',
        'Ciemny motyw Windows',
        'Blender',
        'Kopiuj winstaller na pulpit',
    ]
    #zaznacza checkboxy określone w tablicy
    for checkbox_name in checkboxes_to_check:
        check_checkbox(checkbox_name)


if not is_admin():
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, sys.argv[0], None, 1)
    sys.exit()
# else:
    # print("Skrypt uruchomiony z uprawnieniami administratora.")
    # show_message("Skrypt uruchomiony z uprawnieniami administratora.")

# Sprawdzenie czy jest zainstalowany winget

if not check_winget_installed():
    show_message("Instalowanie winget!")
    
    # Instalowanie programu
    try:
        subprocess.run(["powershell", "-Command", "Add-AppxPackage https://github.com/microsoft/winget-cli/releases/latest/download/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle"])
    except Exception as e:
        show_message("Problem podczas instalacji winget "+str(e))



# Tworzenie głównego okna
root = tk.Tk()

# Dodanie tytułu do okna
root.title("Winstaller 0.2.2")

# Ustawienie rozmiaru okna
root.geometry("700x600")

# tworzenie ramki na lewo od okna głównego
left_frame = tk.Frame(root, bg="darkgray", padx=10)
left_frame.pack(side="left", fill="y")

#tworzenie trzech kolumn na checkboxy
frame1 = tk.Frame(root)
frame1.pack(side="left", padx=10)
frame2 = tk.Frame(root)
frame2.pack(side="left", padx=10)
# frame3 = tk.Frame(root)
# frame3.pack(side="left", padx=10)

# tworzenie przycisku "Rozpocznij instalację!"
install_button = tk.Button(left_frame, text="Uruchom!", command=start_installation, font=("OpenSans", 18))
install_button.pack(pady=50)

# tworzenie przycisku "benchmark starter"
benchmark_button = tk.Button(left_frame, text="benchmark starter", command=start_benchmark, font=("OpenSans", 18))
benchmark_button.pack(pady=20)

uncheck_button = tk.Button(left_frame, text="odznacz wszystkie", command=uncheck_all_checkboxes, font=("OpenSans", 12))
uncheck_button.pack()

#Etykieta
task_label = tk.Label(left_frame, text="Postęp zadań:", bg="darkgray")
task_label.pack(pady=10)

#Etykieta obecnego zadania
current_task_label = tk.Label(left_frame, text="Brak zadań.", bg="darkgray")
current_task_label.pack(pady=10)

# Dodanie paska postępu
progress_bar = ttk.Progressbar(left_frame, orient="horizontal", length=200, mode="determinate")
progress_bar.pack(pady=10)

counter = 0
for checkbox in checkbox_function:
    if counter < 15:
        create_checkbox(checkbox, frame1)
    else:
        create_checkbox(checkbox, frame2)
    counter = counter + 1


# Uruchomienie pętli zdarzeń okna
root.mainloop()
