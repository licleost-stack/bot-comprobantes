import os
import json
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import google.genai as genai
from google.genai import types
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image

# ============================================================
# CONFIGURACIÓN
# ============================================================

config = {}

try:
    with open("config.txt", "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                config[k.strip()] = v.strip().strip('"').strip("'")
except Exception as e:
    print(f"❌ Error al leer config.txt: {e}")

TELEGRAM_TOKEN = config.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")
SPREADSHEET_ID = config.get("SPREADSHEET_ID", "")

# ============================================================
# GEMINI
# ============================================================

client_gemini = genai.Client(api_key=GEMINI_API_KEY)

# ============================================================
# GOOGLE SHEETS
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

try:
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=SCOPES
    )

    client_sheets = gspread.authorize(creds)

    sheet = client_sheets.open_by_key(
        SPREADSHEET_ID
    ).sheet1

    print("✅ Conectado exitosamente a Google Sheets.")

except Exception as e:
    print(f"❌ Error conectando con Google Sheets: {e}")


# ============================================================
# PROMPT PARA GEMINI
# ============================================================

PROMPT_OCR = """
Analiza la siguiente imagen de comprobante o transferencia bancaria/fintech.

Extrae la siguiente información y devuélvela estrictamente en formato JSON
con estas claves:

- "fecha": Fecha y hora del comprobante (formato DD/MM/AAAA HH:MM).
  Si no hay hora, solo fecha.

- "remitente": Nombre completo de quien envía o realiza la transferencia.

- "monto": Número flotante con el monto transferido.
  Ejemplo: 433040.00
  Sin signos pesos ni puntos de miles.

- "operacion": Número de transacción, comprobante o referencia.
  Si no existe, colocar "S/D".

Si la imagen no es un comprobante de transferencia válido,
responde exactamente:

{"error": "no_valido"}
"""


# ============================================================
# COLA DE COMPROBANTES
# ============================================================

receipt_queue = asyncio.Queue()

processing_task = None

# Cantidad máxima aproximada de comprobantes por minuto.
# Si tu cuenta dice 5 por minuto, usamos 4 para dejar margen.
SECONDS_BETWEEN_REQUESTS = 15


# ============================================================
# RECIBIR FOTO
# ============================================================

async def receive_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        photo = update.message.photo[-1]

        # Creamos un identificador único para la foto
        file_id = photo.file_id

        # Guardamos en la cola
        await receipt_queue.put({
            "update": update,
            "file_id": file_id
        })

        cantidad = receipt_queue.qsize()

        await update.message.reply_text(
            f"📥 Comprobante recibido.\n"
            f"⏳ En cola: {cantidad}"
        )

    except Exception as e:

        logging.error(f"Error recibiendo comprobante: {e}")

        await update.message.reply_text(
            "⚠️ No pude recibir el comprobante."
        )


# ============================================================
# PROCESADOR DE LA COLA
# ============================================================

async def process_queue(application):

    print("🟢 Procesador de cola iniciado.")

    while True:

        item = await receipt_queue.get()

        update = item["update"]
        file_id = item["file_id"]

        try:

            await process_receipt(update, file_id)

        except Exception as e:

            logging.error(
                f"Error procesando comprobante: {e}"
            )

        finally:

            receipt_queue.task_done()

        # Esperamos antes de enviar otra consulta a Gemini
        await asyncio.sleep(SECONDS_BETWEEN_REQUESTS)


# ============================================================
# PROCESAR UN COMPROBANTE
# ============================================================

async def process_receipt(update, file_id):

    status_msg = await update.message.reply_text(
        "🔎 Analizando comprobante..."
    )

    photo_path = f"temp_{file_id}.jpg"

    max_retries = 5

    try:

        # ----------------------------------------------------
        # DESCARGAR FOTO
        # ----------------------------------------------------

        photo_file = await update.message.get_bot().get_file(file_id)

        await photo_file.download_to_drive(photo_path)

        image = Image.open(photo_path)

        # ----------------------------------------------------
        # GEMINI
        # ----------------------------------------------------

        for intento in range(max_retries):

            try:

                response = client_gemini.models.generate_content(

                    model="gemini-3.6-flash",

                    contents=[
                        image,
                        PROMPT_OCR
                    ],

                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )

                # Si llegamos acá, funcionó
                break

            except Exception as e:

                error_text = str(e).lower()

                # Detectamos límites / demasiadas solicitudes
                if (
                    "429" in error_text
                    or "rate limit" in error_text
                    or "quota" in error_text
                    or "resource exhausted" in error_text
                ):

                    espera = 30 * (intento + 1)

                    await status_msg.edit_text(
                        f"⏳ Gemini está limitando las solicitudes.\n"
                        f"Esperando {espera} segundos..."
                    )

                    await asyncio.sleep(espera)

                else:

                    raise e

        else:

            raise Exception(
                "No fue posible procesar el comprobante "
                "después de varios intentos."
            )

        # ----------------------------------------------------
        # LEER JSON
        # ----------------------------------------------------

        data = json.loads(response.text)

        if "error" in data:

            await status_msg.edit_text(
                "❌ No pude identificar un comprobante válido."
            )

            return

        # ----------------------------------------------------
        # DATOS
        # ----------------------------------------------------

        fecha = data.get("fecha", "")
        remitente = data.get("remitente", "")
        monto = data.get("monto", 0)
        operacion = str(
            data.get("operacion", "")
        )

        try:
            monto = float(monto)
        except:
            monto = 0

        # ----------------------------------------------------
        # GUARDAR EN GOOGLE SHEETS
        # ----------------------------------------------------

        nueva_fila = [
            fecha,
            remitente,
            monto,
            operacion
        ]

        sheet.append_row(nueva_fila)

        # ----------------------------------------------------
        # MENSAJE DE CONFIRMACIÓN
        # ----------------------------------------------------

        monto_fmt = (
            f"${monto:,.2f}"
            .replace(",", "X")
            .replace(".", ",")
            .replace("X", ".")
        )

        msg_exito = (
            f"✅ **Registrado en la planilla**\n\n"
            f"👤 **Origen:** {remitente}\n"
            f"💰 **Monto:** {monto_fmt}\n"
            f"📅 **Fecha:** {fecha}\n"
            f"🔢 **Ref:** {operacion}"
        )

        await status_msg.edit_text(
            msg_exito,
            parse_mode="Markdown"
        )

    except Exception as e:

        logging.error(
            f"Error procesando comprobante: {e}"
        )

        await status_msg.edit_text(
            f"⚠️ Error al procesar:\n{str(e)[:500]}"
        )

    finally:

        # ----------------------------------------------------
        # BORRAR FOTO TEMPORAL
        # ----------------------------------------------------

        if os.path.exists(photo_path):

            try:
                os.remove(photo_path)
            except:
                pass


# ============================================================
# INICIAR BOT
# ============================================================

async def post_init(application):

    global processing_task

    processing_task = asyncio.create_task(
        process_queue(application)
    )

    print("🟢 Cola de procesamiento activada.")


# ============================================================
# MAIN
# ============================================================

def main():

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO
    )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Recibir solamente fotografías
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            receive_receipt
        )
    )

    print("🤖 Bot listo y escuchando en Telegram...")
    print("📥 Los comprobantes se procesarán mediante una cola.")
    print(
        f"⏱️ Intervalo entre comprobantes: "
        f"{SECONDS_BETWEEN_REQUESTS} segundos"
    )

    app.run_polling()


if __name__ == "__main__":
    main()
