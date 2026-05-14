"""
Psif Identification Module

Changelog / Modifications:
- 2025-2026 update: Added periodic background polling of Psif value using 'g mpsif'
  command (similar to GET polling in amc_interface.py).
  Displays live value in a read-only Entry widget.
  Implemented using _start_get_loop / _get_loop / _stop_get_loop pattern.
  Polling runs only while this window is open and serial is connected.

Author: DAGBAGI Mohamed 
"""

import tkinter as tk
from tkinter import ttk, messagebox
import logging
import threading
import time

from protocol import dec_decode

# Configure logging for debugging and runtime status messages
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------- ToolTip Class ----------
# Reused from electrical_params.py for consistency
class ToolTip:
    """Displays hover tooltips for widgets to enhance user experience."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, event=None):
        self.show_tip()

    def _on_leave(self, event=None):
        self.hide_tip()

    def show_tip(self):
        if self.tip:
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            x = y = 100
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self.tip, text=self.text, bg="#FFF9C4", fg="#222",
                         padx=6, pady=3, relief="solid", borderwidth=1,
                         font=("Segoe UI", 9))
        label.pack()

    def hide_tip(self):
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None

# ---------- PMFluxIdentification Class ----------
class PMFluxIdentification:
    def __init__(self, root, serial_manager):
        """
        Initializes the Psif Identification module window and UI.

        Args:
            root: Tkinter Toplevel window (modal child of main app).
            serial_manager: Shared SerialManager instance for serial access.
        """
        self.root = root
        self.serial_manager = serial_manager
        self.root.title("Psif Identification")
        self.root.geometry("450x420")   # enlarged from original 450x300

        # Polling control flags & thread
        self.get_loop_running = False
        self.get_loop_thread = None

        # Configure grid weights for responsiveness
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Define consistent fonts (matching electrical_params.py) — unchanged
        self.font_large = ("Segoe UI", 10, "bold")
        self.font_regular = ("Segoe UI", 9)
        self.font_small = ("Segoe UI", 8)

        # Configure ttk styles for custom theming — unchanged
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background="#f6f7fb")
        style.configure("Card.TLabelframe", background="#ffffff", relief="groove", borderwidth=1)
        style.configure("TLabel", background="#ffffff", font=self.font_regular)
        style.configure("TButton", font=self.font_regular, padding=2)

        # Create a frame for the UI — increased padding
        main_frame = ttk.Labelframe(self.root, text="Psif Identification", style="Card.TLabelframe", padding="16")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=16, pady=16)
        main_frame.columnconfigure(0, weight=1)

        # Start Identification button — more padding around it
        self.start_btn = ttk.Button(main_frame, text="Start Identification", command=self.on_start_identification)
        self.start_btn.grid(row=0, column=0, pady=20, padx=30, sticky="ew")
        ToolTip(self.start_btn, "Start the Psif identification process")

        # Live Psif display frame — more space
        live_frame = ttk.Labelframe(main_frame, text="Live Psif Value (mWb)", style="Card.TLabelframe", padding="12")
        live_frame.grid(row=1, column=0, sticky="ew", pady=(12, 16))
        live_frame.columnconfigure(0, weight=1)

        self.psif_entry = tk.Entry(live_frame, font=self.font_regular, justify="center",
                                   state="readonly", readonlybackground="#f8fcff", width=20)
        self.psif_entry.grid(row=0, column=0, sticky="ew", padx=30, pady=16)
        self.psif_entry.insert(0, "N/A")
        ToolTip(self.psif_entry, "Current Psif value (updated every ~1s)")

        # Results section frame — more vertical breathing room
        results_frame = ttk.Labelframe(main_frame, text="Last Identified Psif", style="Card.TLabelframe", padding="12")
        results_frame.grid(row=2, column=0, sticky="ew", pady=16)
        results_frame.columnconfigure(0, weight=1)

        self.psif_label = self._make_label(results_frame, "Psif: N/A", self.font_regular)
        self.psif_label.grid(row=0, column=0, sticky=tk.W, padx=20, pady=12)

        # Start periodic GET polling when window opens
        self._start_get_loop()

        # Make sure polling stops when window is closed
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    # ---------- UI Helper Methods ----------
    def _make_label(self, parent, text, font_=None, **kwargs):
        """Creates a styled Tkinter Label with optional font override."""
        if font_ is None:
            font_ = self.font_regular
        return tk.Label(parent, text=text, font=font_, bg="#ffffff", **kwargs)

    def _parse_value(self, raw: str) -> float:
        """Decode 6-char SI protocol string to physical float [Wb]. Raises ValueError on bad input."""
        return dec_decode(raw)
        
    # ---------- Workflow Methods ----------
    def on_start_identification(self):
        """
        Event handler for "Start Identification" button.
        Checks serial connection, validates motor speed (>500 RPM), triggers identification,
        and updates UI with the result.
        """
        if not self.serial_manager.is_open:
            messagebox.showerror("Error", "Not connected to serial port.")
            return

        # Change button text to "Waiting..." and disable it
        self.start_btn.config(text="Waiting...", state="disabled")

        def worker():
            try:
                # Check motor speed
                #speed_response = self.serial_manager.send("g speed", expect_response=True)
                #try:
                #    speed = float(speed_response)
                #except ValueError:
                #    self.root.after(0, lambda: messagebox.showerror("Error", "Invalid speed response from device."))
                #    self.root.after(0, lambda: self.start_btn.config(text="Start Identification", state="normal"))
                #    return

                #if abs(speed) <= 100:
                #    self.root.after(0, lambda: messagebox.showerror("Error", "Motor speed must be > 500 RPM to start identification."))
                #    self.root.after(0, lambda: self.start_btn.config(text="Start Identification", state="normal"))
                #    return

                # Send identification command
                psiid_response = self.serial_manager.send("s psiid", expect_response=True)
                logging.info(f"Psif identification triggered: s psiid → Response: {psiid_response}")

                # Wait 5 seconds for device stabilization
                #time.sleep(5)

                # Read Psif value (returns Wb as SI float)
                raw_psif = self.serial_manager.send("g mpsif", expect_response=True)
                psif_wb  = self._parse_value(raw_psif)          # [Wb]

                # Format for display
                psif_display = f"{psif_wb * 1000.0:.3f} mWb"

                # Update UI on main thread
                def update_ui():
                    self.psif_label.config(text=f"Psif: {psif_display}")
                    logging.info("Psif identification completed.")
                    self.start_btn.config(text="Start Identification", state="normal")

                self.root.after(0, update_ui)

            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", f"Identification failed: {e}"))
                self.root.after(0, lambda: self.start_btn.config(text="Start Identification", state="normal"))

        # Run workflow in daemon thread
        threading.Thread(target=worker, daemon=True).start()

    # ---------- Periodic GET Polling (live value) ----------
    def _start_get_loop(self):
        self._stop_get_loop()
        self.get_loop_running = True
        self.get_loop_thread = threading.Thread(target=self._get_loop, daemon=True)
        self.get_loop_thread.start()

    def _stop_get_loop(self):
        if self.get_loop_thread and self.get_loop_thread.is_alive():
            self.get_loop_running = False
            self.get_loop_thread.join(timeout=1.2)
        self.get_loop_thread = None

    def _get_loop(self):
        while self.get_loop_running:
            if self.serial_manager.is_open:
                try:
                    #raw = self.serial_manager.send("g speed", expect_response=True)
                    #val = self._parse_value(raw) if raw else None
                    raw = self.serial_manager.send("g mpsif", expect_response=True)
                    psif_wb = self._parse_value(raw) if raw else None
                    display = f"{psif_wb * 1000.0:.3f} mWb" if psif_wb is not None else "Error"

                    def update():
                        self.psif_entry.config(state="normal")
                        self.psif_entry.delete(0, tk.END)
                        self.psif_entry.insert(0, display)
                        self.psif_entry.config(state="readonly")

                    self.root.after(0, update)
                except:
                    self.root.after(0, lambda: [
                        self.psif_entry.config(state="normal"),
                        self.psif_entry.delete(0, tk.END),
                        self.psif_entry.insert(0, "Error"),
                        self.psif_entry.config(state="readonly")
                    ])
            time.sleep(1.0)

    def _on_closing(self):
        self._stop_get_loop()
        try:
            self.root.destroy()
        except Exception:
            pass

# ---------- Standalone Testing Support ----------
if __name__ == "__main__":
    class DummySerial:
        def __init__(self):
            self.is_open = False
        def send(self, command, expect_response=False):
            if expect_response:
                return "0"  # Simulate response for testing
            return None

    root = tk.Tk()
    app = PMFluxIdentification(root, DummySerial())
    root.mainloop()