import os
import re
import smtplib
import imaplib
import email
import time
import unicodedata
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import pandas as pd
from playwright.sync_api import sync_playwright

# =====================================================================
# 1. CONFIGURACIÓN DE PLATAFORMA Y CARPETAS
# =====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIR_CAPTURAS = os.path.join(BASE_DIR, "capturas")
DIR_COSTOS = os.path.join(BASE_DIR, "capturas_costo")
os.makedirs(DIR_CAPTURAS, exist_ok=True)
os.makedirs(DIR_COSTOS, exist_ok=True)

URL_COBRANZA = "https://vemgr.dbaifin.com/#/collection/all/list?page=2&pageSize=20"


def cargar_archivo_env(ruta: str):
    """Carga variables simples de un archivo .env sin dependencias externas."""
    if not os.path.isfile(ruta):
        return
    with open(ruta, encoding="utf-8") as archivo:
        for linea in archivo:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip("\"'"))


cargar_archivo_env(os.path.join(BASE_DIR, ".env"))


def obtener_correo_limpio(texto: str):
    """Extrae un correo eliminando espacios y caracteres invisibles copiados de la tabla."""
    texto = unicodedata.normalize("NFKC", texto or "")
    texto = "".join(caracter for caracter in texto if unicodedata.category(caracter) != "Cf")
    texto = re.sub(r"\s+", "", texto).strip("<>\"'`.,;:")
    coincidencia = re.search(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
        texto,
    )
    correo = coincidencia.group(0).lower() if coincidencia else None
    return correo if correo and "*" not in correo else None

# =====================================================================
# 2. CONFIGURACIÓN DE CORREO Y MENSAJES
# =====================================================================
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_SERVER = "imap.gmail.com"
USAR_IMAP = True  # Activa IMAP en Gmail para consultar hilos existentes.

EMAIL_REMITENTE = os.environ.get("GMAIL_EMAIL", "").strip()
SMTP_LOGIN = os.environ.get("SMTP_LOGIN", EMAIL_REMITENTE).strip()
PASSWORD_APP = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
# Chromium guarda aquí la sesión iniciada de la plataforma.
PERFIL_CHROMIUM = os.path.join(BASE_DIR, "perfil_chromium_cobranza")
EJECUTABLE_CHROMIUM = "/usr/bin/chromium"

# --- TEXTO PARA CORREOS GMAIL ESTÁNDAR (Opciones 1, 2 y 4) ---
ASUNTO_EMAIL_BASE = "Aviso: saldo vencido por más de 14 días"

# --- GENERADORES DINÁMICOS DE MENSAJES PERSONALIZADOS POR CLIENTE ---
def generar_asunto_email(nombre_cliente: str = "Cliente", empresa: str = "Gestión de Cobranzas") -> str:
    """Genera el asunto del correo personalizado con el nombre del cliente si está disponible."""
    empresa = re.sub(r"\s+", " ", empresa or "Gestión de Cobranzas").strip()[:40]
    if nombre_cliente and nombre_cliente != "Cliente":
        return f"{empresa}: Aviso de regularización — {nombre_cliente.upper()}"
    return f"{empresa}: {ASUNTO_EMAIL_BASE}"

def generar_mensaje_email(nombre_cliente: str = "Cliente", empresa: str = "Gestión de Cobranzas") -> str:
    """Genera el aviso de cobro proporcionado por la financiera."""
    saludo = f"Estimado/a {nombre_cliente}," if (nombre_cliente and nombre_cliente != "Cliente") else "Estimado/a cliente,"
    empresa = re.sub(r"\s+", " ", empresa or "Gestión de Cobranzas").strip()[:40]
    return (
        f"{saludo}\n\n"
        f"🚨 NOTIFICACIÓN DE COBRO – ACCIÓN REQUERIDA | {empresa} 🚨\n\n"
        "⚠️ PLAZO DE PAGO VENCIDO ⚠️\n\n"
        "Debido a la falta de respuesta y a los intentos previos de negociación, le informamos que su cuenta ha sido trasladada a una etapa de gestión de cobro interna por el saldo pendiente.\n\n"
        "Para evitar que el caso avance a las siguientes etapas de gestión, le solicitamos regularizar su situación a la mayor brevedad posible.\n\n"
        "🔴 PAGO: Realice el pago pendiente a través de los canales oficiales disponibles.\n\n"
        "🔴 COMPROBANTE: Una vez efectuado el pago, envíe su comprobante para actualizar el estado de su cuenta.\n\n"
        "🔴 NEGOCIACIÓN: Si necesita una alternativa de pago, comuníquese con nosotros para revisar las opciones disponibles.\n\n"
        "⚠️ IMPORTANTE: De no recibirse el pago o una respuesta dentro del plazo indicado, la cuenta podrá continuar con el proceso de gestión correspondiente conforme a las condiciones aplicables.\n\n"
        "👉 Regularice su situación cuanto antes y evite mayores inconvenientes.\n\n"
        f"Atentamente,\n{empresa} — Departamento de Cobranzas"
    )

# =====================================================================
# 2.1. ROTACIÓN DE MENSAJES CORTOS PARA LA PLATAFORMA (MÁX 290 CARACTERES)
# =====================================================================
# La plataforma brmgr.dbaifin.com rechaza textos largos ("Short message length is too long,
# please control within 290 character") y limita a máximo 3 mensajes por día.
# Todos los mensajes son avisos formales y se mantienen por debajo de 285 caracteres.
PLANTILLAS_MENSAJES_CORTOS = [
    "{empresa}: Hola, {nome}. Queremos evitar cualquier interrupción en tu servicio, ya que notamos un saldo vencido desde hace más de 14 días. Revisa el desglose que enviamos a tu correo y apóyanos realizando tu pago hoy.",
    "{empresa}: {nome}, mantener tu cuenta al corriente te evita cargos adicionales. Tienes un pendiente con más de 14 días de atraso; por favor verifica los datos en tu email y liquídalo hoy mismo.",
    "{empresa}: Aviso importante para {nome}. Nos gustaría ayudarte a mantener tu historial impecable, pero registramos un pago vencido hace más de 14 días. Consulta tu correo electrónico y ponte al día hoy.",
    "{empresa}: {nome}, sabemos que a veces se pasan las fechas, pero tu saldo lleva más de 14 días vencido y requiere atención inmediata. Revisa los detalles en tu correo y realiza el abono durante el día de hoy.",
    "{empresa}: {nome}, para seguir brindándote atención sin contratiempos, necesitamos regularizar tu saldo vencido (+14 días). Ya tienes la información completa en tu bandeja de entrada; agradecemos tu pago hoy."
]

def obtener_mensaje_corto_plataforma(indice_ronda: int, nombre_cliente: str = "Cliente", empresa: str = "Gestión de Cobranzas") -> tuple[str, int]:
    """
    Selecciona de forma rotativa un mensaje corto para la plataforma.
    Garantiza estrictamente un tamaño menor a 290 caracteres (máximo 285).
    Retorna (mensaje_formateado, numero_variante_1_a_30).
    """
    partes = [p for p in (nombre_cliente or "").split() if len(p) > 1]
    if partes and partes[0].lower() != "cliente":
        # Formato compacto tipo 'Joao Carlos' o 'Joao C.' para preservar espacio
        if len(partes) > 1:
            nombre_compacto = f"{partes[0].title()} {partes[1][0].upper()}."
        else:
            nombre_compacto = partes[0].title()
    else:
        nombre_compacto = "Cliente"

    num_variantes = len(PLANTILLAS_MENSAJES_CORTOS)
    variante_idx = indice_ronda % num_variantes
    plantilla = PLANTILLAS_MENSAJES_CORTOS[variante_idx]
    
    empresa_compacta = re.sub(r"\s+", " ", empresa or "Gestión de Cobranzas").strip()[:40]
    mensaje = plantilla.format(nome=nombre_compacto, empresa=empresa_compacta)
    
    # Seguro infranqueable de longitud para la plataforma (máx 285 chars)
    if len(mensaje) > 285:
        mensaje = mensaje[:282] + "..."
        
    return mensaje, variante_idx + 1


def enviar_sms_plataforma_con_rotacion(pestana_detalle, indice_ronda: int, nombre_cliente: str, empresa: str):
    """Envía una plantilla autorizada y prueba otras variantes si la plataforma la rechaza."""
    for intento in range(len(PLANTILLAS_MENSAJES_CORTOS)):
        mensaje, variante = obtener_mensaje_corto_plataforma(
            indice_ronda + intento, nombre_cliente, empresa
        )
        print(f"    📱 Variante #{variante:02d}/{len(PLANTILLAS_MENSAJES_CORTOS)} | {len(mensaje)} caracteres")

        boton = pestana_detalle.locator("button").filter(
            has_text=re.compile(r"enviar\s+(mensajes|sms)", re.I)
        ).last
        boton.click(force=True)
        pestana_detalle.wait_for_timeout(400)
        modales = pestana_detalle.locator(".ant-modal-content:visible")
        modales.first.wait_for(state="visible", timeout=10000)
        modal = None
        for indice_modal in range(modales.count()):
            candidato = modales.nth(indice_modal)
            if candidato.locator("textarea#content, textarea").count() > 0:
                modal = candidato
                break
        if modal is None:
            raise RuntimeError("No se abrió el modal de SMS con el campo de mensaje.")
        modal.locator("textarea#content, textarea").first.fill(mensaje)
        pestana_detalle.wait_for_timeout(600)
        modal.locator("button:has-text('Aceptar')").last.click()
        pestana_detalle.wait_for_timeout(800)

        alerta = pestana_detalle.locator(".ant-message-error, .ant-notification-notice-error, .ant-form-explain").first
        motivo = alerta.inner_text().strip() if alerta.is_visible() else ""
        if motivo:
            print(f"    ⚠️ AVISO PLATAFORMA: {motivo}")
            try:
                modal.wait_for(state="hidden", timeout=2000)
            except Exception:
                pestana_detalle.keyboard.press("Escape")
            if re.search(r"similar|similitud", motivo, re.I) and intento + 1 < len(PLANTILLAS_MENSAJES_CORTOS):
                print("    ↪️ Probando la siguiente plantilla autorizada...")
                pestana_detalle.wait_for_timeout(800)
                continue
            return False, None, intento + 1, motivo

        try:
            modal.wait_for(state="hidden", timeout=12000)
        except Exception:
            motivo = "La plataforma no confirmó el envío del SMS."
            pestana_detalle.keyboard.press("Escape")
            return False, None, intento + 1, motivo
        return True, variante, intento + 1, ""

    return False, None, len(PLANTILLAS_MENSAJES_CORTOS), "Todas las plantillas fueron rechazadas."

# =====================================================================
# 3. GESTOR INTELIGENTE DE IMAP (HILOS DE CONVERSACIÓN PERSISTENTES)
# =====================================================================
class GestorHilosIMAP:
    """
    Mantiene una conexión persistente a IMAP para consultar hilos existentes
    sin reconectar ni hacer login en cada iteración (evita bloqueos de Gmail y acelera el envío).
    """
    def __init__(self, servidor=IMAP_SERVER, usuario=EMAIL_REMITENTE, clave=PASSWORD_APP):
        self.servidor = servidor
        self.usuario = usuario
        self.clave = clave
        self.imap = None
        self.carpeta_enviados = None

    def conectar(self):
        if not USAR_IMAP:
            return
        try:
            self.imap = imaplib.IMAP4_SSL(self.servidor, timeout=15)
            self.imap.login(self.usuario, self.clave)
            
            # Detectar automáticamente la carpeta de Enviados
            status, carpetas = self.imap.list()
            self.carpeta_enviados = '"[Gmail]/Enviados"'
            if status == "OK" and carpetas:
                for c in carpetas:
                    c_str = c.decode("latin-1", errors="ignore")
                    if "\\Sent" in c_str:
                        match = re.search(r'"([^"]+)"$', c_str) or re.search(r'(\S+)$', c_str)
                        if match:
                            self.carpeta_enviados = f'"{match.group(1)}"'
                        break
            self.imap.select(self.carpeta_enviados, readonly=True)
            return True
        except Exception as e:
            print(f"  [AVISO IMAP] No se pudo iniciar sesión IMAP ({e}). Los correos se enviarán como hilos nuevos.")
            self.imap = None
            return False

    def obtener_hilo(self, destinatario: str):
        if not self.imap:
            return None, None
        try:
            status, data = self.imap.search(None, f'(TO "{destinatario}")')
            if status != "OK" or not data or not data[0]:
                return None, None
                
            ids = data[0].split()
            if not ids:
                return None, None
            ultimo_id = ids[-1]

            status, msg_data = self.imap.fetch(ultimo_id, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)])")
            if status != "OK" or not msg_data or not msg_data[0]:
                return None, None

            header_text = msg_data[0][1].decode("utf-8", errors="ignore")
            msg_obj = email.message_from_string(header_text)
            return msg_obj.get("Message-ID"), msg_obj.get("Subject")
        except Exception:
            # Si se desconectó la sesión, intentar reconectar una vez
            try:
                self.conectar()
            except Exception:
                pass
            return None, None

    def cerrar(self):
        if self.imap:
            try:
                self.imap.close()
                self.imap.logout()
            except Exception:
                pass
            self.imap = None


# =====================================================================
# 4. FUNCIONES DE ENVÍO DE CORREO SMTP
# =====================================================================
def validar_configuracion_smtp():
    if not EMAIL_REMITENTE or not PASSWORD_APP:
        raise RuntimeError(
            "Configura GMAIL_EMAIL y GMAIL_APP_PASSWORD en el entorno antes de enviar correos."
        )


def crear_conexion_smtp():
    """Crea y autentica una conexión persistente a Gmail SMTP."""
    validar_configuracion_smtp()
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15)
        server.starttls()
        server.login(SMTP_LOGIN, PASSWORD_APP)
        return server
    except Exception as e:
        print(f"  [ERROR SMTP INIT] No se pudo conectar a SMTP: {e}")
        return None


def reconectar_smtp(server) -> bool:
    """Recupera una sesión SMTP persistente que el servidor haya cerrado."""
    validar_configuracion_smtp()
    try:
        server.close()
    except Exception:
        pass
    try:
        server.connect(SMTP_SERVER, SMTP_PORT)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_LOGIN, PASSWORD_APP)
        return True
    except Exception:
        return False


def enviar_correo_smtp(destinatario: str, nombre_cliente: str = "Cliente", server_smtp=None, gestor_imap=None, empresa: str = "Gestión de Cobranzas") -> bool:
    """Envía el correo estándar de texto personalizado respondiendo al mismo hilo si existía."""
    validar_configuracion_smtp()
    msg = MIMEMultipart()
    msg["From"] = EMAIL_REMITENTE
    msg["To"] = destinatario

    asunto_calculado = generar_asunto_email(nombre_cliente, empresa)
    msg_id_previo, asunto_previo = (None, None)
    if gestor_imap:
        msg_id_previo, asunto_previo = gestor_imap.obtener_hilo(destinatario)

    if msg_id_previo:
        msg["In-Reply-To"] = msg_id_previo
        msg["References"] = msg_id_previo
        msg["Subject"] = asunto_previo if asunto_previo else f"Re: {asunto_calculado}"
        print(f"  [HILO] 🔗 Respondiendo dentro del hilo previo de: {destinatario}")
    else:
        msg["Subject"] = asunto_calculado
        print(f"  [NUEVO HILO] ✉️ Creando nuevo hilo para: {destinatario} ({nombre_cliente})")

    mensaje_texto = generar_mensaje_email(nombre_cliente, empresa)
    msg.attach(MIMEText(mensaje_texto, "plain", "utf-8"))

    if server_smtp:
        try:
            server_smtp.send_message(msg)
            print(f"  [SMTP] ✅ Correo enviado exitosamente a: {destinatario}")
            time.sleep(0.8)
            return True
        except Exception as e:
            print(f"  [AVISO SMTP] Fallo en sesión persistente ({e}), reintentando...")
            if reconectar_smtp(server_smtp):
                try:
                    server_smtp.send_message(msg)
                    print(f"  [SMTP] ✅ Correo enviado exitosamente a: {destinatario}")
                    time.sleep(0.8)
                    return True
                except Exception:
                    pass

    for intento in range(2):
        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.starttls()
                server.login(SMTP_LOGIN, PASSWORD_APP)
                server.send_message(msg)
            print(f"  [SMTP] ✅ Correo enviado exitosamente a: {destinatario}")
            time.sleep(0.8)
            return True
        except Exception as e:
            if intento == 0:
                time.sleep(1.2)
            else:
                print(f"  [ERROR SMTP] No se pudo enviar a {destinatario}: {e}")
                return False
    return False


def enviar_correo_con_costo(destinatario: str, nombre_cliente: str, ruta_img_costo: str, server_smtp=None, gestor_imap=None, empresa: str = "Gestión de Cobranzas") -> bool:
    """Envía un correo HTML personalizado con el estado de cuenta adjunto."""
    validar_configuracion_smtp()
    msg = MIMEMultipart("related")
    msg["From"] = EMAIL_REMITENTE
    msg["To"] = destinatario

    empresa = re.sub(r"\s+", " ", empresa or "Gestión de Cobranzas").strip()[:40]
    asunto_personal = f"{empresa}: Información de su cuenta — {nombre_cliente.upper()}"
    msg_id_previo, asunto_previo = (None, None)
    if gestor_imap:
        msg_id_previo, asunto_previo = gestor_imap.obtener_hilo(destinatario)

    if msg_id_previo:
        msg["In-Reply-To"] = msg_id_previo
        msg["References"] = msg_id_previo
        msg["Subject"] = asunto_previo if asunto_previo else f"Re: {asunto_personal}"
        print(f"  [HILO] 🔗 Respondiendo en hilo previo de: {destinatario}")
    else:
        msg["Subject"] = asunto_personal
        print(f"  [NUEVO HILO] ✉️ Nuevo correo personalizado para: {destinatario}")

    cuerpo_html = f"""<!DOCTYPE html>
    <html lang="es-VE">
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; color: #222; background-color: #f7f9fa; margin: 0; padding: 20px; }}
            .container {{ max-width: 620px; margin: 0 auto; background: #ffffff; border-radius: 8px; border: 1px solid #e1e4e8; padding: 25px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }}
            .header {{ border-bottom: 2px solid #d93025; padding-bottom: 12px; margin-bottom: 20px; }}
            .header h2 {{ color: #d93025; margin: 0; font-size: 20px; }}
            .content p {{ font-size: 15px; line-height: 1.6; color: #333333; }}
            .highlight-box {{ margin: 20px 0; border: 1px solid #d0d7de; padding: 15px; border-radius: 6px; background-color: #fdfdfd; text-align: center; }}
            .highlight-box img {{ max-width: 100%; height: auto; display: block; margin: 0 auto; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .alert-text {{ color: #b32400; font-weight: bold; background: #fff4e5; padding: 10px 14px; border-left: 4px solid #b32400; border-radius: 4px; margin: 18px 0; font-size: 14px; }}
            .footer {{ margin-top: 25px; border-top: 1px solid #eee; padding-top: 15px; font-size: 12px; color: #666; line-height: 1.4; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>{empresa}: Información de su cuenta</h2>
            </div>
            <div class="content">
                <p>Hola, <strong>{nombre_cliente}</strong>,</p>
                <p>Compartimos el detalle disponible de su cuenta para su consulta:</p>
                
                <div class="highlight-box">
                    <img src="cid:captura_costo" alt="Detalle de cuenta - {nombre_cliente}" />
                </div>

                <div class="alert-text">
                    Si ya realizó un pago, responda este correo con el comprobante para que podamos actualizar su registro.
                </div>

                <p>Para consultar el saldo, las opciones de pago autorizadas o solicitar atención, responda directamente a este correo. Si ya realizó el pago, puede adjuntar su comprobante.</p>
            </div>
            <div class="footer">
                <p><strong>{empresa} — Departamento de Cobranzas</strong><br>
                Este es un mensaje automático de información de cuenta.</p>
            </div>
        </div>
    </body>
    </html>
    """
    msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))

    # Adjuntar imagen con Content-ID para renderizado inline
    try:
        with open(ruta_img_costo, "rb") as f:
            img = MIMEImage(f.read())
            img.add_header("Content-ID", "<captura_costo>")
            img.add_header("Content-Disposition", "inline", filename=os.path.basename(ruta_img_costo))
            msg.attach(img)
    except Exception as e:
        print(f"  [AVISO] No se pudo adjuntar imagen de costo: {e}")

    # Envío mediante servidor persistente si está disponible
    if server_smtp:
        try:
            server_smtp.send_message(msg)
            print(f"  [SMTP] ✅ Correo con captura de costo enviado a: {destinatario} ({nombre_cliente})")
            time.sleep(0.8)
            return True
        except Exception as e:
            print(f"  [AVISO SMTP] Fallo persistente ({e}), reintentando conexión nueva...")
            if reconectar_smtp(server_smtp):
                try:
                    server_smtp.send_message(msg)
                    print(f"  [SMTP] ✅ Correo con captura de costo enviado a: {destinatario} ({nombre_cliente})")
                    time.sleep(0.8)
                    return True
                except Exception:
                    pass

    for intento in range(2):
        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.starttls()
                server.login(SMTP_LOGIN, PASSWORD_APP)
                server.send_message(msg)
            print(f"  [SMTP] ✅ Correo con captura de costo enviado a: {destinatario} ({nombre_cliente})")
            time.sleep(0.8)
            return True
        except Exception as e:
            if intento == 0:
                time.sleep(1.2)
            else:
                print(f"  [ERROR SMTP] No se pudo enviar correo personalizado a {destinatario}: {e}")
                return False
    return False


# =====================================================================
# 5. MODAL AÑADIR COLECCIÓN (SOPORTE PARA EMAIL Y SMS)
# =====================================================================
def llenar_modal_coleccion(pestana_detalle, tipo_canal="email"):
    """
    Llena el modal de 'Añadir colección' seleccionando dinámicamente según el canal (email o sms).
    Tiempos optimizados de respuesta para evitar demoras innecesarias.
    """
    modal = pestana_detalle.locator(".ant-modal-content:visible").filter(has_text="Añadir colección").first
    modal.wait_for(state="visible", timeout=15000)
    pestana_detalle.wait_for_timeout(800)

    # 1. CANAL DE CONTACTO
    modal.locator("div#reasonId2.ant-select").click()
    pestana_detalle.wait_for_timeout(400)
    if tipo_canal == "sms":
        pestana_detalle.locator(".ant-select-dropdown:visible li").filter(has_text=re.compile(r"mensaje corto|sms", re.I)).first.click()
        print("    -> [1/6] Canal: Mensaje corto (SMS)")
    else:
        pestana_detalle.locator(".ant-select-dropdown:visible li").filter(has_text=re.compile(r"e-?mail|correo", re.I)).first.click()
        print("    -> [1/6] Canal: e-mail")
    pestana_detalle.wait_for_timeout(600)

    # 2. TIPO DE NÚMERO
    modal.locator("div#telephoneType.ant-select").click()
    pestana_detalle.wait_for_timeout(400)
    if tipo_canal == "sms":
        opc_personal = pestana_detalle.locator(".ant-select-dropdown:visible li").filter(has_text=re.compile(r"personal", re.I))
        if opc_personal.count() > 0:
            opc_personal.first.click()
            print("    -> [2/6] Tipo de número: Número personal")
        else:
            pestana_detalle.locator(".ant-select-dropdown:visible li:not(.ant-select-dropdown-menu-item-disabled)").first.click()
            print("    -> [2/6] Tipo de número: Primer teléfono disponible")
    else:
        pestana_detalle.locator(".ant-select-dropdown:visible li:has-text('Número de teléfono móvil')").first.click()
        print("    -> [2/6] Tipo de número: Número de teléfono móvil")
    pestana_detalle.wait_for_timeout(600)

    # 3. NÚMERO TELEFÓNICO
    modal.locator("div#telephoneNo.ant-select").first.click()
    pestana_detalle.wait_for_timeout(400)
    opciones_tel = pestana_detalle.locator(".ant-select-dropdown:visible li:not(.ant-select-dropdown-menu-item-disabled)")
    if opciones_tel.count() > 0:
        opciones_tel.first.click()
    else:
        pestana_detalle.keyboard.press("Escape")
    pestana_detalle.wait_for_timeout(600)
    print("    -> [3/6] Número telefónico cargado")

    # 4. ESTADO DE COMUNICACIÓN
    modal.locator("div#reasonId.ant-select").first.click()
    pestana_detalle.wait_for_timeout(400)
    if tipo_canal == "sms":
        opciones_sms = pestana_detalle.locator(".ant-select-dropdown:visible li").filter(
            has_text=re.compile(r"(sms|mensaje).*(enviado|enviada)", re.I)
        )
        if opciones_sms.count() == 0:
            disponibles = pestana_detalle.locator(".ant-select-dropdown:visible li").all_inner_texts()
            raise RuntimeError(f"No se encontró el estado SMS enviado. Opciones disponibles: {disponibles}")
        estado_sms = opciones_sms.first.inner_text().strip()
        opciones_sms.first.click()
        print(f"    -> [4/6] Estado: {estado_sms}")
    else:
        pestana_detalle.locator(".ant-select-dropdown:visible li").filter(has_text=re.compile(r"correo enviado|e-?mail enviado", re.I)).first.click()
        print("    -> [4/6] Estado: Correo enviado")
    pestana_detalle.wait_for_timeout(600)

    # 5. FECHA DE SEGUIMIENTO (Hoy)
    input_fecha = modal.locator(".ant-calendar-picker-input")
    if not input_fecha.input_value():
        input_fecha.click()
        pestana_detalle.wait_for_timeout(400)
        celda_hoy = pestana_detalle.locator(".ant-calendar-today, .ant-calendar-selected-day").first
        if celda_hoy.is_visible():
            celda_hoy.click()
        else:
            pestana_detalle.keyboard.press("Escape")
        pestana_detalle.wait_for_timeout(400)
    print("    -> [5/6] Fecha de seguimiento: Hoy")

    # 6. NOTAS DE OPERACIÓN
    texto_nota = "SMS enviado" if tipo_canal == "sms" else "Correo enviado"
    modal.locator("#note.ant-input").fill(texto_nota)
    pestana_detalle.wait_for_timeout(400)
    print(f"    -> [6/6] Nota asignada: '{texto_nota}'")

    # 7. CONFIRMAR
    btn_aceptar = modal.locator("button:has-text('Aceptar')").last
    btn_aceptar.click()
    modal.wait_for(state="hidden", timeout=15000)
    pestana_detalle.wait_for_timeout(1000)
    print(f"    -> ✅ Registro de [{tipo_canal.upper()}] guardado con éxito.")


def obtener_cantidad_colecciones(pestana_detalle) -> int:
    """Cuenta cuántas filas existen actualmente en la tabla 'Colección' de la ficha."""
    try:
        tabla_col = pestana_detalle.locator(".ant-table-wrapper").filter(has_text=re.compile(r"Colecci[oó]n|cole[çc][aã]o", re.I)).first
        if not tabla_col.is_visible() or tabla_col.locator(".ant-table-placeholder:visible").count() > 0:
            return 0
        filas = tabla_col.locator("tbody tr:not(.ant-table-placeholder)")
        return filas.count()
    except Exception:
        return 0


def obtener_cantidad_sms_coleccion(pestana_detalle) -> int:
    """Cuenta solo las gestiones registradas como SMS/Mensaje corto."""
    try:
        tabla_col = pestana_detalle.locator(".ant-table-wrapper").filter(has_text=re.compile(r"Colecci[oó]n|cole[çc][aã]o", re.I)).first
        if not tabla_col.is_visible() or tabla_col.locator(".ant-table-placeholder:visible").count() > 0:
            return 0
        filas = tabla_col.locator("tbody tr:not(.ant-table-placeholder)")
        return sum(
            1 for i in range(filas.count())
            if re.search(r"mensaje corto|\bSMS\b", filas.nth(i).inner_text(), re.I)
        )
    except Exception:
        return 0


def obtener_indice_columna_email(pagina_lista) -> int:
    """Obtiene la posición real de la columna Email, aunque el orden cambie."""
    encabezados = pagina_lista.locator("thead th")
    for indice in range(encabezados.count()):
        texto = encabezados.nth(indice).inner_text().strip()
        if re.search(r"e-?mail|correo", texto, re.I):
            return indice
    return 6  # Compatibilidad con la disposición original si no se lee el encabezado.


def obtener_indice_columna_nombre(pagina_lista) -> int:
    """Obtiene la posición real de la columna Nombre del cliente."""
    encabezados = pagina_lista.locator("thead th")
    for indice in range(encabezados.count()):
        texto = encabezados.nth(indice).inner_text().strip()
        if re.search(r"nombre\s+del\s+cliente", texto, re.I):
            return indice
    return 4  # Compatibilidad con la disposición original.


def obtener_indice_columna_empresa(pagina_lista):
    """Obtiene la columna que identifica la financiera que otorgó el préstamo."""
    encabezados = pagina_lista.locator("thead th")
    for indice in range(encabezados.count()):
        texto = encabezados.nth(indice).inner_text().strip()
        if re.search(r"chaleco\s+fuente|empresa.*(?:prest|financ)|financ", texto, re.I):
            return indice
    return None


def obtener_empresa_prestamista(columnas, indice_empresa) -> str:
    """Extrae un nombre breve de la financiera desde la fila actual."""
    if indice_empresa is None or columnas.count() <= indice_empresa:
        return "Gestión de Cobranzas"
    # La tabla puede cortar una palabra por el ancho de la celda (UNITFinan\nce).
    empresa = columnas.nth(indice_empresa).inner_text().replace("\n", "")
    empresa = re.sub(r"[ \t]+", " ", empresa).strip()
    return empresa[:40] or "Gestión de Cobranzas"


def obtener_celda_email(columnas, indice_preferido: int):
    """Encuentra la celda de correo por su contenido, aun con columnas desalineadas."""
    indices = [indice_preferido] + [i for i in range(columnas.count()) if i != indice_preferido]
    for indice in indices:
        celda = columnas.nth(indice)
        if "@" in celda.inner_text():
            return celda
    return columnas.nth(indice_preferido)


# =====================================================================
# 6. NAVEGADOR CHROMIUM CON SESIÓN PERSISTENTE (ARCH LINUX)
# =====================================================================
def abrir_chromium(playwright_instance):
    """
    Abre Chromium con un perfil exclusivo. En el primer uso el usuario inicia
    sesión manualmente; Chromium conserva esa sesión para los siguientes usos.
    """
    try:
        context = playwright_instance.chromium.launch_persistent_context(
            user_data_dir=PERFIL_CHROMIUM,
            executable_path=EJECUTABLE_CHROMIUM,
            headless=False,
            viewport={"width": 1440, "height": 900},
        )
        print("✅ Chromium abierto con el perfil de cobranzas.")
        return context
    except Exception as e:
        print(f"❌ No se pudo abrir Chromium: {e}")
        print("Instálalo en Arch con: sudo pacman -S chromium")
        return None


# =====================================================================
# 7. FLUJO PRINCIPAL CON LAS 6 OPCIONES INTEGRALES
# =====================================================================
def registrar_correos_previos_en_todas_las_paginas(tipo_canal="email"):
    """Registra una gestión ya confirmada, recorriendo toda la paginación."""
    selector_filas = ".ant-table-row, .ant-table-tbody tr:not(.ant-table-placeholder), table tbody tr:not(.ant-table-placeholder)"
    completos, omitidos, fallidos = [], [], []
    etiqueta = "SMS" if tipo_canal == "sms" else "correo"

    with sync_playwright() as p:
        context = abrir_chromium(p)
        if not context:
            return

        pagina_lista = next((pg for pg in context.pages if "dbaifin.com" in pg.url and "detail" not in pg.url), None)
        if not pagina_lista:
            pagina_lista = context.pages[0] if context.pages else context.new_page()
        pagina_lista.bring_to_front()

        if "dbaifin.com" not in pagina_lista.url:
            pagina_lista.goto(URL_COBRANZA, wait_until="domcontentloaded", timeout=20000)

        numero_pagina = 1
        while True:
            try:
                pagina_lista.wait_for_selector(selector_filas, timeout=20000)
            except Exception:
                print("⚠️ No se detectó la tabla. Inicia sesión y deja visible la lista S4; luego presiona Enter.")
                input()
                pagina_lista.wait_for_selector(selector_filas, timeout=20000)

            filas = pagina_lista.locator(selector_filas)
            total_filas = filas.count()
            indice_nombre = obtener_indice_columna_nombre(pagina_lista)
            print(f"\n📄 PÁGINA {numero_pagina}: {total_filas} registros.")

            for i in range(total_filas):
                pestana_detalle = None
                id_app = "sin ID"
                try:
                    fila = pagina_lista.locator(selector_filas).nth(i)
                    columnas = fila.locator("td")
                    if columnas.count() <= indice_nombre:
                        continue

                    match_id = re.search(r"\d+", columnas.nth(0).inner_text().strip())
                    id_app = match_id.group(0) if match_id else None
                    if not id_app:
                        continue

                    lineas_nombre = [linea.strip() for linea in columnas.nth(indice_nombre).inner_text().split("\n") if linea.strip()]
                    nombre_cliente = next((linea.title() for linea in lineas_nombre if not re.search(r"\d{3,}", linea) and len(linea) > 2), "Cliente")
                    print(f"[{id_app}] Revisando {nombre_cliente}...")

                    enlace_id = columnas.nth(0).locator("a").first
                    enlace_id.scroll_into_view_if_needed()
                    with context.expect_page() as nueva_pestana_info:
                        enlace_id.click()
                    pestana_detalle = nueva_pestana_info.value
                    pestana_detalle.wait_for_load_state("domcontentloaded")
                    pestana_detalle.wait_for_timeout(2000)

                    gestiones = obtener_cantidad_colecciones(pestana_detalle)
                    cantidad_canal = obtener_cantidad_sms_coleccion(pestana_detalle) if tipo_canal == "sms" else gestiones
                    limite = 3 if tipo_canal == "sms" else 1
                    if cantidad_canal >= limite:
                        print(f"    ⏭️ Omitido: ya tiene {cantidad_canal} gestión(es) de {etiqueta}.")
                        omitidos.append(id_app)
                    else:
                        print(f"    📝 Registrando {etiqueta} previo en Colección...")
                        boton_coleccion = pestana_detalle.locator("button:has-text('Agregar estado de colección')").first
                        boton_coleccion.scroll_into_view_if_needed()
                        boton_coleccion.click(force=True)
                        llenar_modal_coleccion(pestana_detalle, tipo_canal=tipo_canal)
                        completos.append(id_app)

                    pestana_detalle.close()
                    pagina_lista.bring_to_front()
                    pagina_lista.wait_for_timeout(700)
                except Exception as e:
                    print(f"    ⚠️ Error: {e}")
                    fallidos.append(id_app)
                    try:
                        if pestana_detalle and not pestana_detalle.is_closed():
                            pestana_detalle.close()
                    except Exception:
                        pass
                    pagina_lista.bring_to_front()

            siguiente = pagina_lista.locator(".ant-pagination-next:not(.ant-pagination-disabled)").first
            if siguiente.count() == 0:
                break
            print("➡️ Avanzando a la siguiente página...")
            siguiente.click()
            pagina_lista.wait_for_timeout(1500)
            numero_pagina += 1

    print(f"\n📊 REPORTE — REGISTRO DE {etiqueta.upper()} PREVIOS")
    print(f"✅ Registrados: {len(completos)} | ⏭️ Ya gestionados: {len(omitidos)} | ❌ Fallidos: {len(fallidos)}")


def enviar_y_registrar_en_todas_las_paginas(respetar_limite=True, enviar_mensaje_plataforma=True, solo_sms=False):
    """Envía correo o SMS y registra la gestión, recorriendo toda la paginación."""
    selector_filas = ".ant-table-row, .ant-table-tbody tr:not(.ant-table-placeholder), table tbody tr:not(.ant-table-placeholder)"
    completos, limite, fallidos = [], [], []
    contador_rondas_msg = 0
    server_smtp = None if solo_sms else crear_conexion_smtp()
    gestor_imap = None if solo_sms else GestorHilosIMAP()
    if gestor_imap:
        gestor_imap.conectar()

    try:
        with sync_playwright() as p:
            context = abrir_chromium(p)
            if not context:
                return

            pagina_lista = next((pg for pg in context.pages if "dbaifin.com" in pg.url and "detail" not in pg.url), None)
            if not pagina_lista:
                pagina_lista = context.pages[0] if context.pages else context.new_page()
            pagina_lista.bring_to_front()
            if "dbaifin.com" not in pagina_lista.url:
                pagina_lista.goto(URL_COBRANZA, wait_until="domcontentloaded", timeout=20000)

            numero_pagina = 1
            while True:
                try:
                    pagina_lista.wait_for_selector(selector_filas, timeout=20000)
                except Exception:
                    print("⚠️ No se detectó la tabla. Inicia sesión y deja visible la lista S4; luego presiona Enter.")
                    input()
                    pagina_lista.wait_for_selector(selector_filas, timeout=20000)

                total_filas = pagina_lista.locator(selector_filas).count()
                indice_email = obtener_indice_columna_email(pagina_lista)
                indice_nombre = obtener_indice_columna_nombre(pagina_lista)
                indice_empresa = obtener_indice_columna_empresa(pagina_lista)
                print(f"\n📄 PÁGINA {numero_pagina}: {total_filas} registros.")

                for i in range(total_filas):
                    pestana_detalle = None
                    id_app = "sin ID"
                    try:
                        fila = pagina_lista.locator(selector_filas).nth(i)
                        columnas = fila.locator("td")
                        if columnas.count() <= max(indice_email, indice_nombre):
                            continue

                        match_id = re.search(r"\d+", columnas.nth(0).inner_text().strip())
                        id_app = match_id.group(0) if match_id else None
                        if not id_app:
                            continue

                        lineas_nombre = [linea.strip() for linea in columnas.nth(indice_nombre).inner_text().split("\n") if linea.strip()]
                        nombre_cliente = next((linea.title() for linea in lineas_nombre if not re.search(r"\d{3,}", linea) and len(linea) > 2), "Cliente")
                        empresa_prestamista = obtener_empresa_prestamista(columnas, indice_empresa)
                        celda_email = obtener_celda_email(columnas, indice_email)
                        correo_texto = celda_email.inner_text().strip()
                        if "*" in correo_texto:
                            for _ in range(3):
                                try:
                                    celda_email.locator("a, button, span, i").first.click(timeout=1500, force=True)
                                    pagina_lista.wait_for_timeout(600)
                                    correo_texto = celda_email.inner_text().strip()
                                    if "*" not in correo_texto and "@" in correo_texto:
                                        break
                                except Exception:
                                    pagina_lista.wait_for_timeout(400)
                        correo = obtener_correo_limpio(correo_texto)

                        enlace_id = columnas.nth(0).locator("a").first
                        enlace_id.scroll_into_view_if_needed()
                        with context.expect_page() as nueva_pestana_info:
                            enlace_id.click()
                        pestana_detalle = nueva_pestana_info.value
                        pestana_detalle.wait_for_load_state("domcontentloaded")
                        pestana_detalle.wait_for_timeout(2500)

                        gestiones = obtener_cantidad_colecciones(pestana_detalle)
                        sms_previos = obtener_cantidad_sms_coleccion(pestana_detalle) if solo_sms else 0
                        cantidad_limite = sms_previos if solo_sms else gestiones
                        if respetar_limite and cantidad_limite >= 3:
                            tipo_limite = "SMS" if solo_sms else "gestiones"
                            print(f"[{id_app}] ⏭️ Omitido: ya tiene {cantidad_limite} {tipo_limite}.")
                            limite.append(id_app)
                        elif not solo_sms and not correo:
                            print(f"[{id_app}] ⚠️ Omitido: no se pudo obtener correo. Texto leído: {correo_texto!r}")
                            fallidos.append(id_app)
                        elif not solo_sms and not enviar_correo_smtp(
                            correo, nombre_cliente, server_smtp, gestor_imap, empresa_prestamista
                        ):
                            fallidos.append(id_app)
                        else:
                            if solo_sms:
                                enviado, variante, intentos, motivo = enviar_sms_plataforma_con_rotacion(
                                    pestana_detalle, contador_rondas_msg, nombre_cliente, empresa_prestamista
                                )
                                contador_rondas_msg += intentos
                                if not enviado:
                                    print(f"[{id_app}] ⏭️ SMS no enviado; no se registrará. Motivo: {motivo}")
                                    fallidos.append(id_app)
                                    pestana_detalle.close()
                                    pagina_lista.bring_to_front()
                                    pagina_lista.wait_for_timeout(700)
                                    continue
                            elif enviar_mensaje_plataforma:
                                enviado, variante, intentos, motivo = enviar_sms_plataforma_con_rotacion(
                                    pestana_detalle, contador_rondas_msg, nombre_cliente, empresa_prestamista
                                )
                                contador_rondas_msg += intentos
                                if not enviado:
                                    print(f"[{id_app}] ⚠️ Mensaje de plataforma no confirmado: {motivo}")

                            boton_coleccion = pestana_detalle.locator("button:has-text('Agregar estado de colección')").first
                            boton_coleccion.scroll_into_view_if_needed()
                            boton_coleccion.click(force=True)
                            llenar_modal_coleccion(pestana_detalle, tipo_canal="sms" if solo_sms else "email")
                            completos.append(id_app)

                        pestana_detalle.close()
                        pagina_lista.bring_to_front()
                        pagina_lista.wait_for_timeout(700)
                    except Exception as e:
                        print(f"[{id_app}] ⚠️ Error: {e}")
                        fallidos.append(id_app)
                        try:
                            if pestana_detalle and not pestana_detalle.is_closed():
                                pestana_detalle.close()
                        except Exception:
                            pass
                        pagina_lista.bring_to_front()

                siguiente = pagina_lista.locator(".ant-pagination-next:not(.ant-pagination-disabled)").first
                if siguiente.count() == 0:
                    break
                print("➡️ Avanzando a la siguiente página...")
                siguiente.click()
                pagina_lista.wait_for_timeout(1500)
                numero_pagina += 1
    finally:
        if server_smtp:
            try:
                server_smtp.quit()
            except Exception:
                pass
        if gestor_imap:
            gestor_imap.cerrar()

    print("\n📊 REPORTE — ENVÍO Y REGISTRO")
    print(f"✅ Completados: {len(completos)} | ⏭️ Límite: {len(limite)} | ❌ Fallidos: {len(fallidos)}")


def enviar_smtp_en_todas_las_paginas():
    """Envía solo correos SMTP a todos los registros visibles en cada página."""
    selector_filas = ".ant-table-row, .ant-table-tbody tr:not(.ant-table-placeholder), table tbody tr:not(.ant-table-placeholder)"
    enviados, fallidos = [], []
    server_smtp = crear_conexion_smtp()
    gestor_imap = GestorHilosIMAP()
    gestor_imap.conectar()

    try:
        with sync_playwright() as p:
            context = abrir_chromium(p)
            if not context:
                return
            pagina_lista = next((pg for pg in context.pages if "dbaifin.com" in pg.url and "detail" not in pg.url), None)
            if not pagina_lista:
                pagina_lista = context.pages[0] if context.pages else context.new_page()
            pagina_lista.bring_to_front()
            if "dbaifin.com" not in pagina_lista.url:
                pagina_lista.goto(URL_COBRANZA, wait_until="domcontentloaded", timeout=20000)

            numero_pagina = 1
            while True:
                try:
                    pagina_lista.wait_for_selector(selector_filas, timeout=20000)
                except Exception:
                    print("⚠️ No se detectó la tabla. Inicia sesión y deja visible la lista; luego presiona Enter.")
                    input()
                    pagina_lista.wait_for_selector(selector_filas, timeout=20000)

                filas = pagina_lista.locator(selector_filas)
                total_filas = filas.count()
                indice_email = obtener_indice_columna_email(pagina_lista)
                indice_nombre = obtener_indice_columna_nombre(pagina_lista)
                indice_empresa = obtener_indice_columna_empresa(pagina_lista)
                print(f"\n📄 PÁGINA {numero_pagina}: {total_filas} registros.")

                for i in range(total_filas):
                    try:
                        columnas = pagina_lista.locator(selector_filas).nth(i).locator("td")
                        if columnas.count() <= max(indice_email, indice_nombre):
                            continue
                        match_id = re.search(r"\d+", columnas.nth(0).inner_text().strip())
                        id_app = match_id.group(0) if match_id else "sin ID"
                        lineas_nombre = [linea.strip() for linea in columnas.nth(indice_nombre).inner_text().split("\n") if linea.strip()]
                        nombre_cliente = next((linea.title() for linea in lineas_nombre if not re.search(r"\d{3,}", linea) and len(linea) > 2), "Cliente")
                        empresa_prestamista = obtener_empresa_prestamista(columnas, indice_empresa)
                        celda_email = obtener_celda_email(columnas, indice_email)
                        correo_texto = celda_email.inner_text().strip()

                        if "*" in correo_texto:
                            for _ in range(3):
                                try:
                                    celda_email.locator("a, button, span, i").first.click(timeout=1500, force=True)
                                    pagina_lista.wait_for_timeout(600)
                                    correo_texto = celda_email.inner_text().strip()
                                    if "*" not in correo_texto and "@" in correo_texto:
                                        break
                                except Exception:
                                    pagina_lista.wait_for_timeout(400)

                        correo = obtener_correo_limpio(correo_texto)
                        if not correo:
                            print(f"[{id_app}] ⚠️ Sin correo válido.")
                            fallidos.append(id_app)
                        elif enviar_correo_smtp(
                            correo, nombre_cliente, server_smtp, gestor_imap, empresa_prestamista
                        ):
                            enviados.append(id_app)
                        else:
                            fallidos.append(id_app)
                    except Exception as e:
                        print(f"⚠️ Error en fila {i + 1}: {e}")
                        fallidos.append(f"fila {i + 1}")

                siguiente = pagina_lista.locator(".ant-pagination-next:not(.ant-pagination-disabled)").first
                if siguiente.count() == 0:
                    break
                print("➡️ Avanzando a la siguiente página...")
                siguiente.click()
                pagina_lista.wait_for_timeout(1500)
                numero_pagina += 1
    finally:
        if server_smtp:
            try:
                server_smtp.quit()
            except Exception:
                pass
        gestor_imap.cerrar()

    print("\n📊 REPORTE — SOLO SMTP")
    print(f"✅ Enviados: {len(enviados)} | ❌ Fallidos: {len(fallidos)}")


def exportar_clientes_a_excel():
    """Exporta nombre y correo de todas las páginas a un archivo Excel."""
    selector_filas = ".ant-table-row, .ant-table-tbody tr:not(.ant-table-placeholder), table tbody tr:not(.ant-table-placeholder)"
    max_registros = 60
    registros, sin_correo = [], 0
    ruta_excel = os.path.join(BASE_DIR, f"clientes_nombre_correo_{datetime.now():%Y%m%d_%H%M%S}.xlsx")

    with sync_playwright() as p:
        context = abrir_chromium(p)
        if not context:
            return
        pagina_lista = next((pg for pg in context.pages if "dbaifin.com" in pg.url and "detail" not in pg.url), None)
        pagina_lista = pagina_lista or (context.pages[0] if context.pages else context.new_page())
        pagina_lista.bring_to_front()
        if "dbaifin.com" not in pagina_lista.url:
            pagina_lista.goto(URL_COBRANZA, wait_until="domcontentloaded", timeout=20000)

        numero_pagina = 1
        while True:
            pagina_lista.wait_for_selector(selector_filas, timeout=20000)
            filas = pagina_lista.locator(selector_filas)
            indice_email = obtener_indice_columna_email(pagina_lista)
            indice_nombre = obtener_indice_columna_nombre(pagina_lista)
            print(f"📄 Exportando página {numero_pagina}: {filas.count()} registros.")

            for i in range(filas.count()):
                if len(registros) >= max_registros:
                    break
                try:
                    columnas = filas.nth(i).locator("td")
                    if columnas.count() <= max(indice_email, indice_nombre):
                        continue
                    lineas = [x.strip() for x in columnas.nth(indice_nombre).inner_text().split("\n") if x.strip()]
                    nombre = next((x.title() for x in lineas if not re.search(r"\d{3,}", x) and len(x) > 2), "Cliente")
                    celda_email = obtener_celda_email(columnas, indice_email)
                    texto_email = celda_email.inner_text().strip()
                    if "*" in texto_email:
                        try:
                            celda_email.locator("a, button, span, i").first.click(timeout=1500, force=True)
                            pagina_lista.wait_for_timeout(500)
                            texto_email = celda_email.inner_text().strip()
                        except Exception:
                            pass
                    correo = obtener_correo_limpio(texto_email)
                    if correo:
                        registros.append({"nombre_cliente": nombre, "correo": correo})
                    else:
                        sin_correo += 1
                except Exception as e:
                    print(f"⚠️ No se pudo leer una fila: {e}")

            if len(registros) >= max_registros:
                break
            siguiente = pagina_lista.locator(".ant-pagination-next:not(.ant-pagination-disabled)").first
            if siguiente.count() == 0:
                break
            siguiente.click()
            pagina_lista.wait_for_timeout(1500)
            numero_pagina += 1

    pd.DataFrame(registros, columns=["nombre_cliente", "correo"]).to_excel(
        ruta_excel, index=False, sheet_name="Contactos"
    )
    print(f"\n✅ Excel creado: {ruta_excel}")
    print(f"   Registros exportados: {len(registros)}/{max_registros} | Sin correo disponible: {sin_correo}")


def ejecutar_scraper():
    print("=" * 76)
    print("        🚀 AUTOMATIZACIÓN DE COBRANZA — VENEZUELA         ")
    print("=" * 76)
    print(" 1. Modo Normal (Email Completo):")
    print("    -> Correo Gmail + Mensaje interno + Añadir colección (E-mail).")
    print(" 2. Modo Control de Límite (Email con Máximo 3 gestiones) [RECOMENDADO]:")
    print("    -> Si tiene < 3: Correo Gmail + Mensaje interno + Añadir colección.")
    print(" 3. Modo SMS Plataforma Completo:")
    print("    -> Envía SMS ('enviar mensajes') + Añade colección (SMS). Máx. 3 SMS registrados.")
    print(" 4. Modo Solo Correos Gmail (Ultra Rápido - Sesión SMTP Persistente):")
    print("    -> Solo desenmascara y envía correo Gmail sin tocar la plataforma.")
    print(" 5. Modo Solo SMS Plataforma (Directo a Todos):")
    print("    -> Envía SMS ('enviar mensajes') a todos directamente sin tocar colección.")
    print(" 6. Modo Personalizado con Captura de Costo (Visual HTML):")
    print("    -> Captura la sección de saldo/costo y envía correo con nombre y foto incrustada.")
    print(" 7. Registrar Correos Ya Enviados:")
    print("    -> Solo añade la gestión E-mail a Colección; no envía correos ni mensajes.")
    print(" 8. Email + Registro (sin mensaje de plataforma):")
    print("    -> Envía correo Gmail y registra la gestión en Colección en todas las páginas.")
    print(" 9. Exportar nombres y correos a Excel:")
    print("    -> Solo lee la lista y guarda un archivo Excel; no envía ni registra nada.")
    print("10. Registrar SMS ya enviados:")
    print("    -> Solo añade SMS enviado a Colección; no manda mensajes.")
    print("=" * 76)
    
    opcion = input("Selecciona una opción (1-10) [Por defecto: 2]: ").strip()
    if opcion not in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
        opcion = "2"

    solo_correo = (opcion == "4")
    modo_solo_sms = (opcion == "5")
    modo_personalizado_costo = (opcion == "6")
    modo_registrar_correos = (opcion == "7")
    modo_limite = (opcion in ("2", "7"))

    if modo_registrar_correos:
        registrar_correos_previos_en_todas_las_paginas()
        return
    if opcion == "9":
        exportar_clientes_a_excel()
        return
    if opcion == "10":
        print("⚠️ Este modo no envía SMS: registra solo envíos que ya confirmaste en el historial.")
        registrar_correos_previos_en_todas_las_paginas(tipo_canal="sms")
        return
    if opcion in ("1", "2"):
        enviar_y_registrar_en_todas_las_paginas(respetar_limite=(opcion == "2"))
        return
    if opcion == "3":
        enviar_y_registrar_en_todas_las_paginas(respetar_limite=True, solo_sms=True)
        return
    if opcion == "8":
        enviar_y_registrar_en_todas_las_paginas(respetar_limite=False, enviar_mensaje_plataforma=False)
        return
    if opcion == "4":
        enviar_smtp_en_todas_las_paginas()
        return

    descripciones = {
        "1": "EMAIL COMPLETO (Sin límite de gestiones)",
        "2": "EMAIL CON CONTROL DE LÍMITE (Máximo 3 por cliente)",
        "3": "SMS PLATAFORMA COMPLETO (Envío + Colección SMS)",
        "4": "SOLO CORREOS GMAIL (Ultra Rápido por SMTP)",
        "5": "SOLO SMS DIRECTO (A todos, sin límite ni colección)",
        "6": "CORREO PERSONALIZADO (Detalle de cuenta + nombre del cliente)",
        "7": "REGISTRAR CORREOS YA ENVIADOS (Sin nuevos envíos)",
        "8": "EMAIL + REGISTRO (Sin mensaje de plataforma)"
    }
    print(f"\n🎯 MODO SELECCIONADO: [{opcion}] {descripciones[opcion]}\n")

    with sync_playwright() as p:
        context = abrir_chromium(p)
        if not context:
            return
        
        # Localizar la pestaña de la lista de cobranza
        pagina_lista = next(
            (pg for pg in context.pages if "dbaifin.com" in pg.url and "detail" not in pg.url),
            None
        )
        if not pagina_lista:
            pagina_lista = next((pg for pg in context.pages if "dbaifin.com" in pg.url), None)
            
        if not pagina_lista:
            pagina_lista = context.pages[0] if context.pages else context.new_page()

        pagina_lista.bring_to_front()
        print(f"📍 Pestaña activa detectada: {pagina_lista.url}")

        if "dbaifin.com" not in pagina_lista.url:
            print(f"Navegando a la lista de cobranza: {URL_COBRANZA}")
            try:
                pagina_lista.goto(URL_COBRANZA, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pagina_lista.evaluate(f"window.location.href = '{URL_COBRANZA}'")
                pagina_lista.wait_for_timeout(2000)

        print("Esperando a que cargue la tabla de clientes...")
        selector_filas = ".ant-table-row, .ant-table-tbody tr:not(.ant-table-placeholder), table tbody tr:not(.ant-table-placeholder)"
        try:
            pagina_lista.wait_for_selector(selector_filas, timeout=20000)
        except Exception:
            print("\n⚠️ AVISO: No se detectó la tabla automáticamente.")
            print("Por favor, asegúrate de tener visible en la pantalla la lista de clientes.")
            input("Presiona ENTER en esta terminal una vez que la lista esté cargada...")
            pagina_lista.wait_for_timeout(1000)

        filas = pagina_lista.locator(selector_filas)
        total_filas = filas.count()
        indice_email = obtener_indice_columna_email(pagina_lista)
        indice_nombre = obtener_indice_columna_nombre(pagina_lista)
        indice_empresa = obtener_indice_columna_empresa(pagina_lista)
        print(f"\n✅ Se encontraron {total_filas} registros en esta página. Procesando...")
        print("=" * 76)

        if total_filas == 0:
            print("❌ No se detectaron filas en la tabla. Verifica que estás en la pantalla de cobranzas.")
            return

        clientes_completos = []
        clientes_limite_alcanzado = []
        clientes_ya_gestionados = []
        clientes_fallidos = []
        contador_rondas_msg = 0

        # Inicializar gestores de conexión persistentes si se enviarán correos
        server_persistente = crear_conexion_smtp() if (solo_correo or opcion in ("1", "2", "6")) else None
        gestor_imap = GestorHilosIMAP() if opcion in ("1", "2", "4", "6") else None
        if gestor_imap:
            gestor_imap.conectar()

        try:
            for i in range(total_filas):
                fila = filas.nth(i)
                columnas = fila.locator("td")
                if columnas.count() <= max(indice_email, indice_nombre):
                    continue

                # 1. ID del cliente (Columna 0)
                texto_col0 = columnas.nth(0).inner_text().strip()
                match_id = re.search(r"\d+", texto_col0)
                id_app = match_id.group(0) if match_id else None
                if not id_app:
                    continue

                # 2. Nombre del cliente (columna detectada por encabezado)
                texto_col4 = columnas.nth(indice_nombre).inner_text().strip()
                lineas_col4 = [l.strip() for l in texto_col4.split("\n") if l.strip()]
                nombre_cliente = "Cliente"
                for linea in lineas_col4:
                    if not re.search(r"\d{3,}", linea) and len(linea) > 2:
                        nombre_cliente = linea.title()
                        break
                if nombre_cliente == "Cliente" and lineas_col4:
                    nombre_cliente = lineas_col4[-1].title()
                empresa_prestamista = obtener_empresa_prestamista(columnas, indice_empresa)

                print(f"\n[{i+1:02d}/{total_filas:02d}] PROCESANDO ID: {id_app} | Cliente: {nombre_cliente}")

                # 3. Desenmascarar correo electrónico (columna detectada por encabezado)
                celda_email = obtener_celda_email(columnas, indice_email)
                correo_texto = celda_email.inner_text().strip()

                if "*" in correo_texto:
                    for _ in range(3):
                        try:
                            enlace_revelar = celda_email.locator("a, button, span, i").first
                            enlace_revelar.scroll_into_view_if_needed()
                            enlace_revelar.click(timeout=1500, force=True)
                            pagina_lista.wait_for_timeout(600)
                            correo_texto = celda_email.inner_text().strip()
                            if "*" not in correo_texto and "@" in correo_texto:
                                break
                        except Exception:
                            pagina_lista.wait_for_timeout(400)

                correo = obtener_correo_limpio(correo_texto)

                # MODO 4: Solo enviar correo por Gmail (Ultra rápido)
                if solo_correo:
                    if not correo:
                        print(f"[{id_app}] ⚠️ Sin correo electrónico registrado o no fue posible desenmascarar.")
                        clientes_fallidos.append({"id": id_app, "etapa": "Extracción Correo", "motivo": "Sin correo"})
                        continue
                    if enviar_correo_smtp(correo, nombre_cliente=nombre_cliente, server_smtp=server_persistente,
                                          gestor_imap=gestor_imap, empresa=empresa_prestamista):
                        clientes_completos.append({"id": id_app, "correo": correo, "nombre": nombre_cliente})
                    else:
                        clientes_fallidos.append({"id": id_app, "etapa": "Envío SMTP", "motivo": "Fallo al enviar correo"})
                    continue

                # MODOS 1, 2, 3, 5 y 6: Interacción con la Ficha Detallada
                pestana_detalle = None
                try:
                    pagina_lista.bring_to_front()
                    enlace_id = columnas.nth(0).locator("a").first
                    if not enlace_id.is_visible():
                        enlace_id = pagina_lista.locator(f"a:has-text('{id_app}')").first

                    enlace_id.scroll_into_view_if_needed()
                    with context.expect_page() as nueva_pestana_info:
                        enlace_id.click()

                    pestana_detalle = nueva_pestana_info.value
                    pestana_detalle.wait_for_load_state("domcontentloaded")

                    # =====================================================
                    # MODO 6: PERSONALIZADO CON CAPTURA DE COSTO
                    # =====================================================
                    if modo_personalizado_costo:
                        if not correo:
                            print(f"[{id_app}] ⚠️ Cliente sin correo registrado. Omitiendo.")
                            pestana_detalle.close()
                            pagina_lista.bring_to_front()
                            clientes_fallidos.append({"id": id_app, "etapa": "Extracción Correo", "motivo": "Sin correo"})
                            continue

                        pestana_detalle.wait_for_timeout(2500)
                        print(f"[{id_app}] 📸 Localizando sección de costo...")

                        titulo_costo = pestana_detalle.locator("text=costo").first
                        if titulo_costo.is_visible():
                            titulo_costo.scroll_into_view_if_needed()
                        pestana_detalle.wait_for_timeout(600)

                        bloque_costo = pestana_detalle.locator("div").filter(has_text="restante sin pagar").filter(has_text="Monto del contrato").last
                        bloque_costo.scroll_into_view_if_needed()
                        pestana_detalle.wait_for_timeout(600)

                        ruta_img_costo = os.path.join(DIR_COSTOS, f"costo_{id_app}.png")
                        bloque_costo.screenshot(path=ruta_img_costo)
                        print(f"[{id_app}] Captura de costo guardada: {ruta_img_costo}")

                        print(f"[{id_app}] ✉️ Enviando correo HTML con captura a {correo} ({nombre_cliente})...")
                        if enviar_correo_con_costo(correo, nombre_cliente, ruta_img_costo, server_smtp=server_persistente,
                                                   gestor_imap=gestor_imap, empresa=empresa_prestamista):
                            clientes_completos.append({"id": id_app, "correo": correo, "nombre": nombre_cliente})
                        else:
                            clientes_fallidos.append({"id": id_app, "etapa": "Envío SMTP", "motivo": "Fallo al enviar correo HTML"})

                        pestana_detalle.close()
                        pagina_lista.bring_to_front()
                        pagina_lista.wait_for_timeout(800)
                        continue

                    # =====================================================
                    # MODO 5: SOLO ENVIAR SMS DIRECTO (SIN LÍMITES NI COLECCIÓN)
                    # =====================================================
                    if modo_solo_sms:
                        pestana_detalle.wait_for_timeout(2500)
                        msg_corto, var_num = obtener_mensaje_corto_plataforma(
                            contador_rondas_msg, nombre_cliente, empresa_prestamista
                        )
                        contador_rondas_msg += 1
                        print(f"[{id_app}] 📱 SMS Plataforma (Variante #{var_num:02d}/{len(PLANTILLAS_MENSAJES_CORTOS)} | {len(msg_corto)} carac.): {msg_corto[:45]}...")

                        btn_enviar_msg = pestana_detalle.locator("button:has-text('enviar mensajes')").last
                        btn_enviar_msg.click(force=True)
                        modal_msg = pestana_detalle.locator(".ant-modal-content:visible").filter(has_text="enviar mensajes").first
                        modal_msg.wait_for(state="visible", timeout=15000)
                        modal_msg.locator("textarea#content, textarea").first.fill(msg_corto)
                        pestana_detalle.wait_for_timeout(600)
                        modal_msg.locator("button:has-text('Aceptar')").last.click()

                        pestana_detalle.wait_for_timeout(800)
                        aviso_alerta = pestana_detalle.locator(".ant-message-error, .ant-notification-notice-error, .ant-form-explain").first
                        if aviso_alerta.is_visible():
                            print(f"    ⚠️ AVISO PLATAFORMA: {aviso_alerta.inner_text().strip()}")

                        try:
                            modal_msg.wait_for(state="hidden", timeout=12000)
                        except Exception:
                            pestana_detalle.keyboard.press("Escape")

                        pestana_detalle.wait_for_timeout(800)
                        print(f"    -> ✅ SMS enviado.")
                        pestana_detalle.close()
                        pagina_lista.bring_to_front()
                        pagina_lista.wait_for_timeout(800)
                        clientes_completos.append({"id": id_app, "nombre": nombre_cliente, "tipo": f"SMS Variante #{var_num}"})
                        continue

                    # =====================================================
                    # MODOS 1, 2 Y 3: VERIFICACIÓN Y REGISTRO EN COLECCIÓN
                    # =====================================================
                    # Espera dinámica en lugar de 15 segundos fijos
                    pestana_detalle.wait_for_timeout(3500)
                    cant_colecciones = obtener_cantidad_colecciones(pestana_detalle)
                    print(f"[{id_app}] Gestiones previas en Colección: {cant_colecciones}")

                    if modo_registrar_correos and cant_colecciones > 0:
                        print(f"[{id_app}] ⏭️ Ya tiene una gestión registrada. Omitiendo.")
                        clientes_ya_gestionados.append({"id": id_app, "nombre": nombre_cliente, "filas": cant_colecciones})
                        pestana_detalle.close()
                        pagina_lista.bring_to_front()
                        pagina_lista.wait_for_timeout(800)
                        continue

                    if modo_limite and cant_colecciones >= 3:
                        print(f"[{id_app}] ⛔ Ya tiene {cant_colecciones} gestiones registradas (Límite máximo 3 por día alcanzado). Omitiendo.")
                        clientes_limite_alcanzado.append({"id": id_app, "nombre": nombre_cliente, "filas": cant_colecciones})
                        pestana_detalle.close()
                        pagina_lista.bring_to_front()
                        pagina_lista.wait_for_timeout(800)
                        continue

                    if opcion == "3":
                        sms_previos = obtener_cantidad_sms_coleccion(pestana_detalle)
                        if sms_previos >= 3:
                            print(f"[{id_app}] ⛔ Ya tiene {sms_previos} SMS registrados. Límite de SMS alcanzado.")
                            clientes_limite_alcanzado.append({"id": id_app, "nombre": nombre_cliente, "filas": sms_previos})
                            pestana_detalle.close()
                            pagina_lista.bring_to_front()
                            pagina_lista.wait_for_timeout(800)
                            continue

                    # --- MODO 7: SOLO REGISTRAR CORREOS YA ENVIADOS ---
                    if modo_registrar_correos:
                        print(f"[{id_app}] 📝 Registrando correo previo en Añadir colección...")
                        btn_col = pestana_detalle.locator("button:has-text('Agregar estado de colección')").first
                        btn_col.scroll_into_view_if_needed()
                        btn_col.click(force=True)
                        llenar_modal_coleccion(pestana_detalle, tipo_canal="email")

                    # --- MODOS 1 Y 2: EMAIL COMPLETO ---
                    elif opcion in ("1", "2"):
                        if correo:
                            print(f"[{id_app}] Enviando correo formal completo por Gmail a {correo} ({nombre_cliente})...")
                            enviar_correo_smtp(correo, nombre_cliente=nombre_cliente, server_smtp=server_persistente,
                                               gestor_imap=gestor_imap, empresa=empresa_prestamista)
                        else:
                            print(f"[{id_app}] ⚠️ Sin correo para enviar por Gmail, continuando con plataforma...")

                        if opcion == "1":
                            enviado, var_num, intentos, motivo = enviar_sms_plataforma_con_rotacion(
                                pestana_detalle, contador_rondas_msg, nombre_cliente, empresa_prestamista
                            )
                            contador_rondas_msg += intentos
                            if not enviado:
                                print(f"[{id_app}] ⏭️ Mensaje no enviado; no se registrará. Motivo: {motivo}")
                                clientes_fallidos.append(
                                    {"id": id_app, "etapa": "Envío mensaje plataforma", "motivo": motivo}
                                )
                                pestana_detalle.close()
                                pagina_lista.bring_to_front()
                                pagina_lista.wait_for_timeout(800)
                                continue
                        else:
                            msg_corto, var_num = obtener_mensaje_corto_plataforma(
                                contador_rondas_msg, nombre_cliente, empresa_prestamista
                            )
                            contador_rondas_msg += 1
                            print(f"[{id_app}] 💬 Mensaje Plataforma (Variante #{var_num:02d}/{len(PLANTILLAS_MENSAJES_CORTOS)} | {len(msg_corto)} carac.): {msg_corto[:45]}...")

                            btn_enviar_msg = pestana_detalle.locator("button:has-text('enviar mensajes')").last
                            btn_enviar_msg.click(force=True)
                            modal_msg = pestana_detalle.locator(".ant-modal-content:visible").filter(has_text="enviar mensajes").first
                            modal_msg.wait_for(state="visible", timeout=15000)
                            modal_msg.locator("textarea#content, textarea").first.fill(msg_corto)
                            pestana_detalle.wait_for_timeout(600)
                            modal_msg.locator("button:has-text('Aceptar')").last.click()

                            pestana_detalle.wait_for_timeout(800)
                            aviso_alerta = pestana_detalle.locator(".ant-message-error, .ant-notification-notice-error, .ant-form-explain").first
                            if aviso_alerta.is_visible():
                                print(f"    ⚠️ AVISO PLATAFORMA: {aviso_alerta.inner_text().strip()}")

                            try:
                                modal_msg.wait_for(state="hidden", timeout=12000)
                            except Exception:
                                pestana_detalle.keyboard.press("Escape")

                        pestana_detalle.wait_for_timeout(2000)

                        print(f"[{id_app}] Registrando en Añadir colección: Canal E-MAIL...")
                        btn_col = pestana_detalle.locator("button:has-text('Agregar estado de colección')").first
                        btn_col.scroll_into_view_if_needed()
                        btn_col.click(force=True)
                        llenar_modal_coleccion(pestana_detalle, tipo_canal="email")

                    # --- MODO 3: SMS COMPLETO (ENVÍO + COLECCIÓN) ---
                    elif opcion == "3":
                        enviado, var_num, intentos, motivo = enviar_sms_plataforma_con_rotacion(
                            pestana_detalle, contador_rondas_msg, nombre_cliente, empresa_prestamista
                        )
                        contador_rondas_msg += intentos
                        if not enviado:
                            print(f"[{id_app}] ⏭️ SMS no enviado; no se registrará. Motivo: {motivo}")
                            clientes_fallidos.append({"id": id_app, "etapa": "Envío SMS", "motivo": motivo})
                            pestana_detalle.close()
                            pagina_lista.bring_to_front()
                            pagina_lista.wait_for_timeout(800)
                            continue

                        pestana_detalle.wait_for_timeout(2000)

                        print(f"[{id_app}] 📝 Registrando en Añadir colección: Canal SMS...")
                        btn_col = pestana_detalle.locator("button:has-text('Agregar estado de colección')").first
                        btn_col.scroll_into_view_if_needed()
                        btn_col.click(force=True)
                        llenar_modal_coleccion(pestana_detalle, tipo_canal="sms")

                    pestana_detalle.close()
                    pagina_lista.bring_to_front()
                    pagina_lista.wait_for_timeout(1000)
                    clientes_completos.append({"id": id_app, "nombre": nombre_cliente, "correo": correo})

                except Exception as e:
                    print(f"[{id_app}] ⚠️ Error durante la gestión: {e}")
                    clientes_fallidos.append({"id": id_app, "etapa": "Gestión en Plataforma", "motivo": str(e)})
                    try:
                        if pestana_detalle and not pestana_detalle.is_closed():
                            pestana_detalle.close()
                    except Exception:
                        pass
                    try:
                        for p_abierta in context.pages:
                            if p_abierta != pagina_lista and "detail" in p_abierta.url:
                                p_abierta.close()
                        pagina_lista.bring_to_front()
                    except Exception:
                        pass

        finally:
            # Limpieza y cierre ordenado de sesiones
            if server_persistente:
                try:
                    server_persistente.quit()
                except Exception:
                    pass
            if gestor_imap:
                try:
                    gestor_imap.cerrar()
                except Exception:
                    pass

        # =================================================================
        # REPORTE FINAL CONSOLIDADO
        # =================================================================
        print("\n" + "=" * 76)
        print("                  📊 REPORTE FINAL DE LA JORNADA                  ")
        print("=" * 76)
        print(f"Total registros leídos:       {total_filas}")
        print(f"✅ Gestiones Completadas:      {len(clientes_completos)}")
        print(f"⛔ Omitidos por Límite (>= 3): {len(clientes_limite_alcanzado)}")
        print(f"❌ Fallidos / Con Incidencias: {len(clientes_fallidos)}")
        print("-" * 76)

        if clientes_completos:
            print("\n✅ DETALLE DE CLIENTES PROCESADOS CON ÉXITO:")
            for c in clientes_completos:
                correo_str = f" | Correo: {c['correo']}" if c.get("correo") else ""
                print(f"  -> ID: {c['id']:<10} | Nombre: {c.get('nombre', 'N/A'):<24}{correo_str}")

        if clientes_limite_alcanzado:
            print("\n⛔ DETALLE DE CLIENTES OMITIDOS POR LÍMITE DE GESTIONES (>= 3):")
            for c in clientes_limite_alcanzado:
                print(f"  -> ID: {c['id']:<10} | Nombre: {c.get('nombre', 'N/A'):<24} | Filas previas: {c['filas']}")

        if clientes_ya_gestionados:
            print("\n⏭️ CLIENTES OMITIDOS POR TENER GESTIONES PREVIAS:")
            for c in clientes_ya_gestionados:
                print(f"  -> ID: {c['id']:<10} | Nombre: {c.get('nombre', 'N/A'):<24} | Filas previas: {c['filas']}")

        if clientes_fallidos:
            print("\n❌ DETALLE DE CLIENTES CON ERROR (REQUIEREN REVISIÓN MANUAL):")
            for c in clientes_fallidos:
                print(f"  -> ID: {c['id']:<10} | Etapa: {c.get('etapa', 'Desconocida'):<22} | Motivo: {c['motivo'][:65]}")

        print("=" * 76)


if __name__ == "__main__":
    ejecutar_scraper()
