import os
import sys
import ctypes
import threading
import customtkinter as ctk
from gui.dialogs import ModernConfirmDialog, show_message
from core.config import APP_CONFIG, CATEGORIES, CHECKBOX_FUNCTIONS, BENCHMARK_PROGRAMS
from core.system_utils import reboot_to_bios, reboot_to_advanced_startup, generate_battery_report
from core.updater import check_for_updates
from core.version import get_version_info

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
        self.title(APP_CONFIG["title"])
        self.geometry(APP_CONFIG["window_size"])
        
        # Ustawienie przezroczystości okna
        self.attributes('-alpha', APP_CONFIG["window_alpha"])
        
        # Konfiguracja kolorów
        self._set_appearance_mode("system")
        
        # Tworzenie głównego kontenera
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        self._create_sidebar()
        self._create_main_area()
        
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
        print(get_version_info()+" - gotowy do pracy.\n")

    def _create_sidebar(self):
        # Panel boczny
        self.sidebar_frame = ctk.CTkFrame(self, width=APP_CONFIG["sidebar_width"], corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, rowspan=4, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(5, weight=1)
        
        # Logo
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Winstaller", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 5))
        
        # Przyciski w panelu bocznym
        self.install_button = ctk.CTkButton(self.sidebar_frame, text="Uruchom!", command=self.start_installation, width=160)
        self.install_button.grid(row=1, column=0, padx=20, pady=5)
        
        self.stop_button = ctk.CTkButton(self.sidebar_frame, text="Stop", command=self.stop_installation, state="disabled", width=160)
        self.stop_button.grid(row=2, column=0, padx=20, pady=5)
        
        self.benchmark_button = ctk.CTkButton(self.sidebar_frame, text="Benchmark starter", command=self.start_benchmark, width=160)
        self.benchmark_button.grid(row=3, column=0, padx=20, pady=5)
        
        self.uncheck_button = ctk.CTkButton(self.sidebar_frame, text="Odznacz wszystkie", command=self.uncheck_all_checkboxes, width=160)
        self.uncheck_button.grid(row=4, column=0, padx=20, pady=5)
        
        # Separator i etykieta "Specjalne"
        self.separator = ctk.CTkFrame(self.sidebar_frame, height=1)
        self.separator.grid(row=5, column=0, padx=20, pady=5, sticky="ew")
        
        self.special_label = ctk.CTkLabel(self.sidebar_frame, text="Specjalne:", font=ctk.CTkFont(size=14))
        self.special_label.grid(row=6, column=0, padx=20, pady=(0, 5))
        
        # Przyciski w sekcji specjalne
        self.bios_button = ctk.CTkButton(
            self.sidebar_frame,
            text="Restart do BIOS",
            command=lambda: self.confirm_restart(
                "Czy na pewno chcesz uruchomić ponownie komputer i wejść do BIOS-u?",
                reboot_to_bios
            ),
            width=160
        )
        self.bios_button.grid(row=7, column=0, padx=20, pady=5)
        
        self.advanced_boot_button = ctk.CTkButton(
            self.sidebar_frame,
            text="Restart zaawansowany",
            command=lambda: self.confirm_restart(
                "Czy na pewno chcesz uruchomić ponownie komputer w trybie zaawansowanym?",
                reboot_to_advanced_startup
            ),
            width=160
        )
        self.advanced_boot_button.grid(row=8, column=0, padx=20, pady=5)
        
        self.battery_button = ctk.CTkButton(self.sidebar_frame, text="Raport baterii", command=generate_battery_report, width=160)
        self.battery_button.grid(row=9, column=0, padx=20, pady=5)

        self.update_button = ctk.CTkButton(
            self.sidebar_frame,
            text="Sprawdź aktualizacje",
            command=lambda: check_for_updates(self),
            width=160
        )
        self.update_button.grid(row=10, column=0, padx=20, pady=5)
        
        # Pusty wiersz z wagą 1 (elastyczny odstęp)
        self.sidebar_frame.grid_rowconfigure(11, weight=1)
        
        # Etykiety i pasek postępu
        self.task_label = ctk.CTkLabel(self.sidebar_frame, text="Postęp zadań:")
        self.task_label.grid(row=12, column=0, padx=20, pady=(20, 0))
        
        self.current_task_label = ctk.CTkLabel(self.sidebar_frame, text="Brak zadań.")
        self.current_task_label.grid(row=13, column=0, padx=20, pady=(5, 10))
        
        self.progress_bar = ctk.CTkProgressBar(self.sidebar_frame)
        self.progress_bar.grid(row=14, column=0, padx=20, pady=(0, 10))
        self.progress_bar.set(0)
        
        # Przycisk logów
        self.log_button = ctk.CTkButton(self.sidebar_frame, text="Pokaż logi", command=self.log_toggle, width=160)
        self.log_button.grid(row=15, column=0, padx=20, pady=(0, 20))

    def _create_main_area(self):
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

    def create_category_label(self, name):
        label = ctk.CTkLabel(
            self.scrollable_frame,
            text=name,
            font=ctk.CTkFont(size=14, weight="bold")
        )
        label.pack(pady=(15, 5), padx=10, anchor="w")

    def create_checkbox(self, name):
        var = ctk.BooleanVar()
        checkbox = ctk.CTkCheckBox(self.scrollable_frame, text=name, variable=var)
        checkbox.pack(pady=2, padx=25, anchor="w")  # Zwiększony padding z lewej dla hierarchii
        checkbox_data = {"var": var, "checkbox": checkbox}
        self.checkboxes.append(checkbox_data)

    def count_checkboxes_checked(self):
        return sum(1 for checkbox in self.checkboxes if checkbox["var"].get())

    def uncheck_all_checkboxes(self):
        for checkbox in self.checkboxes:
            if checkbox["var"].get():
                checkbox['checkbox'].deselect()

    def checkbox_all_set_state(self, state):
        for checkbox in self.checkboxes:
            checkbox['checkbox'].configure(state=state)

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
                        CHECKBOX_FUNCTIONS[checkbox['checkbox'].cget("text")]()
                    except Exception as e:
                        show_message(self, "Problem podczas wykonania "+checkbox['checkbox'].cget("text")+"\n"+str(e))
                        
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
            show_message(self, "Zakończono zadania!")
        
        t = threading.Thread(target=execute_install)
        t.start()

    def stop_installation(self):
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        show_message(self, "Zatrzymuję zadania...")

    def start_benchmark(self):
        for checkbox_name in BENCHMARK_PROGRAMS:
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

    def confirm_restart(self, message, callback):
        dialog = ModernConfirmDialog(self, message, callback)
        dialog.wait_window() 