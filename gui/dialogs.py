import customtkinter as ctk

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

class ModernConfirmDialog(ctk.CTkToplevel):
    def __init__(self, parent, message, on_yes):
        super().__init__(parent)
        
        # Konfiguracja okna
        self.title("Potwierdzenie")
        
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

def show_message(parent, message):
    dialog = ModernDialog(parent, message)
    dialog.wait_window() 