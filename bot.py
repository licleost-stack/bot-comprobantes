import os
import json
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import google.genai as genai
from google.genai import types
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image

# Carga de datos desde config.txt
config = {}
try:
    with open("config.txt", "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                config[k.strip()] = v.strip().strip('"').strip("'")
except Exception as e:
    print("Error al leer config.txt")

TELEGRAM_TOKEN = config.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")
SPREADSHEET_ID = config.get("SPREADSHEET_ID", "")

# Configuración de Gemini
client_gemini = genai.Client(api_key=GEMINI_API_KEY)

# Configuración de Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

try:
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client_sheets = gspread.authorize(creds)
    sheet = client_sheets.open_by_key(SPREADSHEET_ID).sheet1
    print("✅ Conectado exitosamente a Google Sheets.")
except Exception as e:
    print(f"❌ Error conectando con Google Sheets: {e}")

PROMPT_OCR = """
Analiza la siguiente imagen de comprobante o transferencia bancaria/fintech.
Extrae la siguiente información y devuélvela estrictamente en formato JSON con estas claves:
- "fecha": Fecha y hora del comprobante (formato DD/MM/AAAA HH:MM). Si no hay hora, solo fecha.
- "remitente": Nombre completo de quien envía o realiza la transferencia.
- "monto": Número flotante con el monto transferido (ej: 433040.00). Sin signos pesos ni puntos de miles.
- "operacion": Número de transacción, comprobante o referencia (si existe, si no "S/D").

Si la imagen no es un comprobante de transferencia válido, responde con {"error": "no_valido"}.
"""

async def process_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("📥 Procesando comprobante...")
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_path = "temp_receipt.jpg"
        await photo_file.download_to_drive(photo_path)
        
        image = Image.open(photo_path)
        
        response = client_gemini.models.generate_content(
            model='gemini-3.6-flash',
            contents=[image, PROMPT_OCR],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        data = json.loads(response.text)
        
        if os.path.exists(photo_path):
            os.remove(photo_path)
            
        if "error" in data:
            await status_msg.edit_text("❌ No pude identificar un comprobante válido.")
            return

        nueva_fila = [
            data.get("fecha", ""),
            data.get("remitente", ""),
            float(data.get("monto", 0)),
            str(data.get("operacion", ""))
        ]
        
        sheet.append_row(nueva_fila)
        
        monto_fmt = f"${nueva_fila[2]:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        msg_exito = (
            f"✅ **Registrado en la planilla**\n\n"
            f"👤 **Origen:** {nueva_fila[1]}\n"
            f"💰 **Monto:** {monto_fmt}\n"
            f"📅 **Fecha:** {nueva_fila[0]}\n"
            f"🔢 **Ref:** {nueva_fila[3]}"
        )
        await status_msg.edit_text(msg_exito, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error: {e}")
        await status_msg.edit_text(f"⚠️ Error al procesar: {str(e)}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, process_receipt))
    print("🤖 Bot listo y escuchando en Telegram...")
    app.run_polling()

if __name__ == "__main__":
    main()
