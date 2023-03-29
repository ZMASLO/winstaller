import os
import sys
import ctypes
import subprocess
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox

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

checkboxes = []

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

def check_checkbox(name):
    for checkbox in checkboxes:
        if checkbox['checkbox'].cget("text") == name:
            checkbox['checkbox'].select()

def winget_install(name):
    subprocess.run(["winget", "install", "-e" ,"--accept-package-agreements", "--accept-source-agreements" , name])


def show_message(message):
    # Funkcja wyświetlająca okno dialogowe z informacją
    messagebox.showinfo("Informacja", message) 


def start_installation():
    progress_bar["value"] = 0  # resetowanie paska postępu
    
    if count_checkboxes_checked():
        progress_bar_single_task_percentage = 100/count_checkboxes_checked()

    for checkbox in checkboxes:
        if checkbox["var"].get():
            current_task_label["text"] = checkbox['checkbox'].cget("text") #aktualizacja labelki
            current_task_label.update()
            if checkbox['checkbox'].cget("text") == "HW Monitor":
                winget_install("CPUID.HWMonitor")
            if checkbox['checkbox'].cget("text") == "7-Zip":
                winget_install("7zip.7zip")
            progress_bar["value"] = progress_bar["value"] + progress_bar_single_task_percentage
            progress_bar.update()
    
    current_task_label["text"] = "Brak zadań."
    current_task_label.update()
    

def start_benchmark():
    checkboxes_to_check =[
        'HW Monitor',
        '7-Zip'
    ]

    for checkbox_name in checkboxes_to_check:
        check_checkbox(checkbox_name)
        


    print("Rozpoczynam benchmark")


if not is_admin():
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, sys.argv[0], None, 1)
    sys.exit()
# else:
    # print("Skrypt uruchomiony z uprawnieniami administratora.")
    # show_message("Skrypt uruchomiony z uprawnieniami administratora.")

# Sprawdzenie czy jest zainstalowany winget

if not check_winget_installed():
    print("Program Winget nie jest zainstalowany.")
    print("Rozpoczynam instalację...")

    # Pobieranie pliku instalacyjnego
    os.makedirs("winget_installer", exist_ok=True)
    subprocess.run(["curl", "-Lo", "winget_installer/appinstaller.exe", "https://github.com/microsoft/winget-cli/releases/latest/download/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.appxbundle"])

    # Instalowanie programu
    subprocess.run(["powershell", "-Command", "Add-AppxPackage -Path winget_installer\\appinstaller.exe"])

    # Sprawdzanie, czy program został zainstalowany
    if check_winget_installed():
        print("Program Winget został pomyślnie zainstalowany.")
    else:
        print("Błąd podczas instalacji programu Winget.")


# Tworzenie głównego okna
root = tk.Tk()

# Dodanie tytułu do okna
root.title("Winstaller 0.1")

# Ustawienie rozmiaru okna
root.geometry("600x600")

# tworzenie ramki na lewo od okna głównego
left_frame = tk.Frame(root, bg="darkgray", padx=10)
left_frame.pack(side="left", fill="y")

#tworzenie trzech kolumn na checkboxy
frame1 = tk.Frame(root)
frame1.pack(side="left", padx=10)
frame2 = tk.Frame(root)
frame2.pack(side="left", padx=10)
frame3 = tk.Frame(root)
frame3.pack(side="left", padx=10)

# tworzenie przycisku "Rozpocznij instalację!"
install_button = tk.Button(left_frame, text="Instaluj!", command=start_installation, font=("OpenSans", 18))
install_button.pack(pady=50)

# tworzenie przycisku "benchmark starter"
benchmark_button = tk.Button(left_frame, text="benchmark starter", command=start_benchmark, font=("OpenSans", 18))
benchmark_button.pack()

#Etykieta
task_label = tk.Label(left_frame, text="Postęp zadań:", bg="darkgray")
task_label.pack(pady=10)

#Etykieta obecnego zadania
current_task_label = tk.Label(left_frame, text="Brak zadań.", bg="darkgray")
current_task_label.pack(pady=10)

# Dodanie paska postępu
progress_bar = ttk.Progressbar(left_frame, orient="horizontal", length=200, mode="determinate")
progress_bar.pack(pady=10)

# # Dodanie etykiety do okna
# label = tk.Label(root, text="Zaznacz programy, które chcesz zainstalować:")
# label.pack(pady=10)

# Kolumna 1
create_checkbox("Google Chrome",frame1)
create_checkbox("Telegram",frame1)
create_checkbox("Messenger",frame1)
create_checkbox("Discord",frame1)
create_checkbox("TeamSpeak 3",frame1)
create_checkbox("Steam",frame1)
create_checkbox("Epic Games Store",frame1)
create_checkbox("Ubisoft Connect",frame1)
create_checkbox("Battle.net",frame1)
create_checkbox("DirectX 9",frame1)
create_checkbox("Chrome",frame1)

#Kolumna 2 create_checkbox("",frame2)
create_checkbox("Windows Terminal",frame2)
create_checkbox("HW Monitor",frame2)
create_checkbox("HW Info",frame2)
create_checkbox("7-Zip",frame2)
create_checkbox("NVCleanstall",frame2)
create_checkbox("CPU-Z",frame2)
create_checkbox("GPU-Z",frame2)
create_checkbox("Kopiuj pliki na pulpit",frame2)
create_checkbox("Instaluj lokalne oprogramowanie",frame2)


# Uruchomienie pętli zdarzeń okna
root.mainloop()
