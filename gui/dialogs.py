import customtkinter as ctk

def _center_on_parent(window, width, height):
    parent = window.master
    x = parent.winfo_x() + (parent.winfo_width() - width) // 2
    y = parent.winfo_y() + (parent.winfo_height() - height) // 2
    window.geometry(f"{width}x{height}+{x}+{y}")

class ModernDialog(ctk.CTkToplevel):
    def __init__(self, parent, message):
        super().__init__(parent)
        
        # Konfiguracja okna
        self.title("Informacja")
        _center_on_parent(self, 400, 150)
        
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

class ModernConfirmDialog(ctk.CTkToplevel):
    def __init__(self, parent, message, on_yes):
        super().__init__(parent)
        
        # Konfiguracja okna
        self.title("Potwierdzenie")
        _center_on_parent(self, 400, 150)
        
        # Ustawienia okna
        self.resizable(False, False)
        self.grab_set()  # Okno modalne
        self.attributes('-alpha', 0.98)
        
        # Kontener na treść
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # Wiadomość
        self.message_label = ctk.CTkLabel(
            self,
            text=message,
            wraplength=350,
            font=("Segoe UI", 12)
        )
        self.message_label.grid(row=0, column=0, columnspan=2, padx=20, pady=(20, 10))
        
        # Przyciski
        self.yes_button = ctk.CTkButton(
            self,
            text="Tak",
            width=100,
            command=lambda: self.on_button_click(on_yes)
        )
        self.yes_button.grid(row=1, column=1, padx=20, pady=(0, 20))
        
        self.no_button = ctk.CTkButton(
            self,
            text="Nie",
            width=100,
            command=self.destroy
        )
        self.no_button.grid(row=1, column=0, padx=20, pady=(0, 20))
        
        # Fokus na przycisk Nie
        self.no_button.focus()
        
        # Obsługa klawiszy
        self.bind("<Return>", lambda e: self.destroy())
        self.bind("<Escape>", lambda e: self.destroy())
        
    def on_button_click(self, callback):
        self.destroy()
        callback()

class ModernUpdateDialog(ctk.CTkToplevel):
    def __init__(self, parent, current_version, new_version, on_yes):
        super().__init__(parent)
        
        # Konfiguracja okna
        self.title("Dostępna aktualizacja")
        _center_on_parent(self, 400, 300)
        
        # Ustawienia okna
        self.resizable(False, False)
        self.grab_set()  # Okno modalne
        self.attributes('-alpha', 0.98)
        
        # Kontener na treść
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        
        # Ikona aktualizacji (emoji)
        self.update_icon = ctk.CTkLabel(
            self,
            text="🔄",
            font=("Segoe UI", 32)
        )
        self.update_icon.grid(row=0, column=0, columnspan=2, pady=(20, 0))
        
        # Wiadomość
        message = f"Dostępna jest nowa wersja programu!\n\n" \
                 f"Obecna wersja: {current_version}\n" \
                 f"Nowa wersja: {new_version}\n\n" \
                 f"Czy chcesz zaktualizować program?"
        
        self.message_label = ctk.CTkLabel(
            self,
            text=message,
            wraplength=350,
            font=("Segoe UI", 12)
        )
        self.message_label.grid(row=1, column=0, columnspan=2, padx=20, pady=(10, 20))
        
        # Przyciski
        self.yes_button = ctk.CTkButton(
            self,
            text="Aktualizuj",
            width=120,
            command=lambda: self.on_button_click(on_yes)
        )
        self.yes_button.grid(row=2, column=1, padx=20, pady=(0, 20))
        
        self.no_button = ctk.CTkButton(
            self,
            text="Później",
            width=120,
            command=self.destroy
        )
        self.no_button.grid(row=2, column=0, padx=20, pady=(0, 20))
        
        # Fokus na przycisk Później
        self.no_button.focus()
        
        # Obsługa klawiszy
        self.bind("<Return>", lambda e: self.on_button_click(on_yes))
        self.bind("<Escape>", lambda e: self.destroy())
        
    def on_button_click(self, callback):
        self.destroy()
        callback()

class ModernProgressDialog(ctk.CTkToplevel):
    def __init__(self, parent, title="Postęp"):
        super().__init__(parent)
        
        # Konfiguracja okna
        self.title(title)
        _center_on_parent(self, 400, 150)
        
        # Ustawienia okna
        self.resizable(False, False)
        self.grab_set()  # Okno modalne
        self.attributes('-alpha', 0.98)
        
        # Kontener na treść
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # Etykieta statusu
        self.status_label = ctk.CTkLabel(
            self,
            text="Przygotowywanie...",
            font=("Segoe UI", 12)
        )
        self.status_label.grid(row=0, column=0, padx=20, pady=(20, 10))
        
        # Pasek postępu
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.grid(row=1, column=0, padx=20, pady=(0, 10), sticky="ew")
        self.progress_bar.set(0)
        
        # Etykieta z procentami
        self.percent_label = ctk.CTkLabel(
            self,
            text="0%",
            font=("Segoe UI", 10)
        )
        self.percent_label.grid(row=2, column=0, padx=20, pady=(0, 20))
    
    def update_progress(self, progress, status=None):
        """Aktualizuje pasek postępu i status."""
        self.progress_bar.set(progress)
        self.percent_label.configure(text=f"{int(progress * 100)}%")
        if status:
            self.status_label.configure(text=status)
        self.update()

def show_message(parent, message):
    dialog = ModernDialog(parent, message)
    dialog.wait_window() 