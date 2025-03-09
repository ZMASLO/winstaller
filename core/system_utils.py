import os
import sys
import shutil
import ctypes
import winreg
import subprocess

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

def windows_light_mode():
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    value_name = "AppsUseLightTheme"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, value_name)
        winreg.CloseKey(key)
    except Exception as e:
        raise Exception(f"Wystąpił błąd podczas usuwania wartości {value_name} z klucza {key_path}: {e}")

def windows_dark_mode():
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize")
    winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, 0)
    winreg.CloseKey(key)

def reboot_to_bios():
    subprocess.run(["shutdown.exe", "-t", "1", "-r", "-fw"])

def reboot_to_advanced_startup():
    subprocess.run(["shutdown.exe", "-t", "1", "-r", "-o"])

def generate_battery_report():
    result = subprocess.run(["powercfg", "-batteryreport"])
    print(result)

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

def find_exe(dir):
    for file in os.listdir(dir):
        if file.endswith(".exe"):
            return file
    return None

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
        "microsoft.windowscommunicationsapps",
        "Microsoft.WindowsFeedbackHub",
        "Microsoft.WindowsMaps",
        "Microsoft.WindowsPhone",
        "Microsoft.WindowsSoundRecorder",
        "Microsoft.ConnectivityStore",
        "Microsoft.CommsPhone",
        "Microsoft.ZuneMusic",
        "Microsoft.ZuneVideo",
        "Microsoft.YourPhone",
        "Microsoft.Getstarted",
        "Microsoft.MicrosoftOfficeHub",

        #Sponsored Windows 10 AppX Apps
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
        "*Microsoft.Advertising.Xaml*",
    ]
    for bloat in bloatware:
        subprocess.run(["powershell.exe", "-Command", "Get-AppxPackage *"+bloat+"* | Remove-AppxPackage"]) 