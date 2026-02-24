import os
import logging
import asyncio
import threading
import json
import uuid
import hmac
import requests
import tempfile
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler
import yt_dlp

# ================== CONFIGURACIÓN DESDE ENTORNO ==================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
PAYMENT_WEBHOOK_TOKEN = os.environ.get("PAYMENT_WEBHOOK_TOKEN")
HELEKET_MERCHANT_UUID = os.environ.get("HELEKET_MERCHANT_UUID")
HELEKET_API_KEY = os.environ.get("HELEKET_API_KEY")
PO_TOKEN_PROVIDER = os.environ.get("PO_TOKEN_PROVIDER")  # URL del servicio bgutil-ytdlp-pot-provider (opcional)
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost")
PORT = int(os.environ.get("PORT", 8080))

# Datos de pago fijos (para Transfermóvil y Cubacel)
TARJETA_NUMERO = "9234567890123456"  # CAMBIAR por tu tarjeta
CUENTA_SALDO = "51234567"            # CAMBIAR por tu número de teléfono para recibir saldo

# Precios (en CUP y USDT)
PRECIOS = {
    "basico": {"tarjeta": 250, "saldo": 120, "usdt": 0.5},
    "premium": {"tarjeta": 600, "saldo": 300, "usdt": 1.0},
}
PROMO_DESCUENTO = 0.75  # 75% de descuento para nuevos usuarios (pagan 0.75 USDT en lugar de 1)
REFERIDO_DESC_BASICO = 10
REFERIDO_DESC_PREMIUM = 15

# Configuración de logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== SUPABASE ==================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_user(chat_id):
    result = supabase.table("users").select("*").eq("chat_id", chat_id).execute()
    return result.data[0] if result.data else None

def create_user(chat_id, promo_end=None, referral_code=None):
    data = {
        "chat_id": chat_id,
        "plan": "free",
        "videos_used": 0,
        "reset_date": (datetime.utcnow() + timedelta(days=1)).isoformat(),
        "referral_code": referral_code or str(uuid.uuid4())[:8],
        "promo_end": promo_end.isoformat() if promo_end else None,
        "discount_next_month": 0,
        "notified_5h": False,
        "notified_1h": False,
        "notified_30m": False,
        "notified_10m": False,
        "notified_expired": False,
    }
    supabase.table("users").insert(data).execute()

def update_user(chat_id, updates):
    supabase.table("users").update(updates).eq("chat_id", chat_id).execute()

def get_user_by_referral(code):
    result = supabase.table("users").select("*").eq("referral_code", code).execute()
    return result.data[0] if result.data else None

def set_referrer(chat_id, referrer_id):
    supabase.table("users").update({"referrer_id": referrer_id}).eq("chat_id", chat_id).execute()

def get_pending_payment(chat_id):
    result = supabase.table("payments").select("*").eq("chat_id", chat_id).eq("status", "pending").execute()
    return result.data[0] if result.data else None

def create_pending_payment(chat_id, plan, metodo, telefono, monto, tarjeta_destino=None, invoice_id=None):
    data = {
        "chat_id": chat_id,
        "plan_purchased": plan,
        "method": metodo,
        "amount": monto,
        "currency": "CUP" if metodo in ["tarjeta", "saldo"] else "USDT",
        "status": "pending",
        "metadata": {
            "telefono": telefono,
            "tarjeta_destino": tarjeta_destino,
            "invoice_id": invoice_id
        }
    }
    result = supabase.table("payments").insert(data).execute()
    return result.data[0]['id'] if result.data else None

def complete_payment(payment_id, trans_id):
    supabase.table("payments").update({
        "status": "completed",
        "trans_id": trans_id,
        "completed_at": datetime.utcnow().isoformat()
    }).eq("id", payment_id).execute()

def find_pending_payment(metodo, telefono=None, monto=None, tarjeta=None, invoice_id=None):
    query = supabase.table("payments").select("*").eq("status", "pending").eq("method", metodo)
    if telefono:
        query = query.filter("metadata->>telefono", "eq", telefono)
    if monto:
        query = query.eq("amount", monto)
    if tarjeta:
        query = query.filter("metadata->>tarjeta_destino", "eq", tarjeta)
    if invoice_id:
        query = query.filter("metadata->>invoice_id", "eq", invoice_id)
    result = query.execute()
    return result.data[0] if result.data else None

def activate_plan(chat_id, plan):
    if plan == "free":
        reset = timedelta(days=1)
    else:
        reset = timedelta(days=30)
    reset_date = (datetime.utcnow() + reset).isoformat()
    update_user(chat_id, {"plan": plan, "videos_used": 0, "reset_date": reset_date})

def aplicar_descuento_referido(chat_id):
    user = get_user(chat_id)
    if user and user.get("referrer_id"):
        referrer_id = user["referrer_id"]
        plan = user["plan"]
        descuento = REFERIDO_DESC_BASICO if plan == "basico" else REFERIDO_DESC_PREMIUM
        referrer = get_user(referrer_id)
        nuevo_descuento = referrer.get("discount_next_month", 0) + descuento
        update_user(referrer_id, {"discount_next_month": nuevo_descuento})

def get_limit(plan):
    limites = {"free": 5, "basico": 100, "premium": 1000}
    return limites.get(plan, 5)

def get_period(plan):
    return "día" if plan == "free" else "mes"

def get_admin_stats():
    users = supabase.table("users").select("*").execute().data
    payments = supabase.table("payments").select("*").execute().data
    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    today_income = sum(p["amount"] for p in payments if p["status"]=="completed" and datetime.fromisoformat(p["completed_at"]).date() == today)
    week_income = sum(p["amount"] for p in payments if p["status"]=="completed" and datetime.fromisoformat(p["completed_at"]).date() >= week_ago)
    month_income = sum(p["amount"] for p in payments if p["status"]=="completed" and datetime.fromisoformat(p["completed_at"]).date() >= month_ago)
    pending = len([p for p in payments if p["status"]=="pending"])
    return {
        "total_users": len(users),
        "today_income": today_income,
        "week_income": week_income,
        "month_income": month_income,
        "pending_tickets": pending
    }

# ================== FUNCIONES DE DESCARGA con yt-dlp ==================
async def get_format_list(url, chat_id, plan_info):
    """Obtiene la lista de formatos disponibles, usando PO Token si es YouTube y el plan es premium."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'simulate': True,
        'forceurl': True,
    }
    # Si es YouTube y el plan es premium, intentar obtener PO Token
    if 'youtube.com' in url or 'youtu.be' in url:
        if plan_info['plan'] == 'premium' and PO_TOKEN_PROVIDER:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(PO_TOKEN_PROVIDER) as resp:
                        if resp.status == 200:
                            token_data = await resp.json()
                            po_token = token_data.get('token')
                            if po_token:
                                ydl_opts['extractor_args'] = {
                                    'youtube': {
                                        'player-client': ['web', 'default'],
                                        'po_token': [f'web+{po_token}']
                                    }
                                }
                                logger.info("PO Token obtenido correctamente")
                # También podríamos usar --impersonate
                ydl_opts['impersonate'] = 'chrome-124'
            except Exception as e:
                logger.error(f"Error obteniendo PO Token: {e}")
        elif plan_info['plan'] != 'premium':
            # Si no es premium, no se permite YouTube
            return None, "YouTube solo está disponible en plan Premium", 0

    loop = asyncio.get_event_loop()
    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                raise Exception(f"yt-dlp error: {str(e)}")
            formats = []
            for f in info.get('formats', []):
                if f.get('vcodec') != 'none' or f.get('acodec') != 'none':
                    format_id = f['format_id']
                    quality = f.get('format_note') or (f.get('height') and f"{f['height']}p") or 'audio'
                    ext = f.get('ext', 'unknown')
                    filesize = f.get('filesize') or f.get('filesize_approx')
                    size_str = f" ({filesize//1048576} MB)" if filesize else ''
                    label = f"{quality} - {ext}{size_str}"
                    formats.append((format_id, label))
            if not formats and info.get('url'):
                formats = [('direct', '📥 Descargar (calidad única)')]
            return formats, info.get('title', 'Sin título'), info.get('duration', 0)
    return await loop.run_in_executor(None, extract)

async def get_direct_url_with_format(url, format_id, chat_id, plan_info):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'simulate': True,
        'forceurl': True,
        'format': format_id
    }
    if 'youtube.com' in url or 'youtu.be' in url:
        if plan_info['plan'] == 'premium' and PO_TOKEN_PROVIDER:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(PO_TOKEN_PROVIDER) as resp:
                        if resp.status == 200:
                            token_data = await resp.json()
                            po_token = token_data.get('token')
                            if po_token:
                                ydl_opts['extractor_args'] = {
                                    'youtube': {
                                        'player-client': ['web', 'default'],
                                        'po_token': [f'web+{po_token}']
                                    }
                                }
                ydl_opts['impersonate'] = 'chrome-124'
            except:
                pass
    loop = asyncio.get_event_loop()
    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'url' in info:
                return info['url']
            elif 'requested_formats' in info:
                return info['requested_formats'][0]['url']
            else:
                raise Exception("No se pudo extraer la URL directa")
    return await loop.run_in_executor(None, extract)

# ================== FLASK APP ==================
app = Flask(__name__)

# Ruta raíz con meta tag de Heleket
@app.route('/')
def home():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="heleket" content="c88b07b1" />
        <title>Bot Descargador</title>
        <style>
            body { background: #1a1e2b; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .container { text-align: center; }
            h1 { background: linear-gradient(135deg, #b9b9b9, #e5e5e5, #b9b9b9); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-size: 3em; }
            .sub { color: #aaa; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>⚡ Bot Descargador</h1>
            <p class="sub">Usa el bot de Telegram o la <a href="/webapp" style="color:gold;">WebApp</a>.</p>
        </div>
    </body>
    </html>
    """
    return html

# Servir la WebApp (archivo separado)
@app.route('/webapp')
def webapp():
    try:
        with open('webapp.html', 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return "webapp.html no encontrado", 404

# Webhook para pagos desde tu servicio Flask (Transfermóvil/Cubacel)
@app.route('/payment-webhook', methods=['POST'])
def payment_webhook():
    auth_token = request.headers.get('X-Auth-Token')
    if not auth_token or not hmac.compare_digest(auth_token, PAYMENT_WEBHOOK_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    tipo = data.get('type')
    if tipo == 'TRANSFERMOVIL_PAGO':
        return procesar_transfermovil(data)
    elif tipo == 'CUBACEL_SALDO_RECIBIDO':
        return procesar_cubacel(data)
    else:
        return jsonify({"error": "Unknown type"}), 400

def procesar_transfermovil(data):
    pago = data['data']
    telefono = pago.get('telefono_origen')
    monto = pago.get('monto')
    tarjeta_destino = pago.get('tarjeta_destino') or data.get('card_number')
    trans_id = pago.get('trans_id')

    ticket = find_pending_payment(metodo='tarjeta', telefono=telefono, monto=monto, tarjeta=tarjeta_destino)
    if ticket:
        chat_id = ticket['chat_id']
        plan = ticket['plan_purchased']
        activate_plan(chat_id, plan)
        complete_payment(ticket['id'], trans_id)
        aplicar_descuento_referido(chat_id)
        # Notificar al usuario
        threading.Thread(target=enviar_notificacion, args=(chat_id, plan)).start()
        return jsonify({"status": "ok", "message": "Plan activado"})
    else:
        return jsonify({"status": "ignored"})

def procesar_cubacel(data):
    pago = data['data']
    remitente = pago.get('remitente')
    monto = pago.get('monto')
    trans_id = pago.get('trans_id') or f"CUBACEL_{int(datetime.utcnow().timestamp())}"

    ticket = find_pending_payment(metodo='saldo', telefono=remitente, monto=monto)
    if ticket:
        chat_id = ticket['chat_id']
        plan = ticket['plan_purchased']
        activate_plan(chat_id, plan)
        complete_payment(ticket['id'], trans_id)
        aplicar_descuento_referido(chat_id)
        threading.Thread(target=enviar_notificacion, args=(chat_id, plan)).start()
        return jsonify({"status": "ok"})
    return jsonify({"status": "ignored"})

def enviar_notificacion(chat_id, plan):
    # Esta función se ejecuta en un hilo separado
    import telegram
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    bot.send_message(chat_id=chat_id, text=f"✅ ¡Pago recibido! Tu plan *{plan.upper()}* está activado.", parse_mode='Markdown')

# Webhook para Heleket
@app.route('/heleket-webhook', methods=['POST'])
def heleket_webhook():
    # Verificar firma (Heleket envía X-Signature con HMAC-SHA256)
    signature = request.headers.get('X-Signature')
    if not signature:
        return jsonify({"error": "Missing signature"}), 401

    payload = request.get_data(as_text=True)
    computed = hmac.new(HELEKET_API_KEY.encode(), payload.encode(), 'sha256').hexdigest()
    if not hmac.compare_digest(signature, computed):
        return jsonify({"error": "Invalid signature"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    invoice_id = data.get('invoice_id')
    status = data.get('status')
    metadata = data.get('metadata', {})

    if status == 'paid' and invoice_id:
        ticket = find_pending_payment(metodo='usdt', invoice_id=invoice_id)
        if ticket:
            chat_id = ticket['chat_id']
            plan = ticket['plan_purchased']
            activate_plan(chat_id, plan)
            complete_payment(ticket['id'], invoice_id)
            aplicar_descuento_referido(chat_id)
            threading.Thread(target=enviar_notificacion, args=(chat_id, plan)).start()
            return jsonify({"status": "ok"})
    return jsonify({"status": "ignored"})

# API para la WebApp
@app.route('/api/create-invoice', methods=['POST'])
def api_create_invoice():
    data = request.get_json()
    chat_id = data.get('chat_id')
    plan = data.get('plan')
    if not chat_id or not plan:
        return jsonify({"error": "Faltan datos"}), 400

    if get_pending_payment(chat_id):
        return jsonify({"error": "Ya tienes una solicitud pendiente. Cáncelala antes de crear otra."}), 400

    user = get_user(chat_id)
    monto_usdt = PRECIOS[plan]["usdt"]
    if user and user.get('promo_end') and datetime.fromisoformat(user['promo_end']) > datetime.utcnow():
        monto_usdt = PROMO_DESCUENTO

    # Llamar a Heleket para crear factura
    import requests
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": HELEKET_API_KEY
    }
    payload = {
        "merchant_uuid": HELEKET_MERCHANT_UUID,
        "amount": monto_usdt,
        "currency": "USDT",
        "network": "BEP20",
        "webhook_url": f"https://{RENDER_EXTERNAL_HOSTNAME}/heleket-webhook",
        "metadata": {
            "chat_id": chat_id,
            "plan": plan
        },
        "expires_in": 30  # minutos
    }
    try:
        resp = requests.post("https://api.heleket.com/v1/invoices", json=payload, headers=headers)
        if resp.status_code == 200:
            inv_data = resp.json()
            invoice_id = inv_data['invoice_id']
            address = inv_data['address']
            # Guardar ticket
            create_pending_payment(chat_id, plan, "usdt", None, monto_usdt, invoice_id=invoice_id)
            return jsonify({
                "invoice_id": invoice_id,
                "address": address,
                "amount": monto_usdt,
                "network": "BEP20",
                "expires": inv_data.get('expires_at')
            })
        else:
            logger.error(f"Error Heleket: {resp.text}")
            return jsonify({"error": "Error al crear factura"}), 500
    except Exception as e:
        logger.error(f"Excepción Heleket: {e}")
        return jsonify({"error": "Error de conexión con Heleket"}), 500

@app.route('/api/check-invoice/<invoice_id>', methods=['GET'])
def api_check_invoice(invoice_id):
    ticket = find_pending_payment(metodo='usdt', invoice_id=invoice_id)
    if ticket and ticket['status'] == 'completed':
        return jsonify({"status": "paid"})
    elif ticket:
        return jsonify({"status": "pending"})
    else:
        return jsonify({"status": "not_found"}), 404

@app.route('/api/cancel-request', methods=['POST'])
def api_cancel_request():
    data = request.get_json()
    chat_id = data.get('chat_id')
    if not chat_id:
        return jsonify({"error": "Faltan datos"}), 400
    supabase.table("payments").update({"status": "cancelled"}).eq("chat_id", chat_id).eq("status", "pending").execute()
    return jsonify({"status": "ok"})

@app.route('/api/user/<int:chat_id>')
def api_user(chat_id):
    user = get_user(chat_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "plan": user['plan'],
        "videos_used": user['videos_used'],
        "limit": get_limit(user['plan'])
    })

@app.route('/api/pending/<int:chat_id>')
def api_pending(chat_id):
    pend = get_pending_payment(chat_id)
    return jsonify({"exists": bool(pend)})

@app.route('/api/create-payment-ticket', methods=['POST'])
def api_create_payment_ticket():
    data = request.get_json()
    chat_id = data.get('chat_id')
    plan = data.get('plan')
    metodo = data.get('metodo')
    telefono = data.get('telefono')
    if not all([chat_id, plan, metodo, telefono]):
        return jsonify({"error": "Faltan datos"}), 400
    if get_pending_payment(chat_id):
        return jsonify({"error": "Ya tienes una solicitud pendiente"}), 400
    monto = PRECIOS[plan][metodo]  # 'tarjeta' o 'saldo'
    create_pending_payment(chat_id, plan, metodo, telefono, monto, TARJETA_NUMERO if metodo=='tarjeta' else None)
    return jsonify({"status": "ok"})

@app.route('/api/admin/stats')
def api_admin_stats():
    # Aquí se podría verificar que la solicitud viene de un admin (por IP o token)
    return jsonify(get_admin_stats())

@app.route('/api/admin/pending-payments')
def api_admin_pending():
    result = supabase.table("payments").select("*").eq("status", "pending").execute()
    return jsonify(result.data)

@app.route('/keepalive')
def keepalive():
    return "OK", 200

# ================== BOT DE TELEGRAM ==================
application = ApplicationBuilder().token(TELEGRAM_TOKEN).connect_timeout(30).read_timeout(30).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    if not user:
        promo_end = datetime.utcnow() + timedelta(hours=24)
        create_user(chat_id, promo_end)
        args = context.args
        if args and args[0].startswith('ref_'):
            ref_code = args[0][4:]
            referrer = get_user_by_referral(ref_code)
            if referrer:
                set_referrer(chat_id, referrer['chat_id'])
        user = get_user(chat_id)

    texto = (
        f"👋 *¡Bienvenido al Bot Descargador!*\n\n"
        f"📊 *Tu plan:* `{user['plan'].upper()}`\n"
        f"📥 *Descargas usadas:* {user['videos_used']}/{get_limit(user['plan'])} ({get_period(user['plan'])})\n"
        f"🎁 *Promoción:* Si eres nuevo, tienes 24h para probar Premium con 75% de descuento (solo 0.75 USDT o equivalente).\n\n"
        f"Envía un enlace para comenzar a descargar."
    )
    keyboard = [
        [InlineKeyboardButton("📦 Planes y Precios", callback_data="planes")],
        [InlineKeyboardButton("🎁 Ventajas", callback_data="ventajas")],
        [InlineKeyboardButton("👥 Referidos", callback_data="referidos")],
        [InlineKeyboardButton("🆘 Soporte", callback_data="soporte")],
        [InlineKeyboardButton("🌐 WebApp", url=f"https://{RENDER_EXTERNAL_HOSTNAME}/webapp")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(texto, parse_mode='Markdown', reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "planes":
        await mostrar_planes(query)
    elif data == "ventajas":
        await mostrar_ventajas(query)
    elif data == "referidos":
        await mostrar_referidos(query, context)
    elif data == "soporte":
        await mostrar_soporte(query)
    elif data.startswith("pagar_"):
        await iniciar_pago_bot(query, context)
    elif data.startswith("pais_"):
        await seleccionar_pais(query, context)
    elif data.startswith("metodo_"):
        await seleccionar_metodo(query, context)
    elif data == "volver_inicio":
        await volver_inicio(query)
    elif data == "cancelar_solicitud":
        await cancelar_solicitud(query, context)

async def mostrar_planes(query):
    texto = (
        "*📦 Planes disponibles:*\n\n"
        "🆓 *Gratuito*\n• 5 descargas/día\n• Redes sociales y sitios públicos\n\n"
        "⚡ *Básico* – 250 CUP/mes (tarjeta) | 120 CUP (saldo) | 0.50 USDT\n"
        "• 100 descargas/mes\n• Redes sociales + sitios básicos\n\n"
        "💎 *Premium* – 600 CUP/mes (tarjeta) | 300 CUP (saldo) | 1 USDT\n"
        "• 1000 descargas/mes\n• YouTube incluido (con tecnología PO Token)\n• Acceso a todos los sitios soportados\n\n"
        "🎁 *Promoción nuevos usuarios:* Premium por solo 0.75 USDT (450 CUP tarjeta / 225 CUP saldo) durante las primeras 24h.\n\n"
        "Selecciona un plan para pagar:"
    )
    keyboard = [
        [InlineKeyboardButton("⚡ Básico", callback_data="pagar_basico"),
         InlineKeyboardButton("💎 Premium", callback_data="pagar_premium")],
        [InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=reply_markup)

async def mostrar_ventajas(query):
    texto = (
        "*🎁 Ventajas de usar nuestro bot:*\n\n"
        "✅ Descarga desde más de 1000 sitios (YouTube, TikTok, Instagram, Facebook, Twitter, Vimeo, etc.)\n"
        "✅ Calidad seleccionable (hasta 4K)\n"
        "✅ Sin anuncios ni límites molestos (según plan)\n"
        "✅ Activación automática de pagos por Transfermóvil, Cubacel y USDT (BEP20)\n"
        "✅ Soporte rápido por Telegram\n"
        "✅ WebApp elegante para gestionar tu cuenta\n"
        "✅ Promociones y descuentos por referidos\n\n"
        "✨ *¡Únete y empieza a descargar!*"
    )
    keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=reply_markup)

async def mostrar_referidos(query, context):
    chat_id = query.from_user.id
    user = get_user(chat_id)
    codigo = user['referral_code']
    texto = (
        f"👥 *Sistema de Referidos*\n\n"
        f"Comparte tu código con amigos y gana descuentos:\n\n"
        f"`{codigo}`\n\n"
        f"🔗 Enlace: `https://t.me/{context.bot.username}?start=ref_{codigo}`\n\n"
        f"*Recompensas:*\n"
        f"• Por cada amigo que contrate Básico → 10% descuento en tu próximo mes.\n"
        f"• Por cada amigo que contrate Premium → 15% descuento.\n"
        f"¡Acumulable!"
    )
    keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=reply_markup)

async def mostrar_soporte(query):
    texto = (
        "🆘 *Soporte*\n\n"
        "Si tienes problemas, escribe tu consulta aquí mismo y un administrador te responderá a la mayor brevedad.\n\n"
        "También puedes hacer una donación voluntaria. No es necesario, pero si deseas apoyar, puedes enviar USDT (BEP20) a la dirección que aparece en la WebApp.\n\n"
        "¡Gracias! 💙"
    )
    keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=reply_markup)

async def iniciar_pago_bot(query, context):
    plan = query.data.split('_')[1]
    context.user_data['plan_seleccionado'] = plan
    texto = "¿Desde dónde vas a pagar?"
    keyboard = [
        [InlineKeyboardButton("🇨🇺 Cuba", callback_data="pais_cuba")],
        [InlineKeyboardButton("🌍 Otro país", callback_data="pais_ext")],
        [InlineKeyboardButton("🔙 Volver", callback_data="planes")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(texto, reply_markup=reply_markup)

async def seleccionar_pais(query, context):
    pais = query.data.split('_')[1]
    context.user_data['pais'] = pais
    if pais == "cuba":
        texto = "Elige método de pago (CUP):"
        keyboard = [
            [InlineKeyboardButton("💳 Tarjeta (Transfermóvil)", callback_data="metodo_tarjeta")],
            [InlineKeyboardButton("📱 Saldo móvil (Cubacel)", callback_data="metodo_saldo")],
            [InlineKeyboardButton("🔙 Volver", callback_data="pagar_" + context.user_data['plan_seleccionado'])]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(texto, reply_markup=reply_markup)
    else:
        await procesar_pago_usdt(query, context)

async def seleccionar_metodo(query, context):
    metodo = query.data.split('_')[1]
    context.user_data['metodo'] = metodo
    plan = context.user_data['plan_seleccionado']
    chat_id = query.from_user.id
    user = get_user(chat_id)

    if get_pending_payment(chat_id):
        keyboard = [[InlineKeyboardButton("❌ Cancelar solicitud anterior", callback_data="cancelar_solicitud")],
                    [InlineKeyboardButton("🔙 Volver", callback_data="pagar_" + plan)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("⚠️ Ya tienes una solicitud pendiente. Cáncelala antes de crear una nueva.", reply_markup=reply_markup)
        return

    monto_tarjeta = PRECIOS[plan]["tarjeta"]
    monto_saldo = PRECIOS[plan]["saldo"]
    if user.get('promo_end') and datetime.fromisoformat(user['promo_end']) > datetime.utcnow():
        monto_tarjeta = int(monto_tarjeta * PROMO_DESCUENTO)
        monto_saldo = int(monto_saldo * PROMO_DESCUENTO)

    if metodo == "tarjeta":
        texto = (
            f"💳 *Pago por Transfermóvil*\n\n"
            f"Plan: *{plan.upper()}*\n"
            f"Monto a pagar: *{monto_tarjeta} CUP*\n\n"
            f"**Número de tarjeta:** `{TARJETA_NUMERO}`\n\n"
            f"📌 *Instrucciones:*\n"
            f"1. Abre Transfermóvil y selecciona 'Transferencia' a la tarjeta mostrada.\n"
            f"2. **Importante:** Activa la casilla *'Mostrar número al destinatario'* antes de confirmar.\n"
            f"3. Realiza el pago y toma una captura (por si acaso).\n\n"
            f"⚠️ *Los pagos por EnZona no se detectan automáticamente. Si pagas por EnZona, envía una captura a soporte.*\n\n"
            f"Luego, escribe tu número de teléfono (el que usaste para pagar) para verificar."
        )
        context.user_data['esperando_telefono'] = 'tarjeta'
        context.user_data['monto'] = monto_tarjeta
        await query.edit_message_text(texto, parse_mode='Markdown')
    elif metodo == "saldo":
        texto = (
            f"📱 *Pago por Saldo Móvil (Cubacel)*\n\n"
            f"Plan: *{plan.upper()}*\n"
            f"Monto a pagar: *{monto_saldo} CUP*\n\n"
            f"**Número de teléfono destino:** `{CUENTA_SALDO}`\n\n"
            f"📌 *Instrucciones:*\n"
            f"1. Realiza una transferencia de saldo desde tu móvil al número indicado.\n"
            f"2. Espera el SMS de confirmación.\n"
            f"3. Toma una captura por si acaso.\n\n"
            f"Luego, escribe tu número de teléfono (desde donde enviaste el saldo)."
        )
        context.user_data['esperando_telefono'] = 'saldo'
        context.user_data['monto'] = monto_saldo
        await query.edit_message_text(texto, parse_mode='Markdown')

async def procesar_pago_usdt(query, context):
    plan = context.user_data['plan_seleccionado']
    chat_id = query.from_user.id
    user = get_user(chat_id)
    monto_usdt = PRECIOS[plan]["usdt"]
    if user and user.get('promo_end') and datetime.fromisoformat(user['promo_end']) > datetime.utcnow():
        monto_usdt = PROMO_DESCUENTO

    # Crear factura vía API (que a su vez llama a Heleket)
    try:
        import requests
        headers = {"Content-Type": "application/json"}
        payload = {"chat_id": chat_id, "plan": plan}
        resp = requests.post(f"https://{RENDER_EXTERNAL_HOSTNAME}/api/create-invoice", json=payload, headers=headers)
        if resp.status_code == 200:
            inv_data = resp.json()
            texto = (
                f"💎 *Pago en USDT (BEP20)*\n\n"
                f"Plan: *{plan.upper()}*\n"
                f"Monto: *{inv_data['amount']} USDT*\n"
                f"Red: *{inv_data['network']}*\n\n"
                f"**Dirección:**\n`{inv_data['address']}`\n\n"
                f"**Invoice ID:** `{inv_data['invoice_id']}`\n\n"
                f"Envía exactamente *{inv_data['amount']} USDT* a la dirección mostrada.\n"
                f"El pago será verificado automáticamente en pocos minutos."
            )
            keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=reply_markup)
        else:
            await query.edit_message_text("❌ Error al generar factura. Intenta más tarde.")
    except Exception as e:
        logger.error(f"Error en procesar_pago_usdt: {e}")
        await query.edit_message_text("❌ Error de conexión.")

async def cancelar_solicitud(query, context):
    chat_id = query.from_user.id
    supabase.table("payments").update({"status": "cancelled"}).eq("chat_id", chat_id).eq("status", "pending").execute()
    await query.edit_message_text("✅ Solicitud cancelada. Puedes crear una nueva.")

async def volver_inicio(query):
    keyboard = [
        [InlineKeyboardButton("📦 Planes", callback_data="planes"),
         InlineKeyboardButton("🎁 Ventajas", callback_data="ventajas")],
        [InlineKeyboardButton("👥 Referidos", callback_data="referidos"),
         InlineKeyboardButton("🆘 Soporte", callback_data="soporte")],
        [InlineKeyboardButton("🌐 WebApp", url=f"https://{RENDER_EXTERNAL_HOSTNAME}/webapp")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("¡Bienvenido de nuevo!", reply_markup=reply_markup)

async def recibir_telefono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'esperando_telefono' not in context.user_data:
        return
    metodo = context.user_data['esperando_telefono']
    telefono = update.message.text.strip()
    if not telefono.isdigit() or len(telefono) < 8:
        await update.message.reply_text("❌ Número inválido. Debe tener al menos 8 dígitos. Intenta de nuevo:")
        return
    chat_id = update.effective_chat.id
    plan = context.user_data['plan_seleccionado']
    monto = context.user_data['monto']
    if metodo == 'tarjeta':
        create_pending_payment(chat_id, plan, 'tarjeta', telefono, monto, TARJETA_NUMERO)
        await update.message.reply_text(
            "✅ ¡Ticket de pago creado!\n\n"
            "En cuanto detectemos tu pago (normalmente en pocos minutos), se activará tu plan automáticamente.\n"
            "Recibirás una notificación cuando esté listo.\n\n"
            "Puedes ver el estado en /mis_pagos"
        )
    elif metodo == 'saldo':
        create_pending_payment(chat_id, plan, 'saldo', telefono, monto, None)
        await update.message.reply_text("✅ Ticket creado. Espera la confirmación.")
    context.user_data.pop('esperando_telefono')

# Handler para mensajes con enlaces (descargas)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    url = update.message.text.strip()
    if not url.startswith('http'):
        await update.message.reply_text("Por favor, envía un enlace válido.")
        return

    user = get_user(chat_id)
    if not user:
        await update.message.reply_text("Primero usa /start para registrarte.")
        return

    # Verificar límite
    if user['videos_used'] >= get_limit(user['plan']):
        period = "hoy" if user['plan'] == 'free' else "este mes"
        await update.message.reply_text(f"❌ Has alcanzado tu límite de {get_limit(user['plan'])} descargas {period}. Mejora tu plan.")
        return

    wait_msg = await update.message.reply_text("⏳ Analizando enlace...")

    try:
        formats, title, duration = await get_format_list(url, chat_id, user)
        if formats is None:
            await wait_msg.edit_text(title)  # title contiene el mensaje de error (YouTube no permitido)
            return
        if not formats:
            await wait_msg.edit_text("❌ No se encontraron formatos descargables.")
            return

        context.user_data['url'] = url
        context.user_data['formats'] = formats
        context.user_data['title'] = title

        buttons = []
        for i, (fid, label) in enumerate(formats[:10]):
            buttons.append([InlineKeyboardButton(label, callback_data=f"format_{i}")])
        reply_markup = InlineKeyboardMarkup(buttons)

        duration_str = f"{duration//60}:{duration%60:02d}" if duration else "desconocida"
        await wait_msg.edit_text(
            f"📹 *{title}*\n⏱️ Duración: {duration_str}\n\nElige calidad:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error en handle_message: {e}")
        await wait_msg.edit_text(f"❌ Error: {str(e)[:200]}")

async def format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("format_"):
        idx = int(data.split("_")[1])
        formats = context.user_data.get('formats', [])
        url = context.user_data.get('url')
        title = context.user_data.get('title', 'Video')
        chat_id = update.effective_chat.id
        user = get_user(chat_id)

        if not url or idx >= len(formats):
            await query.edit_message_text("❌ Sesión expirada. Envía el enlace de nuevo.")
            return

        format_id, label = formats[idx]
        try:
            direct_url = await get_direct_url_with_format(url, format_id, chat_id, user)

            # Incrementar uso
            new_count = user['videos_used'] + 1
            update_user(chat_id, {"videos_used": new_count})

            # Enviar botón con URL invisible
            invisible_char = '\u200b'
            message_text = f"✅ *{title}* ({label}) listo. Presiona el botón:\n[{invisible_char}]({direct_url})"
            keyboard = [[InlineKeyboardButton("📥 Descargar", url=direct_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error al obtener URL: {str(e)[:200]}")

# Registrar handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_telefono))
application.add_handler(CallbackQueryHandler(format_callback, pattern="^format_"))

# ================== SCHEDULER ==================
scheduler = BackgroundScheduler()

def check_promo_reminders():
    users = supabase.table("users").select("*").not_.is_("promo_end", "null").execute().data
    now = datetime.utcnow()
    for u in users:
        end = datetime.fromisoformat(u['promo_end'])
        diff = (end - now).total_seconds() / 3600
        if 4.5 < diff < 5.5 and not u.get('notified_5h'):
            enviar_notificacion(u['chat_id'], "⏳ ¡Solo quedan 5 horas para tu descuento del 75% en Premium!")
            update_user(u['chat_id'], {"notified_5h": True})
        elif 0.9 < diff < 1.1 and not u.get('notified_1h'):
            enviar_notificacion(u['chat_id'], "⚠️ ¡Última hora! Tu descuento expira en 1 hora.")
            update_user(u['chat_id'], {"notified_1h": True})
        elif 0.4 < diff < 0.6 and not u.get('notified_30m'):
            enviar_notificacion(u['chat_id'], "⏰ ¡30 minutos! Apresúrate.")
            update_user(u['chat_id'], {"notified_30m": True})
        elif 0.1 < diff < 0.2 and not u.get('notified_10m'):
            enviar_notificacion(u['chat_id'], "🔥 ¡10 minutos! Último aviso.")
            update_user(u['chat_id'], {"notified_10m": True})
        elif diff <= 0 and not u.get('notified_expired'):
            enviar_notificacion(u['chat_id'], "⌛ Tu promoción ha expirado. Pero aún puedes contratar planes regulares.")
            update_user(u['chat_id'], {"notified_expired": True})

def keepalive_job():
    try:
        requests.get(f"https://{RENDER_EXTERNAL_HOSTNAME}/keepalive", timeout=5)
    except:
        pass

scheduler.add_job(check_promo_reminders, 'interval', hours=1)
scheduler.add_job(keepalive_job, 'interval', minutes=5)
scheduler.start()

# ================== MAIN ==================
def run_bot():
    application.run_polling()

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    app.run(host='0.0.0.0', port=PORT, debug=False)
