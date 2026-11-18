"""Versión de inicio para Windows: usa Google Chrome."""
import os

import cobranza_venezuela as app


def buscar_chrome():
    rutas = (
        os.environ.get("CHROME_PATH", ""),
        r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    )
    return next((ruta for ruta in rutas if os.path.isfile(ruta)), None)


if __name__ == "__main__":
    app.EJECUTABLE_CHROMIUM = buscar_chrome()
    if not app.EJECUTABLE_CHROMIUM:
        print("No se encontró Google Chrome. Indica su ruta en .env con CHROME_PATH=C:\\ruta\\chrome.exe")
    else:
        app.ejecutar_scraper()
