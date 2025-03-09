import os
import sys
import shutil
import ctypes
import subprocess
import customtkinter as ctk
# import darkdetect
import winreg
# import time
import threading
import ctypes
import requests
import zipfile

# Konfiguracja motywu
ctk.set_appearance_mode("system")  # Automatyczne dostosowanie do motywu systemu
ctk.set_default_color_theme("blue")  # Motyw kolorystyczny Windows 11

class ModernApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        # Ukrycie konsoli systemowej
        kernel32 = ctypes.WinDLL('kernel32')
        user32 = ctypes.WinDLL('user32')
        self.hwnd = kernel32.GetConsoleWindow()
        if self.hwnd != 0:
            user32.ShowWindow(self.hwnd, 0)
            
        # Podstawowa konfiguracja okna
        self.title("Winstaller 1.0.0")
        self.geometry("800x750")  # Zwiększamy wysokość okna
        
        # Ustawienie przezroczystości okna (wartość od 0.0 do 1.0)
        self.attributes('-alpha', 0.98)
        
        # Konfiguracja kolorów
        self._set_appearance_mode("system")
        
        # Tworzenie głównego kontenera
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # Panel boczny
        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, rowspan=4, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(4, weight=1)
        
        # Logo
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Winstaller", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))
        
        # Przyciski w panelu bocznym
        self.install_button = ctk.CTkButton(self.sidebar_frame, text="Uruchom!", command=self.start_installation)
        self.install_button.grid(row=1, column=0, padx=20, pady=10)
        
        self.stop_button = ctk.CTkButton(self.sidebar_frame, text="Stop", command=self.stop_installation, state="disabled")
        self.stop_button.grid(row=2, column=0, padx=20, pady=10)
        
        self.benchmark_button = ctk.CTkButton(self.sidebar_frame, text="Benchmark starter", command=self.start_benchmark)
        self.benchmark_button.grid(row=3, column=0, padx=20, pady=10)
        
        self.uncheck_button = ctk.CTkButton(self.sidebar_frame, text="Odznacz wszystkie", command=self.uncheck_all_checkboxes)
        self.uncheck_button.grid(row=4, column=0, padx=20, pady=10)
        
        self.log_button = ctk.CTkButton(self.sidebar_frame, text="Pokaż logi", command=self.log_toggle)
        self.log_button.grid(row=5, column=0, padx=20, pady=10)
        
        # Etykiety i pasek postępu
        self.task_label = ctk.CTkLabel(self.sidebar_frame, text="Postęp zadań:")
        self.task_label.grid(row=6, column=0, padx=20, pady=(20, 0))
        
        self.current_task_label = ctk.CTkLabel(self.sidebar_frame, text="Brak zadań.")
        self.current_task_label.grid(row=7, column=0, padx=20, pady=(5, 10))
        
        self.progress_bar = ctk.CTkProgressBar(self.sidebar_frame)
        self.progress_bar.grid(row=8, column=0, padx=20, pady=(0, 20))
        self.progress_bar.set(0)
        
        # Główny kontener na checkboxy
        self.main_container = ctk.CTkFrame(self)
        self.main_container.grid(row=0, column=1, sticky="nsew")
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(0, weight=1)
        
        # Jeden kontener na checkboxy
        self.scrollable_frame = ctk.CTkScrollableFrame(self.main_container, width=500)
        self.scrollable_frame.grid(row=0, column=0, padx=20, pady=20, sticky="nsew")
        
        # Terminal frame
        self.terminal_visible = False
        self.terminal_frame = ctk.CTkFrame(self.main_container, height=200)
        self.terminal_frame.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="ew")
        self.terminal_frame.grid_remove()  # Ukrywamy na start
        
        # Terminal output
        self.terminal_output = ctk.CTkTextbox(self.terminal_frame, height=150)
        self.terminal_output.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        self.terminal_frame.grid_columnconfigure(0, weight=1)
        
        # Lista checkboxów
        self.checkboxes = []
        
        # Event do zatrzymywania instalacji
        self.stop_event = threading.Event()
        
        # Przekierowanie stdout i stderr do terminala
        self.stdout_redirector = self.TerminalRedirector(self.terminal_output)
        self.stderr_redirector = self.TerminalRedirector(self.terminal_output)
        sys.stdout = self.stdout_redirector
        sys.stderr = self.stderr_redirector
        
        # Początkowy komunikat w terminalu
        print("Winstaller 1.0.0 - gotowy do pracy\n")

    class TerminalRedirector:
        def __init__(self, text_widget):
            self.text_widget = text_widget
            
        def write(self, str):
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", str)
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")
            
        def flush(self):
            pass

    def create_checkbox(self, name):
        var = ctk.BooleanVar()
        checkbox = ctk.CTkCheckBox(self.scrollable_frame, text=name, variable=var)
        checkbox.pack(pady=2, padx=10, anchor="w")
        checkbox_data = {"var": var, "checkbox": checkbox}
        self.checkboxes.append(checkbox_data)

    def count_checkboxes_checked(self):
        counter = 0
        for checkbox in self.checkboxes:
            if checkbox["var"].get():
                counter = counter + 1
        return counter

    def uncheck_all_checkboxes(self):
        for checkbox in self.checkboxes:
            if checkbox["var"].get():
                checkbox['checkbox'].deselect()

    def checkbox_all_set_state(self, state):
        for checkbox in self.checkboxes:
            checkbox['checkbox']['state'] = state

    def check_checkbox(self, name):
        for checkbox in self.checkboxes:
            if checkbox['checkbox'].cget("text") == name:
                checkbox['checkbox'].select()

    def start_installation(self):
        self.progress_bar.set(0)  # resetowanie paska postępu
        self.stop_button.configure(state="normal")

        if self.count_checkboxes_checked():
            progress_bar_single_task_percentage = 100/self.count_checkboxes_checked()

        def execute_install():
            #zablokowanie checboxów na czas instalacji
            self.checkbox_all_set_state("disabled")
            for checkbox in self.checkboxes:
                # stop install event
                if self.stop_event.is_set():
                    self.stop_event.clear()
                    break
                #jeśli checkbox jest zaznaczony
                if checkbox["var"].get():
                    self.current_task_label.configure(text=checkbox['checkbox'].cget("text")) #aktualizacja labelki
                    self.current_task_label.update()
                    try:
                        #wywołanie funkcji instalacyjnej przypisanej w słowniku checkbox_function
                        checkbox_function[checkbox['checkbox'].cget("text")]()
                    except Exception as e:
                        show_message("Problem podczas wykonania "+checkbox['checkbox'].cget("text")+"\n"+str(e))
                        
                    #aktualizacja paska postępu
                    self.progress_bar.set(self.progress_bar.get() + progress_bar_single_task_percentage/100)
                    self.progress_bar.update()
                    checkbox['checkbox'].deselect()

            self.progress_bar.set(0)
            self.progress_bar.update()
            self.current_task_label.configure(text="Brak zadań.")
            self.current_task_label.update()
            self.stop_button.configure(state="disabled")
            self.checkbox_all_set_state("normal")
            show_message("Zakończono zadania!")
        
        t = threading.Thread(target=execute_install)
        t.start()

    def stop_installation(self):
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        show_message("Zatrzymuję zadania...")

    def start_benchmark(self):
        checkboxes_to_check = [
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
            'UL Procyon'
        ]
        for checkbox_name in checkboxes_to_check:
            self.check_checkbox(checkbox_name)

    def log_toggle(self):
        if not self.terminal_visible:
            self.terminal_frame.grid()
            self.log_button.configure(text="Ukryj logi")
            self.terminal_visible = True
        else:
            self.terminal_frame.grid_remove()
            self.log_button.configure(text="Pokaż logi")
            self.terminal_visible = False

class ModernDialog(ctk.CTkToplevel):
    def __init__(self, parent, message):
        super().__init__(parent)
        
        # Konfiguracja okna
        self.title("Informacja")
        
        # Wycentrowanie okna względem rodzica
        window_width = 400
        window_height = 150
        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        parent_height = parent.winfo_height()
        x = parent_x + (parent_width - window_width) // 2
        y = parent_y + (parent_height - window_height) // 2
        self.geometry(f"{window_width}x{window_height}+{x}+{y}")
        
        # Ustawienia okna
        self.resizable(False, False)
        self.grab_set()  # Okno modalne
        self.attributes('-alpha', 0.98)
        
        # Kontener na treść
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # Wiadomość
        self.message_label = ctk.CTkLabel(
            self,
            text=message,
            wraplength=350,
            font=("Segoe UI", 12)
        )
        self.message_label.grid(row=0, column=0, padx=20, pady=(20, 10))
        
        # Przycisk OK
        self.ok_button = ctk.CTkButton(
            self,
            text="OK",
            width=100,
            command=self.destroy
        )
        self.ok_button.grid(row=1, column=0, padx=20, pady=(0, 20))
        
        # Fokus na przycisk OK
        self.ok_button.focus()
        
        # Obsługa klawisza Enter i Escape
        self.bind("<Return>", lambda e: self.destroy())
        self.bind("<Escape>", lambda e: self.destroy())

def show_message(message):
    # Funkcja wyświetlająca okno dialogowe z informacją w stylu aplikacji
    dialog = ModernDialog(app, message)
    dialog.wait_window()


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
    # subprocess.run([os.getcwd()+"\\dx9\\DXSETUP.exe", "/silent"])
    url="https://download.microsoft.com/download/1/7/1/1718CCC4-6315-4D8E-9543-8E28A4E18C4C/dxwebsetup.exe"
    download_install(url, ["/Q"])

def install_rivatuner():
    url="https://ftp.nluug.nl/pub/games/PC/guru3d/rtss/[Guru3D.com]-RTSS.zip"
    download_unzip_install(url, ["/S"])

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
    winget_install("LMStudio.LMStudio")

def install_ul_procyon():
    subprocess.run([os.getcwd()+"\\procyon\\procyon-setup.exe", "/silent"])

def copy_benchmark_tools():
    copy_directory_to_desktop("BenchmarkTools")

def copy_winstaller():
    copy_file_to_desktop("winstaller.exe")

def download_install(url, install_parameters):
    print(f"Pobieranie z {url}...")
    install_file = "install.exe"
    response = requests.get(url)
    if response.status_code == 200:
        with open(install_file, 'wb') as file:
            file.write(response.content)
        print("Pobrano plik instalacyjny")

        # array for adding install parameters array
        install = []
        install.insert(0, os.getcwd()+"\\"+install_file)
        for install_parameter in install_parameters:
            install.append(install_parameter)
        
        print("Uruchamianie instalatora...")
        result = subprocess.run(install, capture_output=True, text=True)
        print(result.stdout)
        if result.stderr:
            print("Błędy:", result.stderr)

        # delete after install
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

        #delete downloaded .zip    
        os.remove(zip_file)

        #find install exe in extracted directory
        install_file=find_exe(os.getcwd()+"\\install_extracted")
        if install_file:
            print(f"Znaleziono plik instalacyjny: {install_file}")
            
            # array for adding install parameters array
            install = []
            install.insert(0, os.getcwd()+"\\install_extracted\\"+install_file)
            for install_parameter in install_parameters:
                install.append(install_parameter)
            
            print("Uruchamianie instalatora...")
            result = subprocess.run(install, capture_output=True, text=True)
            print(result.stdout)
            if result.stderr:
                print("Błędy:", result.stderr)

        # delete extracted directory
        shutil.rmtree("install_extracted")
        print("Zakończono instalację\n")
    else:
        raise Exception("Błąd pobierania "+str(response.status_code))

def find_exe(dir):
    for file in os.listdir(dir):
        if file.endswith(".exe"):
            return file
    return None  # Zwróć None, jeśli nie znaleziono pliku .exe w katalogu

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
    subprocess.run(["winget", "install", "-e", "--silent" ,"--accept-package-agreements", "--accept-source-agreements" , "Microsoft.OneDrive"])

def generate_battery_report():
    subprocess.run(["powercfg", "-batteryreport"])

def reboot_to_bios():
    subprocess.run(["shutdown.exe", "-t", "1", "-r", "-fw"])

def reboot_to_advanced_startup():
    subprocess.run(["shutdown.exe", "-t", "1", "-r", "-o"])

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
    "Rivatuner": install_rivatuner,
    "CapFrameX": install_capframex,
    "HW Monitor": install_hw_monitor,
    "HW Info": install_hw_info,
    "7-Zip": install_7zip,
    "NVCleanstall": install_nvcleanstall,
    "CPU-Z": install_cpuz,
    "GPU-Z": install_gpuz,
    "Blender": install_blender,
    "Davinci Resolve Studio": install_davinci_resolve_studio,
    "Creative Cloud": install_creativecloud,
    "DisplayCal": install_displaycal,
    "MSI Afterburner": install_msi_afterburner,
    "LM Studio": install_lm_studio,
    "Ul Procyon": install_ul_procyon,
    "Kopiuj BenchmarkTools na pulpit": copy_benchmark_tools,
    "Kopiuj winstaller na pulpit": copy_winstaller,
    "Instaluj lokalne oprogramowanie": install_local_software,
    "Usuń bloatware z Windows": remove_bloat,
    "Ciemny motyw Windows": windows_dark_mode,
    "Jasny motyw Windows": windows_light_mode,
    "Odinstaluj OneDrive": uninstall_onedrive,
    "Generuj raport z baterii": generate_battery_report,
    "Restart i uruchom BIOS": reboot_to_bios,
    "Restart i zaawansowane uruchamianie": reboot_to_advanced_startup,
    # "TEST BOX": test_box,
    
    }
    

def copy_file_to_desktop(file_name):
    user_home = os.path.expanduser("~")
    dest_path = os.path.join(user_home, "Desktop")
    shutil.copy(file_name, dest_path)

def copy_directory_to_desktop(dir):
    user_home = os.path.expanduser("~")
    dest_path = os.path.join(user_home, "Desktop", dir)
    if (os.path.exists(dir) and os.path.isdir(dir)):
        shutil.copytree(dir, dest_path)
    else:
        raise Exception("Folder BenchmarkTools nie istnieje!")
    
    
def winget_install(name):
    print(f"Instalowanie {name}...")
    result = subprocess.run(["winget", "install", "-e", "--silent" ,"--accept-package-agreements", "--accept-source-agreements" , name], 
                          capture_output=True, 
                          text=True)
    print(result.stdout)
    if result.stderr:
        print("Błędy:", result.stderr)
    print(f"Zakończono instalację {name}\n")


if __name__ == "__main__":
    if not is_admin():
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, sys.argv[0], None, 1)
        sys.exit()

    app = ModernApp()
    
    if not check_winget_installed():
        show_message("Instalowanie winget!")
        try:
            subprocess.run(["powershell", "-Command", "Add-AppxPackage https://github.com/microsoft/winget-cli/releases/latest/download/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle"])
        except Exception as e:
            show_message("Problem podczas instalacji winget "+str(e))

    # Tworzenie checkboxów
    for checkbox in checkbox_function:
        app.create_checkbox(checkbox)
    
    app.mainloop()
