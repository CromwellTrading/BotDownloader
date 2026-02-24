const express = require('express');
const { Telegraf, Markup } = require('telegraf');
const { createClient } = require('@supabase/supabase-js');
const ytdl = require('yt-dlp-exec');
const axios = require('axios');
const { v4: uuidv4 } = require('uuid');
const cron = require('node-cron');
const path = require('path');
const crypto = require('crypto');
require('dotenv').config();

// ================== CONFIGURACIÓN ==================
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_KEY;
const ADMIN_CHAT_ID = parseInt(process.env.ADMIN_CHAT_ID);
const PAYMENT_WEBHOOK_TOKEN = process.env.PAYMENT_WEBHOOK_TOKEN;
const HELEKET_MERCHANT_UUID = process.env.HELEKET_MERCHANT_UUID;
const HELEKET_API_KEY = process.env.HELEKET_API_KEY;
const PORT = process.env.PORT || 8080;
const BASE_URL = process.env.RENDER_EXTERNAL_URL || `http://localhost:${PORT}`;

// Datos de pago
const TARJETA_NUMERO = "9234567890123456"; // Cambiar por tu tarjeta
const USDT_WALLET = "0xTuWalletBEP20";      // Cambiar por tu wallet BEP20
const USDT_NETWORK = "BEP20";
const NUMERO_SALDO = "51234567";             // Cambiar por tu número para saldo

// Precios
const PRECIOS = {
  basico: { tarjeta: 250, saldo: 120, usdt: 0.5 },
  premium: { tarjeta: 600, saldo: 300, usdt: 1.0 }
};
const PROMO_DESCUENTO = 0.75; // 75% descuento primeros usuarios
const REFERIDO_DESC_BASICO = 10;
const REFERIDO_DESC_PREMIUM = 15;

// ================== SUPABASE CLIENT ==================
const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

// Funciones DB
async function getUser(chatId) {
  const { data } = await supabase.from('users').select('*').eq('chat_id', chatId).maybeSingle();
  return data;
}

async function createUser(chatId, promoEnd = null, referralCode = null) {
  const code = referralCode || uuidv4().slice(0, 8);
  const { error } = await supabase.from('users').insert({
    chat_id: chatId,
    plan: 'free',
    videos_used: 0,
    reset_date: new Date(Date.now() + 86400000).toISOString(),
    referral_code: code,
    promo_end: promoEnd,
    discount_next_month: 0
  });
  if (error) console.error('Error creating user:', error);
}

async function updateUser(chatId, updates) {
  await supabase.from('users').update(updates).eq('chat_id', chatId);
}

async function getUserByReferral(code) {
  const { data } = await supabase.from('users').select('*').eq('referral_code', code).maybeSingle();
  return data;
}

async function getPendingPayment(chatId) {
  const { data } = await supabase
    .from('payments')
    .select('*')
    .eq('chat_id', chatId)
    .eq('status', 'pending')
    .maybeSingle();
  return data;
}

async function createPendingPayment(chatId, plan, method, telefono, monto, tarjetaDestino = null, invoiceId = null) {
  const { error } = await supabase.from('payments').insert({
    chat_id: chatId,
    plan_purchased: plan,
    method,
    amount: monto,
    currency: method === 'usdt' ? 'USDT' : 'CUP',
    status: 'pending',
    metadata: { telefono, tarjeta_destino: tarjetaDestino, invoice_id: invoiceId }
  });
  if (error) console.error('Error creating payment:', error);
}

async function completePayment(paymentId, transId) {
  await supabase
    .from('payments')
    .update({ status: 'completed', trans_id: transId, completed_at: new Date().toISOString() })
    .eq('id', paymentId);
}

async function findPendingPayment({ method, telefono, monto, tarjeta, invoiceId }) {
  let query = supabase.from('payments').select('*').eq('status', 'pending').eq('method', method);
  if (telefono) query = query.filter('metadata->>telefono', 'eq', telefono);
  if (monto) query = query.eq('amount', monto);
  if (tarjeta) query = query.filter('metadata->>tarjeta_destino', 'eq', tarjeta);
  if (invoiceId) query = query.filter('metadata->>invoice_id', 'eq', invoiceId);
  const { data } = await query.maybeSingle();
  return data;
}

async function activatePlan(chatId, plan) {
  const resetDays = plan === 'free' ? 1 : 30;
  const resetDate = new Date(Date.now() + resetDays * 86400000).toISOString();
  await updateUser(chatId, { plan, videos_used: 0, reset_date: resetDate });
}

async function aplicarDescuentoReferido(chatId) {
  const user = await getUser(chatId);
  if (user?.referrer_id) {
    const referrer = await getUser(user.referrer_id);
    const descuento = user.plan_purchased === 'basico' ? REFERIDO_DESC_BASICO : REFERIDO_DESC_PREMIUM;
    const nuevoDescuento = (referrer.discount_next_month || 0) + descuento;
    await updateUser(user.referrer_id, { discount_next_month: nuevoDescuento });
  }
}

function getLimit(plan) {
  const limits = { free: 5, basico: 100, premium: 1000 };
  return limits[plan] || 5;
}

function getPeriod(plan) {
  return plan === 'free' ? 'día' : 'mes';
}

async function getAdminStats() {
  const { data: users } = await supabase.from('users').select('*');
  const { data: payments } = await supabase.from('payments').select('*').eq('status', 'completed');
  const now = new Date();
  const today = now.toISOString().split('T')[0];
  const weekAgo = new Date(now - 7 * 86400000).toISOString();
  const monthAgo = new Date(now - 30 * 86400000).toISOString();

  const todayIncome = payments.filter(p => p.completed_at?.startsWith(today)).reduce((s, p) => s + p.amount, 0);
  const weekIncome = payments.filter(p => p.completed_at >= weekAgo).reduce((s, p) => s + p.amount, 0);
  const monthIncome = payments.filter(p => p.completed_at >= monthAgo).reduce((s, p) => s + p.amount, 0);
  const { count: pending } = await supabase.from('payments').select('*', { count: 'exact', head: true }).eq('status', 'pending');

  return {
    total_users: users.length,
    today_income: todayIncome,
    week_income: weekIncome,
    month_income: monthIncome,
    pending_tickets: pending
  };
}

// ================== EXPRESS SERVER ==================
const app = express();
app.use(express.json());

// Página principal con meta tag para Heleket
app.get('/', (req, res) => {
  res.send(`
    <!DOCTYPE html>
    <html>
      <head>
        <meta name="heleket" content="c88b07b1" />
        <title>Bot Descargador</title>
        <style>
          body { background: #1a1e2b; color: #e0e0e0; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; }
          h1 { background: linear-gradient(135deg, #b9b9b9, #e5e5e5, #b9b9b9); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        </style>
      </head>
      <body>
        <h1>⚡ Bot Descargador</h1>
        <p>WebApp en <a href="/webapp">/webapp</a></p>
      </body>
    </html>
  `);
});

// Webapp completa (HTML embebido para simplificar)
app.get('/webapp', (req, res) => {
  res.sendFile(path.join(__dirname, 'webapp.html'));
});

// Webhook para pagos desde tu servicio Flask (Transfermóvil/Cubacel)
app.post('/payment-webhook', async (req, res) => {
  const authToken = req.headers['x-auth-token'];
  if (!authToken || !crypto.timingSafeEqual(Buffer.from(authToken), Buffer.from(PAYMENT_WEBHOOK_TOKEN))) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  const data = req.body;
  if (data.type === 'TRANSFERMOVIL_PAGO') {
    const pago = data.data;
    const telefono = pago.telefono_origen;
    const monto = pago.monto;
    const tarjeta = pago.tarjeta_destino || data.card_number;
    const transId = pago.trans_id;

    const ticket = await findPendingPayment({ method: 'tarjeta', telefono, monto, tarjeta });
    if (ticket) {
      await activatePlan(ticket.chat_id, ticket.plan_purchased);
      await completePayment(ticket.id, transId);
      await aplicarDescuentoReferido(ticket.chat_id);
      // Notificar al usuario
      await bot.telegram.sendMessage(ticket.chat_id, `✅ ¡Pago recibido! Tu plan *${ticket.plan_purchased.toUpperCase()}* está activado.`, { parse_mode: 'Markdown' });
      return res.json({ status: 'ok' });
    }
  } else if (data.type === 'CUBACEL_SALDO_RECIBIDO') {
    const pago = data.data;
    const remitente = pago.remitente;
    const monto = pago.monto;
    const transId = pago.trans_id || `CUBACEL_${Date.now()}`;

    const ticket = await findPendingPayment({ method: 'saldo', telefono: remitente, monto });
    if (ticket) {
      await activatePlan(ticket.chat_id, ticket.plan_purchased);
      await completePayment(ticket.id, transId);
      await aplicarDescuentoReferido(ticket.chat_id);
      await bot.telegram.sendMessage(ticket.chat_id, `✅ ¡Pago recibido! Tu plan *${ticket.plan_purchased.toUpperCase()}* está activado.`, { parse_mode: 'Markdown' });
      return res.json({ status: 'ok' });
    }
  }
  res.json({ status: 'ignored' });
});

// Webhook para Heleket
app.post('/heleket-webhook', async (req, res) => {
  const auth = req.headers.authorization;
  if (!auth || auth !== `Bearer ${HELEKET_API_KEY}`) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  const data = req.body;
  const invoiceId = data.invoice_id;
  const status = data.status;
  if (status === 'paid' && invoiceId) {
    const ticket = await findPendingPayment({ method: 'usdt', invoiceId });
    if (ticket) {
      await activatePlan(ticket.chat_id, ticket.plan_purchased);
      await completePayment(ticket.id, invoiceId);
      await aplicarDescuentoReferido(ticket.chat_id);
      await bot.telegram.sendMessage(ticket.chat_id, `✅ ¡Pago USDT recibido! Tu plan *${ticket.plan_purchased.toUpperCase()}* está activado.`, { parse_mode: 'Markdown' });
      return res.json({ status: 'ok' });
    }
  }
  res.json({ status: 'ignored' });
});

// Endpoints para webapp
app.get('/api/user/:chatId', async (req, res) => {
  const user = await getUser(parseInt(req.params.chatId));
  if (!user) return res.status(404).json({ error: 'User not found' });
  res.json({
    plan: user.plan,
    videos_used: user.videos_used,
    limit: getLimit(user.plan)
  });
});

app.get('/api/pending/:chatId', async (req, res) => {
  const pend = await getPendingPayment(parseInt(req.params.chatId));
  res.json({ exists: !!pend });
});

app.post('/api/create-payment-ticket', async (req, res) => {
  const { chat_id, plan, metodo, telefono } = req.body;
  if (!chat_id || !plan || !metodo || !telefono) {
    return res.status(400).json({ error: 'Missing data' });
  }
  if (await getPendingPayment(chat_id)) {
    return res.status(400).json({ error: 'Ya tienes una solicitud pendiente' });
  }
  const monto = PRECIOS[plan][metodo]; // 'tarjeta' o 'saldo'
  await createPendingPayment(chat_id, plan, metodo, telefono, monto, TARJETA_NUMERO);
  res.json({ status: 'ok' });
});

app.post('/api/create-invoice', async (req, res) => {
  const { chat_id, plan } = req.body;
  if (!chat_id || !plan) return res.status(400).json({ error: 'Missing data' });

  const user = await getUser(chat_id);
  let montoUsdt = PRECIOS[plan].usdt;
  if (user?.promo_end && new Date(user.promo_end) > new Date()) {
    montoUsdt = PROMO_DESCUENTO;
  }

  // Aquí llamarías a Heleket API para crear factura real
  // Simulamos:
  const invoiceId = uuidv4();
  await createPendingPayment(chat_id, plan, 'usdt', null, montoUsdt, null, invoiceId);

  res.json({
    invoice_id: invoiceId,
    address: USDT_WALLET,
    amount: montoUsdt,
    network: USDT_NETWORK,
    expires: new Date(Date.now() + 30 * 60000).toISOString()
  });
});

app.post('/api/cancel-request', async (req, res) => {
  const { chat_id } = req.body;
  await supabase.from('payments').update({ status: 'cancelled' }).eq('chat_id', chat_id).eq('status', 'pending');
  res.json({ status: 'ok' });
});

app.get('/api/admin/stats', async (req, res) => {
  res.json(await getAdminStats());
});

app.get('/api/admin/pending-payments', async (req, res) => {
  const { data } = await supabase.from('payments').select('*').eq('status', 'pending');
  res.json(data);
});

// ================== BOT DE TELEGRAM ==================
const bot = new Telegraf(TELEGRAM_TOKEN);

// Middleware para session (usamos Map simple)
const session = new Map();
bot.use((ctx, next) => {
  if (!ctx.from) return next();
  const chatId = ctx.from.id;
  ctx.session = session.get(chatId) || {};
  ctx.saveSession = () => session.set(chatId, ctx.session);
  return next();
});

bot.start(async (ctx) => {
  const chatId = ctx.chat.id;
  let user = await getUser(chatId);
  if (!user) {
    const promoEnd = new Date(Date.now() + 24 * 3600000).toISOString();
    await createUser(chatId, promoEnd);
    // Referido
    const ref = ctx.startPayload;
    if (ref && ref.startsWith('ref_')) {
      const referrer = await getUserByReferral(ref.slice(4));
      if (referrer) {
        await updateUser(chatId, { referrer_id: referrer.chat_id });
      }
    }
    user = await getUser(chatId);
  }

  const texto = `👋 *¡Bienvenido al Bot Descargador!*\n\n📊 *Tu plan:* \`${user.plan.toUpperCase()}\`\n📥 *Descargas usadas:* ${user.videos_used}/${getLimit(user.plan)} (${getPeriod(user.plan)})\n🎁 *Promoción:* Si eres nuevo, tienes 24h para probar Premium con 75% de descuento.\n\nEnvía un enlace para comenzar.`;
  const keyboard = Markup.inlineKeyboard([
    [Markup.button.callback('📦 Planes', 'planes'), Markup.button.callback('🎁 Ventajas', 'ventajas')],
    [Markup.button.callback('👥 Referidos', 'referidos'), Markup.button.callback('🆘 Soporte', 'soporte')],
    [Markup.button.url('🌐 WebApp', `${BASE_URL}/webapp`)]
  ]);
  await ctx.reply(texto, { parse_mode: 'Markdown', ...keyboard });
});

bot.action('planes', async (ctx) => {
  const texto = `*📦 Planes disponibles:*\n\n🆓 *Gratuito*\n• 5 descargas/día\n• Redes sociales y sitios públicos\n\n⚡ *Básico* – 250 CUP/mes (tarjeta) | 120 CUP (saldo) | 0.50 USDT\n• 100 descargas/mes\n• Redes sociales + sitios básicos\n\n💎 *Premium* – 600 CUP/mes (tarjeta) | 300 CUP (saldo) | 1 USDT\n• 1000 descargas/mes\n• YouTube incluido\n• Acceso a todos los sitios\n\n🎁 *Promoción nuevos:* Premium por solo 0.75 USDT (450 CUP / 225 CUP saldo) primeras 24h.\n\nSelecciona:`;
  const keyboard = Markup.inlineKeyboard([
    [Markup.button.callback('⚡ Básico', 'pagar_basico'), Markup.button.callback('💎 Premium', 'pagar_premium')],
    [Markup.button.callback('🔙 Volver', 'volver_inicio')]
  ]);
  await ctx.editMessageText(texto, { parse_mode: 'Markdown', ...keyboard });
});

bot.action('ventajas', async (ctx) => {
  const texto = `*🎁 Ventajas:*\n\n✅ Descarga desde más de 1000 sitios\n✅ Calidad seleccionable\n✅ Sin anuncios\n✅ Pagos automáticos por Transfermóvil, Cubacel y USDT\n✅ Soporte rápido\n✅ WebApp elegante\n✅ Descuentos por referidos`;
  await ctx.editMessageText(texto, { parse_mode: 'Markdown', reply_markup: Markup.inlineKeyboard([Markup.button.callback('🔙 Volver', 'volver_inicio')]) });
});

bot.action('referidos', async (ctx) => {
  const user = await getUser(ctx.from.id);
  const codigo = user.referral_code;
  const texto = `👥 *Sistema de Referidos*\n\nComparte tu código:\n\`${codigo}\`\n🔗 https://t.me/${bot.botInfo.username}?start=ref_${codigo}\n\n*Recompensas:*\n• Básico → 10% descuento\n• Premium → 15% descuento\n¡Acumulable!`;
  await ctx.editMessageText(texto, { parse_mode: 'Markdown', reply_markup: Markup.inlineKeyboard([Markup.button.callback('🔙 Volver', 'volver_inicio')]) });
});

bot.action('soporte', async (ctx) => {
  const texto = `🆘 *Soporte*\n\nEscribe tu consulta aquí mismo y un admin responderá.\n\nDonaciones voluntarias USDT (BEP20):\n\`${USDT_WALLET}\`\n¡Gracias!`;
  await ctx.editMessageText(texto, { parse_mode: 'Markdown', reply_markup: Markup.inlineKeyboard([Markup.button.callback('🔙 Volver', 'volver_inicio')]) });
});

// Flujo de pagos
bot.action(/pagar_(.+)/, async (ctx) => {
  const plan = ctx.match[1]; // basico o premium
  ctx.session.plan = plan;
  ctx.saveSession();
  const keyboard = Markup.inlineKeyboard([
    [Markup.button.callback('🇨🇺 Cuba', 'pais_cuba'), Markup.button.callback('🌍 Otro país', 'pais_ext')],
    [Markup.button.callback('🔙 Volver', 'planes')]
  ]);
  await ctx.editMessageText('¿Desde dónde vas a pagar?', keyboard);
});

bot.action('pais_cuba', async (ctx) => {
  const keyboard = Markup.inlineKeyboard([
    [Markup.button.callback('💳 Tarjeta (Transfermóvil)', 'metodo_tarjeta'),
     Markup.button.callback('📱 Saldo móvil (Cubacel)', 'metodo_saldo')],
    [Markup.button.callback('🔙 Volver', `pagar_${ctx.session.plan}`)]
  ]);
  await ctx.editMessageText('Elige método de pago (CUP):', keyboard);
});

bot.action('pais_ext', async (ctx) => {
  // Redirige a USDT
  await procesarPagoUsdt(ctx);
});

bot.action(/metodo_(.+)/, async (ctx) => {
  const metodo = ctx.match[1]; // 'tarjeta' o 'saldo'
  ctx.session.metodo = metodo;
  ctx.session.monto = ctx.session.plan === 'basico' ? PRECIOS.basico[metodo] : PRECIOS.premium[metodo];

  // Verificar si ya tiene pendiente
  const pending = await getPendingPayment(ctx.from.id);
  if (pending) {
    const keyboard = Markup.inlineKeyboard([
      Markup.button.callback('❌ Cancelar solicitud anterior', 'cancelar_solicitud'),
      Markup.button.callback('🔙 Volver', `pagar_${ctx.session.plan}`)
    ]);
    return ctx.editMessageText('⚠️ Ya tienes una solicitud pendiente. Cáncelala antes de crear una nueva.', keyboard);
  }

  let texto = '';
  if (metodo === 'tarjeta') {
    texto = `💳 *Pago por Transfermóvil*\n\nPlan: *${ctx.session.plan.toUpperCase()}*\nMonto: *${ctx.session.monto} CUP*\n\n**Número de tarjeta:** \`${TARJETA_NUMERO}\`\n\n📌 *Instrucciones:*\n1. Abre Transfermóvil y selecciona 'Transferencia' a la tarjeta.\n2. Activa *'Mostrar número al destinatario'* antes de confirmar.\n3. Realiza el pago.\n⚠️ *EnZona no se detecta automático. Si pagas por EnZona, envía captura a soporte.*\n\nLuego, escribe tu número de teléfono.`;
  } else {
    texto = `📱 *Pago por Saldo Móvil (Cubacel)*\n\nPlan: *${ctx.session.plan.toUpperCase()}*\nMonto: *${ctx.session.monto} CUP*\n\n**Número destino:** \`${NUMERO_SALDO}\`\n\n📌 *Instrucciones:*\n1. Transfiere saldo al número indicado.\n2. Espera SMS.\n3. Luego escribe tu número de teléfono.`;
  }
  ctx.session.esperandoTelefono = metodo;
  ctx.saveSession();
  await ctx.editMessageText(texto, { parse_mode: 'Markdown' });
});

// Recibir teléfono
bot.on('text', async (ctx) => {
  if (!ctx.session.esperandoTelefono) return;
  const metodo = ctx.session.esperandoTelefono;
  const telefono = ctx.message.text.trim();
  if (!/^\d{8,}$/.test(telefono)) {
    return ctx.reply('❌ Número inválido. Debe tener al menos 8 dígitos. Intenta de nuevo:');
  }
  const chatId = ctx.from.id;
  const plan = ctx.session.plan;
  const monto = ctx.session.monto;

  await createPendingPayment(chatId, plan, metodo, telefono, monto, metodo === 'tarjeta' ? TARJETA_NUMERO : null);
  delete ctx.session.esperandoTelefono;
  ctx.saveSession();
  await ctx.reply('✅ ¡Ticket de pago creado! En cuanto detectemos el pago, se activará tu plan.');
});

async function procesarPagoUsdt(ctx) {
  const plan = ctx.session.plan;
  const chatId = ctx.from.id;
  const user = await getUser(chatId);
  let montoUsdt = PRECIOS[plan].usdt;
  if (user?.promo_end && new Date(user.promo_end) > new Date()) {
    montoUsdt = PROMO_DESCUENTO;
  }

  // Crear factura en Heleket (simulado)
  const invoiceId = uuidv4();
  await createPendingPayment(chatId, plan, 'usdt', null, montoUsdt, null, invoiceId);

  const texto = `💎 *Pago en USDT (BEP20)*\n\nPlan: *${plan.toUpperCase()}*\nMonto: *${montoUsdt} USDT*\nRed: *BEP20*\n\n**Dirección:**\n\`${USDT_WALLET}\`\n\n**Invoice ID:** \`${invoiceId}\`\n\nEnvía exactamente *${montoUsdt} USDT* a la dirección. Se verificará automáticamente.`;
  const keyboard = Markup.inlineKeyboard([
    Markup.button.callback('✅ Ya transferí', 'verificar_usdt'),
    Markup.button.callback('🔙 Volver', 'planes')
  ]);
  await ctx.editMessageText(texto, { parse_mode: 'Markdown', ...keyboard });
}

bot.action('cancelar_solicitud', async (ctx) => {
  await supabase.from('payments').update({ status: 'cancelled' }).eq('chat_id', ctx.from.id).eq('status', 'pending');
  await ctx.editMessageText('✅ Solicitud cancelada.');
});

bot.action('volver_inicio', async (ctx) => {
  const keyboard = Markup.inlineKeyboard([
    [Markup.button.callback('📦 Planes', 'planes'), Markup.button.callback('🎁 Ventajas', 'ventajas')],
    [Markup.button.callback('👥 Referidos', 'referidos'), Markup.button.callback('🆘 Soporte', 'soporte')],
    [Markup.button.url('🌐 WebApp', `${BASE_URL}/webapp`)]
  ]);
  await ctx.editMessageText('¡Bienvenido de nuevo!', keyboard);
});

// Comando /admin
bot.command('admin', async (ctx) => {
  if (ctx.chat.id !== ADMIN_CHAT_ID) return;
  const stats = await getAdminStats();
  const texto = `👑 *Panel de Administración*\n\n👥 Usuarios: ${stats.total_users}\n💰 Hoy: ${stats.today_income} CUP\n💰 Semana: ${stats.week_income} CUP\n💰 Mes: ${stats.month_income} CUP\n⏳ Pendientes: ${stats.pending_tickets}`;
  const keyboard = Markup.inlineKeyboard([
    [Markup.button.callback('📋 Ver tickets', 'admin_tickets'),
     Markup.button.callback('💰 Pagos', 'admin_pagos')]
  ]);
  await ctx.reply(texto, { parse_mode: 'Markdown', ...keyboard });
});

// ================== LÓGICA DE DESCARGA CON YT-DLP ==================
async function getFormatList(url, chatId) {
  const user = await getUser(chatId);
  if (!user) throw new Error('Usuario no encontrado');

  // Verificar límite
  if (user.videos_used >= getLimit(user.plan)) {
    throw new Error(`Has alcanzado tu límite de ${getLimit(user.plan)} descargas.`);
  }

  // Opciones de yt-dlp
  const args = ['-J', '--no-playlist']; // salida JSON, sin playlists
  // Si es YouTube y plan premium, podríamos añadir PO Token (simulado)
  // En producción usarías --extractor-args "youtube:po_token=..."

  const output = await ytdl(url, args);
  const info = JSON.parse(output);

  const formats = info.formats
    .filter(f => f.vcodec !== 'none' || f.acodec !== 'none')
    .map(f => ({
      format_id: f.format_id,
      quality: f.format_note || (f.height ? `${f.height}p` : 'audio'),
      ext: f.ext,
      filesize: f.filesize || f.filesize_approx
    }));

  return {
    title: info.title,
    duration: info.duration,
    formats: formats.slice(0, 10) // limitar a 10 opciones
  };
}

async function getDirectUrl(url, formatId, chatId) {
  const args = ['-g', '-f', formatId, '--no-playlist'];
  // Aquí también podrías añadir PO Token
  const directUrl = await ytdl(url, args);
  return directUrl.trim();
}

// Manejador de mensajes con enlaces
bot.on('text', async (ctx) => {
  if (ctx.session.esperandoTelefono) return; // ya manejado
  const url = ctx.message.text.trim();
  if (!url.startsWith('http')) return;

  const chatId = ctx.from.id;
  const user = await getUser(chatId);
  if (!user) return ctx.reply('Primero usa /start');

  // Verificar si puede descargar (límite)
  if (user.videos_used >= getLimit(user.plan)) {
    return ctx.reply(`❌ Has alcanzado tu límite de ${getLimit(user.plan)} descargas. Mejora tu plan.`);
  }

  await ctx.reply('⏳ Analizando enlace...');

  try {
    const { title, duration, formats } = await getFormatList(url, chatId);
    if (!formats.length) {
      return ctx.reply('❌ No se encontraron formatos descargables.');
    }

    // Guardar en sesión
    ctx.session.url = url;
    ctx.session.formats = formats;
    ctx.session.title = title;
    ctx.saveSession();

    const durationStr = duration ? `${Math.floor(duration / 60)}:${(duration % 60).toString().padStart(2, '0')}` : 'desconocida';
    let text = `📹 *${title}*\n⏱️ Duración: ${durationStr}\n\nElige calidad:`;
    const buttons = formats.map((f, i) => [Markup.button.callback(`${f.quality} - ${f.ext}${f.filesize ? ` (${Math.round(f.filesize / 1048576)} MB)` : ''}`, `format_${i}`)]);
    await ctx.reply(text, { parse_mode: 'Markdown', reply_markup: Markup.inlineKeyboard(buttons) });
  } catch (err) {
    console.error(err);
    await ctx.reply(`❌ Error: ${err.message.slice(0, 200)}`);
  }
});

bot.action(/format_(\d+)/, async (ctx) => {
  const index = parseInt(ctx.match[1]);
  const { url, formats, title } = ctx.session;
  if (!url || !formats || index >= formats.length) {
    return ctx.editMessageText('❌ Sesión expirada. Envía el enlace de nuevo.');
  }

  const format = formats[index];
  await ctx.editMessageText(`⏳ Obteniendo enlace para ${format.quality}...`);

  try {
    const directUrl = await getDirectUrl(url, format.format_id, ctx.from.id);

    // Incrementar contador de descargas
    const user = await getUser(ctx.from.id);
    await updateUser(ctx.from.id, { videos_used: (user.videos_used || 0) + 1 });

    // Enviar botón con URL invisible (usamos carácter zero-width space)
    const invisible = '\u200b';
    const text = `✅ *${title}* (${format.quality}) listo. Presiona el botón:\n[${invisible}](${directUrl})`;
    const keyboard = Markup.inlineKeyboard([Markup.button.url('📥 Descargar', directUrl)]);
    await ctx.editMessageText(text, { parse_mode: 'Markdown', ...keyboard });
  } catch (err) {
    await ctx.editMessageText(`❌ Error: ${err.message.slice(0, 200)}`);
  }
});

// ================== CRON JOBS ==================
// Recordatorios de promoción (cada hora)
cron.schedule('0 * * * *', async () => {
  const { data: users } = await supabase.from('users').select('*').not('promo_end', 'is', null);
  const now = new Date();
  for (const user of users) {
    const end = new Date(user.promo_end);
    const diffHours = (end - now) / 3600000;
    if (diffHours < 5 && diffHours > 4.5 && !user.notified_5h) {
      await bot.telegram.sendMessage(user.chat_id, '⏳ ¡Solo quedan 5 horas para tu descuento del 75% en Premium!');
      await updateUser(user.chat_id, { notified_5h: true });
    } else if (diffHours < 1 && diffHours > 0.9 && !user.notified_1h) {
      await bot.telegram.sendMessage(user.chat_id, '⚠️ ¡Última hora! Tu descuento expira en 1 hora.');
      await updateUser(user.chat_id, { notified_1h: true });
    } else if (diffHours < 0.5 && diffHours > 0.4 && !user.notified_30m) {
      await bot.telegram.sendMessage(user.chat_id, '⏰ ¡30 minutos! Apresúrate.');
      await updateUser(user.chat_id, { notified_30m: true });
    } else if (diffHours < 0.1 && diffHours > 0.05 && !user.notified_10m) {
      await bot.telegram.sendMessage(user.chat_id, '🔥 ¡10 minutos! Último aviso.');
      await updateUser(user.chat_id, { notified_10m: true });
    } else if (diffHours <= 0 && !user.notified_expired) {
      await bot.telegram.sendMessage(user.chat_id, '⌛ Tu promoción ha expirado. Aún puedes contratar planes regulares.');
      await updateUser(user.chat_id, { notified_expired: true });
    }
  }
});

// Keepalive cada 5 min
setInterval(() => {
  axios.get(`${BASE_URL}/keepalive`).catch(() => {});
}, 5 * 60 * 1000);

app.get('/keepalive', (req, res) => res.send('OK'));

// ================== INICIAR SERVIDOR Y BOT ==================
bot.launch().then(() => {
  console.log('Bot iniciado');
}).catch(err => {
  console.error('Error al iniciar bot:', err);
});

app.listen(PORT, () => {
  console.log(`Servidor web en puerto ${PORT}`);
});

// Graceful stop
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
