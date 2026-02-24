# main.py
import os
import logging
import asyncio
import threading
import json
import uuid
import hmac
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIGURACIÓN ==================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 0))
PAYMENT_WEBHOOK_TOKEN = os.environ.get("PAYMENT_WEBHOOK_TOKEN")  # token compartido con tu servicio Flask
HELEKET_MERCHANT_UUID = os.environ.get("HELEKET_MERCHANT_UUID")
HELEKET_API_KEY = os.environ.get("HELEKET_API_KEY")
PORT = int(os.environ.get("PORT", 8080))

# Configuración de pagos
TARJETA_NUMERO = "9234567890123456"  # reemplazar con tu tarjeta
USDT_WALLET = "0xTuWalletBEP20"      # tu wallet BEP20
USDT_NETWORK = "BEP20"

# Precios (en CUP y USDT)
PRECIOS = {
    "basico": {"tarjeta": 250, "saldo": 120, "usdt": 0.5},
    "premium": {"tarjeta": 600, "saldo": 300, "usdt": 1.0},
}
PROMO_DESCUENTO = 0.75  # 75% de descuento (paga 0.75 USDT en lugar de 1)
REFERIDO_DESC_BASICO = 10
REFERIDO_DESC_PREMIUM = 15

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

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
        "plan": plan,
        "method": metodo,
        "amount": monto,
        "currency": "CUP" if metodo in ["tarjeta", "saldo"] else "USDT",
        "trans_id": None,
        "status": "pending",
        "metadata": {
            "telefono": telefono,
            "tarjeta_destino": tarjeta_destino,
            "invoice_id": invoice_id
        }
    }
    supabase.table("payments").insert(data).execute()

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
    # Calcular reset_date según el plan
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
        # Aumentar descuento del referente según el plan comprado
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

# ================== FLASK APP (WEBHOOKS Y WEBAPP) ==================
app = Flask(__name__)

# ------------------ Página de inicio con meta tag para Heleket ------------------
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
            <p class="sub">WebApp en construcción. Usa el bot de Telegram por ahora.</p>
        </div>
    </body>
    </html>
    """
    return html

# ------------------ Webhook para pagos desde tu servicio Flask ------------------
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
        plan = ticket['plan']
        activate_plan(chat_id, plan)
        complete_payment(ticket['id'], trans_id)
        aplicar_descuento_referido(chat_id)
        # Notificar al usuario
        threading.Thread(target=enviar_notificacion, args=(chat_id, plan)).start()
        return jsonify({"status": "ok", "message": "Plan activado"})
    else:
        # Pago no solicitado
        return jsonify({"status": "ignored"})

def procesar_cubacel(data):
    pago = data['data']
    remitente = pago.get('remitente')
    monto = pago.get('monto')
    trans_id = pago.get('trans_id') or f"CUBACEL_{int(datetime.utcnow().timestamp())}"

    ticket = find_pending_payment(metodo='saldo', telefono=remitente, monto=monto)
    if ticket:
        chat_id = ticket['chat_id']
        plan = ticket['plan']
        activate_plan(chat_id, plan)
        complete_payment(ticket['id'], trans_id)
        aplicar_descuento_referido(chat_id)
        threading.Thread(target=enviar_notificacion, args=(chat_id, plan)).start()
        return jsonify({"status": "ok"})
    return jsonify({"status": "ignored"})

def enviar_notificacion(chat_id, plan):
    # Enviar mensaje por el bot (necesita el bot)
    # Se llamará desde un hilo para no bloquear el webhook
    import telegram
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    bot.send_message(chat_id=chat_id, text=f"✅ ¡Pago recibido! Tu plan *{plan.upper()}* está activado.", parse_mode='Markdown')

# ------------------ Webhook para Heleket ------------------
@app.route('/heleket-webhook', methods=['POST'])
def heleket_webhook():
    # Verificar firma (Heleket envía X-Signature, pero simplificamos con token en header)
    auth = request.headers.get('Authorization')
    if not auth or auth != f"Bearer {HELEKET_API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    # Estructura típica de Heleket: { "invoice_id": "...", "status": "paid", "metadata": {...} }
    invoice_id = data.get('invoice_id')
    status = data.get('status')
    metadata = data.get('metadata', {})

    if status == 'paid' and invoice_id:
        ticket = find_pending_payment(metodo='usdt', invoice_id=invoice_id)
        if ticket:
            chat_id = ticket['chat_id']
            plan = ticket['plan']
            activate_plan(chat_id, plan)
            complete_payment(ticket['id'], invoice_id)
            aplicar_descuento_referido(chat_id)
            threading.Thread(target=enviar_notificacion, args=(chat_id, plan)).start()
            return jsonify({"status": "ok"})
    return jsonify({"status": "ignored"})

# ------------------ API para la WebApp (crear factura Heleket) ------------------
@app.route('/api/create-invoice', methods=['POST'])
def api_create_invoice():
    data = request.get_json()
    chat_id = data.get('chat_id')
    plan = data.get('plan')
    if not chat_id or not plan:
        return jsonify({"error": "Faltan datos"}), 400

    # Verificar si ya tiene un ticket pendiente
    if get_pending_payment(chat_id):
        return jsonify({"error": "Ya tienes una solicitud pendiente. Cáncelala antes de crear otra."}), 400

    # Calcular monto según plan y promo
    user = get_user(chat_id)
    monto_usdt = PRECIOS[plan]["usdt"]
    if user and user.get('promo_end') and datetime.fromisoformat(user['promo_end']) > datetime.utcnow():
        monto_usdt = PROMO_DESCUENTO

    # Llamar a Heleket para crear factura (simulado)
    # En producción: requests.post a Heleket API
    invoice_id = str(uuid.uuid4())
    direccion_pago = USDT_WALLET  # Heleket genera una dirección única, aquí simplificamos

    # Guardar ticket pendiente
    create_pending_payment(
        chat_id=chat_id,
        plan=plan,
        metodo="usdt",
        telefono=None,
        monto=monto_usdt,
        tarjeta_destino=None,
        invoice_id=invoice_id
    )

    return jsonify({
        "invoice_id": invoice_id,
        "address": direccion_pago,
        "amount": monto_usdt,
        "network": USDT_NETWORK,
        "expires": (datetime.utcnow() + timedelta(minutes=30)).isoformat()
    })

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

# ------------------ WebApp completa (HTML/CSS/JS) ------------------
@app.route('/webapp')
def webapp():
    html = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="heleket" content="c88b07b1" />
        <title>Bot Descargador · WebApp</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            body {
                background: linear-gradient(145deg, #0f1219, #1a1e2b);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                font-family: 'Segoe UI', Roboto, system-ui, sans-serif;
                color: #e0e0e0;
                padding: 16px;
            }
            .app-container {
                max-width: 1200px;
                width: 100%;
                position: relative;
            }
            /* Logos flotantes de fondo */
            .floating-logos {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                pointer-events: none;
                z-index: 0;
                opacity: 0.1;
            }
            .floating-logos i {
                position: absolute;
                font-size: 4rem;
                color: #fff;
                filter: drop-shadow(0 0 20px rgba(100,150,255,0.5));
                animation: float 20s infinite ease-in-out;
            }
            @keyframes float {
                0% { transform: translateY(0) rotate(0deg); }
                50% { transform: translateY(-30px) rotate(5deg); }
                100% { transform: translateY(0) rotate(0deg); }
            }
            .fl1 { top: 10%; left: 5%; animation-delay: 0s; }
            .fl2 { top: 70%; left: 85%; animation-delay: 2s; }
            .fl3 { top: 40%; left: 15%; animation-delay: 4s; }
            .fl4 { top: 80%; left: 10%; animation-delay: 6s; }
            .fl5 { top: 20%; left: 80%; animation-delay: 8s; }
            .fl6 { top: 60%; left: 40%; animation-delay: 10s; }

            /* Tarjeta principal */
            .card {
                background: rgba(26, 30, 43, 0.8);
                backdrop-filter: blur(10px);
                border-radius: 48px;
                padding: 40px 32px;
                box-shadow: 0 25px 50px -12px rgba(0,0,0,0.8), inset 0 0 0 1px rgba(255,255,255,0.05);
                border: 1px solid rgba(255,255,255,0.02);
                position: relative;
                z-index: 1;
                width: 100%;
            }
            h1 {
                text-align: center;
                font-size: 3rem;
                font-weight: 700;
                letter-spacing: 2px;
                margin-bottom: 16px;
                background: linear-gradient(135deg, #c0c0c0, #f0f0f0, #c0c0c0);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                text-shadow: 0 2px 5px rgba(0,0,0,0.5);
            }
            .subtitle {
                text-align: center;
                color: #9ca3af;
                margin-bottom: 48px;
                font-size: 1.1rem;
            }
            /* Botones 3D */
            .button-group {
                display: flex;
                flex-wrap: wrap;
                justify-content: center;
                gap: 24px;
                margin-bottom: 40px;
            }
            .btn-3d {
                background: #2a2f3f;
                border: none;
                border-radius: 20px;
                box-shadow: 0 12px 0 #151a24, 0 15px 25px rgba(0,0,0,0.5);
                color: white;
                font-size: 1.5rem;
                font-weight: bold;
                padding: 18px 36px;
                cursor: pointer;
                transition: all 0.08s linear;
                text-transform: uppercase;
                letter-spacing: 1px;
                min-width: 200px;
                position: relative;
                border: 1px solid rgba(255,255,255,0.1);
            }
            .btn-3d:active {
                transform: translateY(12px);
                box-shadow: 0 2px 0 #151a24, 0 8px 15px rgba(0,0,0,0.5);
            }
            .btn-premium {
                background: linear-gradient(145deg, #3a3145, #4a3f5a);
                box-shadow: 0 12px 0 #2a2330, 0 15px 25px rgba(0,0,0,0.5);
            }
            .btn-admin {
                background: linear-gradient(145deg, #1f3a3a, #2a4a4a);
                box-shadow: 0 12px 0 #142525, 0 15px 25px rgba(0,0,0,0.5);
            }
            /* Info usuario */
            .user-info {
                background: rgba(20, 24, 35, 0.7);
                border-radius: 32px;
                padding: 24px;
                margin-bottom: 32px;
                display: flex;
                flex-wrap: wrap;
                justify-content: space-between;
                align-items: center;
                border: 1px solid rgba(255,215,0,0.2);
                backdrop-filter: blur(5px);
            }
            .plan-badge {
                background: #3a4050;
                padding: 8px 20px;
                border-radius: 40px;
                font-weight: bold;
                border: 1px solid gold;
                box-shadow: 0 0 15px gold;
            }
            .video-count {
                color: #a0aec0;
            }
            /* Panel de administración (solo visible para admin) */
            .admin-panel {
                background: rgba(10, 20, 30, 0.8);
                border-radius: 32px;
                padding: 24px;
                margin-top: 32px;
                border: 1px solid #00aaff;
                display: none;
            }
            .admin-panel.visible {
                display: block;
            }
            .stats-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 16px;
                margin: 16px 0;
            }
            .stat-card {
                background: #1e2433;
                padding: 16px;
                border-radius: 20px;
                text-align: center;
            }
            .stat-value {
                font-size: 1.8rem;
                font-weight: bold;
                color: gold;
            }
            .pending-list {
                margin-top: 20px;
            }
            .pending-item {
                background: #252b3b;
                padding: 12px;
                border-radius: 16px;
                margin: 8px 0;
                display: flex;
                justify-content: space-between;
            }
            .btn-small {
                background: #3a4050;
                border: none;
                border-radius: 12px;
                padding: 6px 12px;
                color: white;
                cursor: pointer;
                box-shadow: 0 4px 0 #1f232e;
                transition: 0.05s linear;
            }
            .btn-small:active {
                transform: translateY(4px);
                box-shadow: 0 0 0 #1f232e;
            }
            /* Responsive */
            @media (max-width: 600px) {
                .btn-3d { font-size: 1.2rem; padding: 14px 24px; min-width: 150px; }
                h1 { font-size: 2rem; }
            }
        </style>
    </head>
    <body>
        <div class="floating-logos">
            <i class="fl1">📹</i> <!-- Reemplazar con logos reales (imágenes) -->
            <i class="fl2">🎵</i>
            <i class="fl3">📺</i>
            <i class="fl4">🎬</i>
            <i class="fl5">📱</i>
            <i class="fl6">🎮</i>
        </div>
        <div class="app-container">
            <div class="card">
                <h1>⚡ DOWNLOAD BOT</h1>
                <div class="subtitle">Descarga videos de 1000+ sitios · Planes flexibles</div>

                <div class="user-info" id="userInfo">
                    <span>Cargando...</span>
                </div>

                <div class="button-group" id="mainButtons">
                    <button class="btn-3d" id="btnBasico">BÁSICO</button>
                    <button class="btn-3d btn-premium" id="btnPremium">PREMIUM</button>
                    <button class="btn-3d btn-admin" id="btnAdmin" style="display: none;">ADMIN</button>
                </div>

                <div id="paymentPanel" style="display: none;"></div>
                <div id="adminPanel" class="admin-panel"></div>
            </div>
        </div>

        <script>
            // Variables globales
            const TELEGRAM_INIT_DATA = '{{ telegram_user }}'; // si quieres pasar datos desde el backend
            let chatId = null;
            let userPlan = 'free';
            let videosUsed = 0;
            let planLimit = 5;
            let isAdmin = false;

            // Simular obtención de chatId (en producción lo pasarías desde el backend)
            async function getChatId() {
                // Si la webapp se abre desde Telegram, window.Telegram.WebApp.initData
                if (window.Telegram && Telegram.WebApp) {
                    const initData = Telegram.WebApp.initData;
                    // Aquí podrías parsear y obtener el chat_id si lo incluyes en la URL
                    // Por simplicidad, pedimos al usuario que ingrese su ID (solo demo)
                    let id = prompt("Ingresa tu ID de Telegram (chat_id) para vincular:");
                    if (id) chatId = parseInt(id);
                } else {
                    // Modo prueba
                    chatId = parseInt(prompt("Ingresa tu chat_id (modo prueba):") || "0");
                }
                if (chatId) {
                    await fetchUserData();
                }
            }

            async function fetchUserData() {
                try {
                    // Llamar a un endpoint que devuelva datos del usuario (necesitas crearlo)
                    // Por ahora simulamos
                    const res = await fetch(`/api/user/${chatId}`);
                    if (res.ok) {
                        const data = await res.json();
                        userPlan = data.plan;
                        videosUsed = data.videos_used;
                        planLimit = data.limit;
                        isAdmin = (chatId === {{ admin_chat_id }});
                        updateUI();
                    }
                } catch (e) {
                    console.error(e);
                }
            }

            function updateUI() {
                const userDiv = document.getElementById('userInfo');
                userDiv.innerHTML = `
                    <span>👤 ID: ${chatId}</span>
                    <span class="plan-badge">${userPlan.toUpperCase()}</span>
                    <span class="video-count">📊 ${videosUsed}/${planLimit} descargas</span>
                `;
                if (isAdmin) {
                    document.getElementById('btnAdmin').style.display = 'inline-block';
                    cargarPanelAdmin();
                }
            }

            async function cargarPanelAdmin() {
                const res = await fetch('/api/admin/stats');
                const stats = await res.json();
                let html = '<h2>👑 Panel Admin</h2><div class="stats-grid">';
                html += `<div class="stat-card"><div>Usuarios</div><div class="stat-value">${stats.total_users}</div></div>`;
                html += `<div class="stat-card"><div>Hoy</div><div class="stat-value">${stats.today_income} CUP</div></div>`;
                html += `<div class="stat-card"><div>Semana</div><div class="stat-value">${stats.week_income} CUP</div></div>`;
                html += `<div class="stat-card"><div>Mes</div><div class="stat-value">${stats.month_income} CUP</div></div>`;
                html += `<div class="stat-card"><div>Pendientes</div><div class="stat-value">${stats.pending_tickets}</div></div>`;
                html += '</div><div class="pending-list" id="pendingList"></div>';
                document.getElementById('adminPanel').innerHTML = html;
                document.getElementById('adminPanel').classList.add('visible');
                // Cargar tickets pendientes
                const pendRes = await fetch('/api/admin/pending-payments');
                const pendientes = await pendRes.json();
                let listHtml = '<h3>Tickets pendientes:</h3>';
                pendientes.forEach(p => {
                    listHtml += `<div class="pending-item">#${p.id} - ${p.plan} - ${p.amount} CUP - <button class="btn-small" onclick="completarManual(${p.id})">✅ Completar</button></div>`;
                });
                document.getElementById('pendingList').innerHTML = listHtml;
            }

            window.onload = getChatId;

            // Botones de planes
            document.getElementById('btnBasico').addEventListener('click', () => iniciarPago('basico'));
            document.getElementById('btnPremium').addEventListener('click', () => iniciarPago('premium'));

            async function iniciarPago(plan) {
                if (!chatId) return alert("Primero identifícate (ingresa chat_id)");
                // Verificar si ya tiene solicitud pendiente
                const pend = await fetch(`/api/pending/${chatId}`).then(r=>r.json());
                if (pend.exists) {
                    if (confirm("Ya tienes una solicitud pendiente. ¿Quieres cancelarla y crear una nueva?")) {
                        await fetch('/api/cancel-request', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({chat_id:chatId})});
                    } else {
                        return;
                    }
                }
                // Mostrar opciones de pago (simulado)
                const metodo = prompt("Elige método: 1-Tarjeta (CUP), 2-Saldo móvil (CUP), 3-USDT");
                if (metodo === '1') {
                    // Mostrar instrucciones tarjeta
                    alert(`Transfiere ${plan==='basico'?250:600} CUP a la tarjeta 9234567890123456. Luego ingresa tu número de teléfono.`);
                    let telefono = prompt("Número de teléfono usado en Transfermóvil:");
                    if (telefono) {
                        await fetch('/api/create-payment-ticket', {
                            method:'POST',
                            body: JSON.stringify({chat_id:chatId, plan, metodo:'tarjeta', telefono})
                        });
                        alert("Ticket creado. Espera la confirmación.");
                    }
                } else if (metodo === '2') {
                    alert(`Recarga ${plan==='basico'?120:300} CUP al número 51234567 (o el que corresponda). Luego ingresa tu número.`);
                    let telefono = prompt("Tu número de teléfono (desde donde enviaste el saldo):");
                    if (telefono) {
                        await fetch('/api/create-payment-ticket', {method:'POST', body: JSON.stringify({chat_id:chatId, plan, metodo:'saldo', telefono})});
                        alert("Ticket creado.");
                    }
                } else if (metodo === '3') {
                    // Crear factura Heleket
                    const res = await fetch('/api/create-invoice', {method:'POST', body: JSON.stringify({chat_id:chatId, plan})});
                    const inv = await res.json();
                    if (inv.error) {
                        alert(inv.error);
                    } else {
                        alert(`Paga ${inv.amount} USDT (BEP20) a la dirección: ${inv.address}\nInvoice ID: ${inv.invoice_id}\nLa factura expira en 30 minutos.`);
                    }
                }
            }
        </script>
    </body>
    </html>
    """
    # Reemplazar variables
    html = html.replace("{{ admin_chat_id }}", str(ADMIN_CHAT_ID))
    return render_template_string(html)

# Endpoints auxiliares para la webapp
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
    # Verificar pendiente
    if get_pending_payment(chat_id):
        return jsonify({"error": "Ya tienes una solicitud pendiente"}), 400
    monto = PRECIOS[plan][metodo]  # 'tarjeta' o 'saldo'
    create_pending_payment(chat_id, plan, metodo, telefono, monto, TARJETA_NUMERO)
    return jsonify({"status": "ok"})

@app.route('/api/admin/stats')
def api_admin_stats():
    # Aquí deberías verificar si es admin (por ip o token simple). Por simplicidad omitimos.
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

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    if not user:
        promo_end = datetime.utcnow() + timedelta(hours=24)
        create_user(chat_id, promo_end)
        # Verificar referido
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
        [InlineKeyboardButton("🌐 WebApp", url=f"https://{request.host}/webapp")],
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
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

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
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async_todos mostrar_referidos(query, context):
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
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def mostrar_soporte(query):
    texto = (
        "🆘 *Soporte*\n\n"
        "Si tienes problemas, escribe tu consulta aquí mismo y un administrador te responderá a la mayor brevedad.\n\n"
        "También puedes hacer una donación voluntaria a nuestra wallet USDT (BEP20):\n"
        f"`{USDT_WALLET}`\n\n"
        "¡Gracias por apoyar el proyecto! 💙"
    )
    keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]]
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def iniciar_pago_bot(query, context):
    plan = query.data.split('_')[1]  # 'basico' o 'premium'
    context.user_data['plan_seleccionado'] = plan
    texto = "¿Desde dónde vas a pagar?"
    keyboard = [
        [InlineKeyboardButton("🇨🇺 Cuba", callback_data="pais_cuba")],
        [InlineKeyboardButton("🌍 Otro país", callback_data="pais_ext")],
        [InlineKeyboardButton("🔙 Volver", callback_data="planes")]
    ]
    await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(keyboard))

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
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        # Extranjero: solo USDT
        await procesar_pago_usdt(query, context)

async def seleccionar_metodo(query, context):
    metodo = query.data.split('_')[1]
    context.user_data['metodo'] = metodo
    plan = context.user_data['plan_seleccionado']
    chat_id = query.from_user.id
    user = get_user(chat_id)

    # Verificar si ya tiene ticket pendiente
    if get_pending_payment(chat_id):
        keyboard = [[InlineKeyboardButton("❌ Cancelar solicitud anterior", callback_data="cancelar_solicitud")],
                    [InlineKeyboardButton("🔙 Volver", callback_data="pagar_" + plan)]]
        await query.edit_message_text("⚠️ Ya tienes una solicitud pendiente. Cáncelala antes de crear una nueva.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Calcular monto según promo
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
            f"**Número de teléfono destino:** `51234567` (cambiar por el tuyo)\n\n"
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
    if user.get('promo_end') and datetime.fromisoformat(user['promo_end']) > datetime.utcnow():
        monto_usdt = PROMO_DESCUENTO

    # Crear factura Heleket (simulado)
    invoice_id = str(uuid.uuid4())
    # En producción llamarías a Heleket API
    create_pending_payment(chat_id, plan, "usdt", None, monto_usdt, invoice_id=invoice_id)

    texto = (
        f"💎 *Pago en USDT (BEP20)*\n\n"
        f"Plan: *{plan.upper()}*\n"
        f"Monto: *{monto_usdt} USDT*\n"
        f"Red: *BEP20*\n\n"
        f"**Dirección:**\n`{USDT_WALLET}`\n\n"
        f"**Invoice ID:** `{invoice_id}`\n\n"
        f"Envía exactamente *{monto_usdt} USDT* a la dirección mostrada.\n"
        f"El pago será verificado automáticamente en pocos minutos."
    )
    keyboard = [[InlineKeyboardButton("✅ Ya transferí", callback_data="verificar_usdt")],
                [InlineKeyboardButton("🔙 Volver", callback_data="planes")]]
    await query.edit_message_text(texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def cancelar_solicitud(query, context):
    chat_id = query.from_user.id
    supabase.table("payments").update({"status": "cancelled"}).eq("chat_id", chat_id).eq("status", "pending").execute()
    await query.edit_message_text("✅ Solicitud cancelada. Puedes crear una nueva.")

async def volver_inicio(query):
    await start(query.message, None)  # Reutilizar start, pero query.message no es Update, habría que adaptar
    # Alternativa simple:
    keyboard = [
        [InlineKeyboardButton("📦 Planes", callback_data="planes"),
         InlineKeyboardButton("🎁 Ventajas", callback_data="ventajas")],
        [InlineKeyboardButton("👥 Referidos", callback_data="referidos"),
         InlineKeyboardButton("🆘 Soporte", callback_data="soporte")],
        [InlineKeyboardButton("🌐 WebApp", url=f"https://{request.host}/webapp")],
    ]
    await query.edit_message_text("¡Bienvenido de nuevo!", reply_markup=InlineKeyboardMarkup(keyboard))

# Handler para recibir el teléfono del usuario
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

# Handlers de descargas (similares a versiones anteriores, con verificación de plan)
# ... (omitido por brevedad, pero puedes incorporar la lógica de get_format_list y get_direct_url_with_format con PO Token)

# Registrar handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_telefono))
# Aquí irían los handlers de descarga

# ================== SCHEDULER ==================
scheduler = BackgroundScheduler()

def check_promo_reminders():
    # Buscar usuarios con promo próxima a expirar
    users = supabase.table("users").select("*").not_.is_("promo_end", "null").execute().data
    now = datetime.utcnow()
    for u in users:
        end = datetime.fromisoformat(u['promo_end'])
        diff = (end - now).total_seconds() / 3600
        if 4.5 < diff < 5.5 and not u.get('notified_5h'):
            enviar_notificacion(u['chat_id'], "⏳ ¡Solo quedan 5 horas para tu descuento del 75% en Premium!")
            supabase.table("users").update({"notified_5h": True}).eq("chat_id", u['chat_id']).execute()
        elif 0.9 < diff < 1.1 and not u.get('notified_1h'):
            enviar_notificacion(u['chat_id'], "⚠️ ¡Última hora! Tu descuento expira en 1 hora.")
            supabase.table("users").update({"notified_1h": True}).eq("chat_id", u['chat_id']).execute()
        elif 0.4 < diff < 0.6 and not u.get('notified_30m'):
            enviar_notificacion(u['chat_id'], "⏰ ¡30 minutos! Apresúrate.")
            supabase.table("users").update({"notified_30m": True}).eq("chat_id", u['chat_id']).execute()
        elif 0.1 < diff < 0.2 and not u.get('notified_10m'):
            enviar_notificacion(u['chat_id'], "🔥 ¡10 minutos! Último aviso.")
            supabase.table("users").update({"notified_10m": True}).eq("chat_id", u['chat_id']).execute()
        elif diff <= 0 and not u.get('notified_expired'):
            enviar_notificacion(u['chat_id'], "⌛ Tu promoción ha expirado. Pero aún puedes contratar planes regulares.")
            supabase.table("users").update({"notified_expired": True}).eq("chat_id", u['chat_id']).execute()

def keepalive_job():
    requests.get(f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'localhost')}/keepalive", timeout=5)

scheduler.add_job(check_promo_reminders, 'interval', hours=1)
scheduler.add_job(keepalive_job, 'interval', minutes=5)
scheduler.start()

# ================== MAIN ==================
def run_bot():
    application.run_polling()

if __name__ == "__main__":
    # Iniciar bot en hilo
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    # Iniciar Flask
    app.run(host='0.0.0.0', port=PORT, debug=False)
