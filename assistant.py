"""Asistente de texto: selecciona texto en cualquier app, pulsa un atajo y Gemini responde."""
import ctypes
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
from tkinter import simpledialog

import keyboard
import pyperclip
import pystray
import requests
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

FROZEN = getattr(sys, "frozen", False)  # True cuando corre como .exe de PyInstaller
BASE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
logging.basicConfig(
    filename=os.path.join(BASE, "assistant.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

for noisy in ("google_genai", "httpx", "urllib3"):  # el SDK registra mucho a nivel INFO
    logging.getLogger(noisy).setLevel(logging.ERROR)

ui = queue.Queue()  # eventos hacia el hilo de la interfaz (tkinter solo vive en el hilo principal)


def load_config():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_key(name, required=True):
    key = os.environ.get(name)
    if key:
        return key
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                var, _, value = line.strip().partition("=")
                if var == name and value:
                    return value.strip().strip('"')
    if required:
        raise SystemExit(f"Falta {name} (variable de entorno o archivo .env)")
    return None


config = load_config()
client = genai.Client(
    api_key=load_key("GEMINI_API_KEY"),
    # sin reintentos internos y con tope de espera: si un modelo está saturado, pasa al siguiente rápido
    http_options=types.HttpOptions(timeout=10000, retry_options=types.HttpRetryOptions(attempts=1)),
)


# ---------- contador de uso diario ----------

USAGE_FILE = os.path.join(BASE, "usage.json")
usage_lock = threading.Lock()


def _load_usage():
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    today = time.strftime("%Y-%m-%d")
    if data.get("date") != today:
        data = {"date": today, "models": {}}
    return data


def record_usage(model):
    """Suma una consulta exitosa al contador del día y devuelve el total de hoy."""
    with usage_lock:
        data = _load_usage()
        data["models"][model] = data["models"].get(model, 0) + 1
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return sum(data["models"].values())


def usage_today():
    with usage_lock:
        return sum(_load_usage()["models"].values())


def capture_selection():
    previous = pyperclip.paste()
    pyperclip.copy("")
    for mod in ("ctrl", "alt", "shift", "windows"):
        keyboard.release(mod)
    time.sleep(0.02)
    keyboard.send("ctrl+c")
    text = ""
    for _ in range(50):
        time.sleep(0.02)
        text = pyperclip.paste()
        if text:
            break
    return text, previous


def paste_text(text):
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")


def ask_groq(model, system_prompt, contents):
    key = load_key("GROQ_API_KEY", required=False)
    if not key:
        raise RuntimeError("Falta GROQ_API_KEY")
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "User-Agent": "asistente-soporte/1.0"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": contents},
            ],
        },
        timeout=10,
    )
    if response.status_code != 200:
        raise RuntimeError(f"{response.status_code} Groq: {response.text[:150]}")
    return response.json()["choices"][0]["message"]["content"].strip()


def ask_model(model, system_prompt, contents):
    """Modelos con prefijo 'groq:' van a Groq; el resto a Gemini."""
    if model.startswith("groq:"):
        return ask_groq(model[5:], system_prompt, contents)
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )
    return (response.text or "").strip()


def ask_gemini(system_prompt, contents):
    """Prueba cada modelo de config['models'] hasta que uno responda."""
    last_error = None
    for model in config["models"]:
        try:
            result = ask_model(model, system_prompt, contents)
            if not result:
                raise RuntimeError("respuesta vacía")
            total = record_usage(model)
            warn_at = config.get("daily_warning", 0)
            if warn_at and total == warn_at:
                ui.put(("toast", f"Llevas {total} consultas hoy; el límite gratis puede estar cerca.", 6000))
            return result
        except Exception as exc:
            logging.warning("Modelo %s falló: %s", model, str(exc)[:200])
            last_error = exc
    raise last_error


def process(mode, text, instruction=None, regenerate_into=None):
    """Llama a Gemini y entrega el resultado (pega directo o abre ventana)."""
    ui.put(("toast", f"{mode['name']}…", 30000))
    contents = text if not instruction else f"Instrucción: {instruction}\n\nTexto:\n{text}"
    try:
        result = ask_gemini(mode["prompt"], contents)
        if not result:
            raise RuntimeError("Gemini devolvió respuesta vacía")
    except Exception as exc:
        logging.exception("Error llamando a Gemini")
        detail = str(exc)
        if "429" in detail or "RESOURCE_EXHAUSTED" in detail:
            message = "Límite gratis de Gemini alcanzado. Espera un minuto o revisa tu cuota."
        elif "503" in detail or "UNAVAILABLE" in detail:
            message = "Gemini saturado. Intenta de nuevo en unos segundos."
        else:
            message = f"Error: {detail[:120]}"
        ui.put(("toast", message, 6000))
        return
    ui.put(("toast", None, 0))
    if regenerate_into is not None:
        ui.put(("update", result))
    elif mode["output"] == "replace":
        paste_text(result)
    else:
        ui.put(("popup", mode, text, instruction, result))


busy = threading.Lock()


def run_mode(mode):
    if not busy.acquire(blocking=False):  # ignora tecla mantenida o doble pulsación
        return
    try:
        _run_mode(mode)
    finally:
        busy.release()


def _run_mode(mode):
    logging.info("Atajo: %s", mode["name"])
    text, previous = capture_selection()
    if not text.strip():
        pyperclip.copy(previous)
        ui.put(("toast", "No hay texto seleccionado", 2500))
        return
    if mode.get("ask"):
        ui.put(("ask", mode, text))
    else:
        process(mode, text)


# ---------- interfaz (hilo principal) ----------

root = tk.Tk()
root.overrideredirect(True)  # sin barra ni icono en la barra de tareas
root.geometry("1x1+0+0")
root.attributes("-alpha", 0)
state = {"toast": None, "popup_text": None}


def show_toast(message, ms):
    if state["toast"] is not None:
        state["toast"].destroy()
        state["toast"] = None
    if not message:
        return
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    tk.Label(win, text=message, bg="#202124", fg="white", padx=14, pady=8, font=("Segoe UI", 10)).pack()
    win.update_idletasks()
    x = win.winfo_screenwidth() - win.winfo_width() - 20
    y = win.winfo_screenheight() - win.winfo_height() - 60
    win.geometry(f"+{x}+{y}")
    win.lift()
    state["toast"] = win
    win.after(ms, lambda: win.destroy() if win.winfo_exists() else None)


def show_popup(mode, original, instruction, result):
    win = tk.Toplevel(root)
    win.title(mode["name"])
    win.attributes("-topmost", True)
    win.geometry("560x340")
    # la barra de botones se empaqueta primero para que el texto nunca la empuje fuera de la ventana
    bar = tk.Frame(win)
    bar.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
    hint = tk.Label(win, text="Ya copiada al portapapeles: pega con Ctrl+V. Ctrl+Enter = copiar y cerrar.", fg="#555", anchor="w")
    hint.pack(side="bottom", fill="x", padx=10)
    box = tk.Text(win, wrap="word", font=("Segoe UI", 11), padx=8, pady=8, undo=True, height=8)
    box.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    box.insert("1.0", result)
    state["popup_text"] = box
    pyperclip.copy(result)  # copia automática: basta con pegar

    def current():
        return box.get("1.0", "end").strip()

    def copy():
        pyperclip.copy(current())
        win.destroy()

    def replace():
        text = current()
        win.destroy()
        root.after(300, lambda: paste_text(text))  # espera a que el foco vuelva a la app anterior

    def regenerate():
        threading.Thread(
            target=process, args=(mode, original, instruction, True), daemon=True
        ).start()

    for label, command in (("Copiar y cerrar", copy), ("Reemplazar", replace), ("Regenerar", regenerate), ("Cerrar", win.destroy)):
        tk.Button(bar, text=label, command=command, width=14).pack(side="left", padx=(0, 6))
    win.bind("<Escape>", lambda _e: win.destroy())
    win.bind("<Control-Return>", lambda _e: copy())
    win.focus_force()
    box.focus_set()


def ask_instruction(mode, text):
    instruction = simpledialog.askstring(mode["name"], "¿Qué hago con el texto seleccionado?", parent=root)
    if instruction and instruction.strip():
        threading.Thread(target=process, args=(mode, text, instruction.strip()), daemon=True).start()


def pump():
    try:
        while True:
            event = ui.get_nowait()
            kind = event[0]
            if kind == "toast":
                show_toast(event[1], event[2])
            elif kind == "popup":
                show_popup(*event[1:])
            elif kind == "ask":
                ask_instruction(*event[1:])
            elif kind == "update":
                box = state["popup_text"]
                if box is not None and box.winfo_exists():
                    box.delete("1.0", "end")
                    box.insert("1.0", event[1])
                    pyperclip.copy(event[1])
            elif kind == "quit":
                state["tray"].stop()
                keyboard.unhook_all()
                root.destroy()
                return
    except queue.Empty:
        pass
    root.after(100, pump)


# ---------- arranque con Windows e icono de bandeja ----------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AsistenteTexto"


def autostart_command():
    if FROZEN:
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{exe}" "{os.path.join(BASE, "assistant.py")}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0] == autostart_command()
    except OSError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def make_tray_icon():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill="#1a73e8")
    try:
        font = ImageFont.truetype("segoeuib.ttf", 40)
    except OSError:
        font = ImageFont.load_default()
    draw.text((32, 33), "A", fill="white", font=font, anchor="mm")
    hotkeys = "  ".join(f"{m['hotkey'].upper()}" for m in config["modes"])

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    menu = pystray.Menu(
        pystray.MenuItem(f"Asistente activo ({hotkeys})", None, enabled=False),
        pystray.MenuItem(lambda item: f"Consultas hoy: {usage_today()}", None, enabled=False),
        pystray.MenuItem("Iniciar con Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.MenuItem("Ver registro", lambda icon, item: os.startfile(os.path.join(BASE, "assistant.log"))),
        pystray.MenuItem("Salir", lambda icon, item: ui.put(("quit",))),
    )
    return pystray.Icon("asistente", image, "Asistente de texto", menu)


def already_running():
    """Mutex de Windows: impide abrir una segunda copia (hotkeys duplicados)."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\AsistenteTextoISP")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main():
    if already_running():
        logging.info("Ya hay una copia corriendo; salgo")
        return
    first_run_marker = os.path.join(BASE, ".autostart_configured")
    if not os.path.exists(first_run_marker):  # solo la primera vez; después manda el menú de la bandeja
        set_autostart(True)
        open(first_run_marker, "w").close()
    tray = make_tray_icon()
    tray.run_detached()
    state["tray"] = tray
    for mode in config["modes"]:
        keyboard.add_hotkey(
            mode["hotkey"],
            lambda m=mode: threading.Thread(target=run_mode, args=(m,), daemon=True).start(),
            suppress=True,  # la app activa no recibe la tecla (evita ayuda de F1, etc.)
        )
    keyboard.add_hotkey(config["quit_hotkey"], lambda: ui.put(("quit",)), suppress=True)
    logging.info("Asistente iniciado")
    ui.put(("toast", "Asistente listo", 2000))
    pump()
    root.mainloop()
    logging.info("Asistente cerrado")


if __name__ == "__main__":
    main()
