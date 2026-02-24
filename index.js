const express = require('express');
const { Telegraf, Markup } = require('telegraf');
const { createClient } = require('@supabase/supabase-js');
const fetch = require('node-fetch');
const cron = require('node-cron');
const { v4: uuidv4 } = require('uuid');
const path = require('path');
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
const TARJETA_NUMERO = "9234567890123456"; // cámbialo
const USDT_WALLET = "0xTuWalletBEP20";
const USDT_NETWORK = "BEP20";

const PRECIOS = {
  basico: { tarjeta: 250, saldo: 120, usdt: 0.5 },
  premium: { tarjeta: 600, saldo: 300, usdt: 1.0 },
};
const PROMO_DESCUENTO = 0.75; // 75% descuento
const REFERIDO_DESC_BASICO = 10;
const REFERIDO_DESC_PREMIUM = 15;

// ================== SUPABASE ==================
const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

// Funciones de BD
async function getUser(chatId) {
  const { data } = await supabase.from('users').select('*').eq('chat_id', chatId).single();
  return data;
}

async function createUser(chatId, promoEnd = null, referralCode = null) {
  const { error } = await supabase.from('users').insert({
    chat_id: chatId,
    plan: 'free',
    videos_used: 0,
    reset_date: new Date(Date.now() + 86400000).toISOString(),
    referral_code: referralCode || uuidv4().slice(0, 8),
    promo_end: promoEnd?.toISOString(),
    discount_next_month: 0,
    notified_5h: false,
    notified_1h: false,
    notified_30m: false,
    notified_10m: false,
    notified_expired: false,
  });
  if (error) console.error('Error createUser:', error);
}

async function updateUser(chatId, updates) {
  await supabase.from('users').update(updates).eq('chat_id', chatId);
}

async function getUserByReferral(code) {
  const { data } = await supabase.from('users').select('*').eq('referral_code', code).single();
  return data;
}

async function getPendingPayment(chatId) {
  const { data } = await supabase
    .from('payments')
    .select('*')
    .eq('chat_id', chatId)
    .eq('status', 'pending')
    .single();
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
    metadata: {
      telefono,
      tarjeta_destino: tarjetaDestino,
      invoice_id: invoiceId,
    },
  });
  if (error) console.error('Error createPendingPayment:', error);
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
  const { data } = await query.single();
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
    const descuento = user.plan === 'basico' ? REFERIDO_DESC_BASICO : REFERIDO_DESC_PREMIUM;
    const referrer = await getUser(user.referrer_id);
    if (referrer) {
      const nuevoDescuento = (referrer.discount_next_month || 0) + descuento;
      await updateUser(user.referrer_id, { discount_next_month: nuevoDescuento });
    }
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
  const today = new Date().setHours(0, 0, 0, 0);
  const weekAgo = new Date(Date.now() - 7 * 86400000);
  const monthAgo = new Date(Date.now() - 30 * 86400000);

  const todayIncome = payments
    .filter(p => new Date(p.completed_at) >= today)
    .reduce((s, p) => s + p.amount, 0);
  const weekIncome = payments
    .filter(p => new Date(p.completed_at) >= weekAgo)
    .reduce((s, p) => s + p.amount, 0);
  const monthIncome = payments
    .filter(p => new Date(p.completed_at) >= monthAgo)
    .reduce((s, p) => s + p.amount, 0);
  const pendingCount = (await supabase.from('payments').select('*', { count: 'exact', head: true }).eq('status', 'pending')).count;

  return {
    total_users: users.length,
    today_income: todayIncome,
    week_income: weekIncome,
    month_income: monthIncome,
    pending_tickets: pendingCount,
  };
}

// ================== BOT DE TELEGRAM ==================
const bot = new Telegraf(TELEGRAM_TOKEN);

// Comando /start
bot.start(async (ctx) => {
  const chatId = ctx.chat.id;
  let user = await getUser(chatId);
  if (!user) {
    const promoEnd = new Date(Date.now() + 24 * 3600000);
    await createUser(chatId, promoEnd);
    // Referido
    const refCode = ctx.payload;
    if (refCode && refCode.startsWith('ref_')) {
      const referrer = await getUserByReferral(refCode.slice(4));
      if (referrer) {
        await updateUser(chatId, { referrer_id: referrer.chat_id });
      }
    }
    user = await getUser(chatId);
  }

  const texto = `
👋 *¡Bienvenido al Bot Descargador!*

📊 *Tu plan:* \`${user.plan.toUpperCase()}\`
📥 *Descargas usadas:* ${user.videos_used}/${getLimit(user.plan)} (${getPeriod(user.plan)})
🎁 *Promoción:* Si eres nuevo, tienes 24h para probar Premium con 75% de descuento (solo 0.75 USDT o equivalente).

Envía un enlace para comenzar a descargar.
  `;

  const keyboard = Markup.inlineKeyboard([
    [Markup.button.callback('📦 Planes y Precios', 'planes')],
    [Markup.button.callback('🎁 Ventajas', 'ventajas'), Markup.button.callback('👥 Referidos', 'referidos')],
    [Markup.button.callback('🆘 Soporte', 'soporte')],
    [Markup.button.url('🌐 WebApp', `${BASE_URL}/webapp`)],
  ]);

  await ctx.replyWithMarkdown(texto, keyboard);
});

// Manejo de callbacks
bot.on('callback_query', async (ctx) => {
  const data = ctx.callbackQuery.data;
  const query = ctx.callbackQuery;
  const chatId = query.from.id;

  if (data === 'planes') {
    const texto = `
*📦 Planes disponibles:*

🆓 *Gratuito*
• 5 descargas/día
• Redes sociales y sitios públicos

⚡ *Básico* – 250 CUP/mes (tarjeta) | 120 CUP (saldo) | 0.50 USDT
• 100 descargas/mes
• Redes sociales + sitios básicos

💎 *Premium* – 600 CUP/mes (tarjeta) | 300 CUP (saldo) | 1 USDT
• 1000 descargas/mes
• YouTube incluido (con tecnología PO Token)
• Acceso a todos los sitios soportados

🎁 *Promoción nuevos usuarios:* Premium por solo 0.75 USDT (450 CUP tarjeta / 225 CUP saldo) durante las primeras 24h.

Selecciona un plan para pagar:
    `;
    const keyboard = Markup.inlineKeyboard([
      [Markup.button.callback('⚡ Básico', 'pagar_basico'), Markup.button.callback('💎 Premium', 'pagar_premium')],
      [Markup.button.callback('🔙 Volver', 'volver_inicio')],
    ]);
    await ctx.editMessageText(texto, { parse_mode: 'Markdown', ...keyboard });
  }

  else if (data === 'ventajas') {
    const texto = `
*🎁 Ventajas de usar nuestro bot:*

✅ Descarga desde más de 1000 sitios (YouTube, TikTok, Instagram, Facebook, Twitter, Vimeo, etc.)
✅ Calidad seleccionable (hasta 4K)
✅ Sin anuncios ni límites molestos (según plan)
✅ Activación automática de pagos por Transfermóvil, Cubacel y USDT (BEP20)
✅ Soporte rápido por Telegram
✅ WebApp elegante para gestionar tu cuenta
✅ Promociones y descuentos por referidos

✨ *¡Únete y empieza a descargar!*
    `;
    const keyboard = Markup.inlineKeyboard([[Markup.button.callback('🔙 Volver', 'volver_inicio')]]);
    await ctx.editMessageText(texto, { parse_mode: 'Markdown', ...keyboard });
  }

  else if (data === 'referidos') {
    const user = await getUser(chatId);
    const codigo = user.referral_code;
    const texto = `
👥 *Sistema de Referidos*

Comparte tu código con amigos y gana descuentos:

\`${codigo}\`

🔗 Enlace: \`https://t.me/${bot.botInfo.username}?start=ref_${codigo}\`

*Recompensas:*
• Por cada amigo que contrate Básico → 10% descuento en tu próximo mes.
• Por cada amigo que contrate Premium → 15% descuento.
¡Acumulable!
    `;
    const keyboard = Markup.inlineKeyboard([[Markup.button.callback('🔙 Volver', 'volver_inicio')]]);
    await ctx.editMessageText(texto, { parse_mode: 'Markdown', ...keyboard });
  }

  else if (data === 'soporte') {
    const texto = `
🆘 *Soporte*

Si tienes problemas, escribe tu consulta aquí mismo y un administrador te responderá a la mayor brevedad.

También puedes hacer una donación voluntaria a nuestra wallet USDT (BEP20):
\`${USDT_WALLET}\`

¡Gracias por apoyar el proyecto! 💙
    `;
    const keyboard = Markup.inlineKeyboard([[Markup.button.callback('🔙 Volver', 'volver_inicio')]]);
    await ctx.editMessageText(texto, { parse_mode: 'Markdown', ...keyboard });
  }

  else if (data.startsWith('pagar_')) {
    const plan = data.split('_')[1];
    ctx.session = ctx.session || {};
    ctx.session.plan = plan;
    const texto = '¿Desde dónde vas a pagar?';
    const keyboard = Markup.inlineKeyboard([
      [Markup.button.callback('🇨🇺 Cuba', 'pais_cuba')],
      [Markup.button.callback('🌍 Otro país', 'pais_ext')],
      [Markup.button.callback('🔙 Volver', 'planes')],
    ]);
    await ctx.editMessageText(texto, keyboard);
  }

  else if (data === 'pais_cuba') {
    const texto = 'Elige método de pago (CUP):';
    const keyboard = Markup.inlineKeyboard([
      [Markup.button.callback('💳 Tarjeta (Transfermóvil)', 'metodo_tarjeta')],
      [Markup.button.callback('📱 Saldo móvil (Cubacel)', 'metodo_saldo')],
      [Markup.button.callback('🔙 Volver', `pagar_${ctx.session.plan}`)],
    ]);
    await ctx.editMessageText(texto, keyboard);
  }

  else if (data === 'pais_ext') {
    await procesarPagoUSDT(ctx);
  }

  else if (data === 'metodo_tarjeta' || data === 'metodo_saldo') {
    const metodo = data.split('_')[1];
    const plan = ctx.session.plan;
    const user = await getUser(chatId);
    const montoTarjeta = PRECIOS[plan].tarjeta;
    const montoSaldo = PRECIOS[plan].saldo;

    let monto = metodo === 'tarjeta' ? montoTarjeta : montoSaldo;
    if (user.promo_end && new Date(user.promo_end) > new Date()) {
      monto = Math.floor(monto * PROMO_DESCUENTO);
    }

    if (await getPendingPayment(chatId)) {
      const keyboard = Markup.inlineKeyboard([
        [Markup.button.callback('❌ Cancelar solicitud anterior', 'cancelar_solicitud')],
        [Markup.button.callback('🔙 Volver', `pagar_${plan}`)],
      ]);
      await ctx.editMessageText('⚠️ Ya tienes una solicitud pendiente. Cáncelala antes de crear una nueva.', keyboard);
      return;
    }

    ctx.session.metodo = metodo;
    ctx.session.monto = monto;

    if (metodo === 'tarjeta') {
      const texto = `
💳 *Pago por Transfermóvil*

Plan: *${plan.toUpperCase()}*
Monto a pagar: *${monto} CUP*

**Número de tarjeta:** \`${TARJETA_NUMERO}\`

📌 *Instrucciones:*
1. Abre Transfermóvil y selecciona 'Transferencia' a la tarjeta mostrada.
2. **Importante:** Activa la casilla *'Mostrar número al destinatario'* antes de confirmar.
3. Realiza el pago y toma una captura (por si acaso).

⚠️ *Los pagos por EnZona no se detectan automáticamente. Si pagas por EnZona, envía una captura a soporte.*

Luego, escribe tu número de teléfono (el que usaste para pagar) para verificar.
      `;
      await ctx.editMessageText(texto, { parse_mode: 'Markdown' });
      ctx.session.esperandoTelefono = 'tarjeta';
    } else {
      const texto = `
📱 *Pago por Saldo Móvil (Cubacel)*

Plan: *${plan.toUpperCase()}*
Monto a pagar: *${monto} CUP*

**Número de teléfono destino:** \`51234567\` (cambiar por el tuyo)

📌 *Instrucciones:*
1. Realiza una transferencia de saldo desde tu móvil al número indicado.
2. Espera el SMS de confirmación.
3. Toma una captura por si acaso.

Luego, escribe tu número de teléfono (desde donde enviaste el saldo).
      `;
      await ctx.editMessageText(texto, { parse_mode: 'Markdown' });
      ctx.session.esperandoTelefono = 'saldo';
    }
  }

  else if (data === 'cancelar_solicitud') {
    await supabase.from('payments').update({ status: 'cancelled' }).eq('chat_id', chatId).eq('status', 'pending');
    await ctx.editMessageText('✅ Solicitud cancelada. Puedes crear una nueva.');
  }

  else if (data === 'volver_inicio') {
    const keyboard = Markup.inlineKeyboard([
      [Markup.button.callback('📦 Planes', 'planes'), Markup.button.callback('🎁 Ventajas', 'ventajas')],
      [Markup.button.callback('👥 Referidos', 'referidos'), Markup.button.callback('🆘 Soporte', 'soporte')],
      [Markup.button.url('🌐 WebApp', `${BASE_URL}/webapp`)],
    ]);
    await ctx.editMessageText('¡Bienvenido de nuevo!', keyboard);
  }
});

async function procesarPagoUSDT(ctx) {
  const plan = ctx.session.plan;
  const chatId = ctx.chat.id;
  const user = await getUser(chatId);
  let montoUSDT = PRECIOS[plan].usdt;
  if (user.promo_end && new Date(user.promo_end) > new Date()) {
    montoUSDT = PROMO_DESCUENTO;
  }

  const invoiceId = uuidv4();
  await createPendingPayment(chatId, plan, 'usdt', null, montoUSDT, null, invoiceId);

  const texto = `
💎 *Pago en USDT (BEP20)*

Plan: *${plan.toUpperCase()}*
Monto: *${montoUSDT} USDT*
Red: *${USDT_NETWORK}*

**Dirección:**
\`${USDT_WALLET}\`

**Invoice ID:** \`${invoiceId}\`

Envía exactamente *${montoUSDT} USDT* a la dirección mostrada.
El pago será verificado automáticamente en pocos minutos.
  `;
  const keyboard = Markup.inlineKeyboard([
    [Markup.button.callback('✅ Ya transferí', 'verificar_usdt')],
    [Markup.button.callback('🔙 Volver', 'planes')],
  ]);
  await ctx.editMessageText(texto, { parse_mode: 'Markdown', ...keyboard });
}

// Escuchar mensajes de texto (teléfono)
bot.on('text', async (ctx) => {
  if (!ctx.session?.esperandoTelefono) return;
  const metodo = ctx.session.esperandoTelefono;
  const telefono = ctx.message.text.trim();
  if (!/^\d{8,}$/.test(telefono)) {
    await ctx.reply('❌ Número inválido. Debe tener al menos 8 dígitos. Intenta de nuevo:');
    return;
  }
  const chatId = ctx.chat.id;
  const plan = ctx.session.plan;
  const monto = ctx.session.monto;

  if (metodo === 'tarjeta') {
    await createPendingPayment(chatId, plan, 'tarjeta', telefono, monto, TARJETA_NUMERO);
    await ctx.replyWithMarkdown(
      '✅ ¡Ticket de pago creado!\n\nEn cuanto detectemos tu pago (normalmente en pocos minutos), se activará tu plan automáticamente.\nRecibirás una notificación cuando esté listo.\n\nPuedes ver el estado en /mis_pagos'
    );
  } else {
    await createPendingPayment(chatId, plan, 'saldo', telefono, monto);
    await ctx.reply('✅ Ticket creado. Espera la confirmación.');
  }
  delete ctx.session.esperandoTelefono;
});

// Comando /mis_pagos (simplificado)
bot.command('mis_pagos', async (ctx) => {
  const { data } = await supabase
    .from('payments')
    .select('*')
    .eq('chat_id', ctx.chat.id)
    .order('created_at', { ascending: false });
  if (!data.length) {
    await ctx.reply('No tienes pagos registrados.');
    return;
  }
  let msg = '*Tus pagos:*\n';
  data.forEach(p => {
    msg += `\n- ${p.plan_purchased} | ${p.amount} ${p.currency} | ${p.status} | ${new Date(p.created_at).toLocaleString()}`;
  });
  await ctx.replyWithMarkdown(msg);
});

// Lanzar bot
bot.launch();

// ================== EXPRESS ==================
const app = express();
app.use(express.json());

// Meta tag para Heleket en la raíz
app.get('/', (req, res) => {
  res.send(`
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="heleket" content="c88b07b1" />
        <title>Bot Descargador</title>
    </head>
    <body>
        <h1>Bot Descargador - API</h1>
        <p>WebApp en <a href="/webapp">/webapp</a></p>
    </body>
    </html>
  `);
});

// Servir WebApp estática
app.use('/webapp', express.static(path.join(__dirname, 'public')));

// Webhook para pagos desde tu servicio Flask
app.post('/payment-webhook', async (req, res) => {
  const auth = req.headers['x-auth-token'];
  if (!auth || auth !== PAYMENT_WEBHOOK_TOKEN) return res.status(401).json({ error: 'Unauthorized' });

  const { type, data } = req.body;
  if (type === 'TRANSFERMOVIL_PAGO') {
    const telefono = data.telefono_origen;
    const monto = data.monto;
    const tarjetaDestino = data.tarjeta_destino || req.body.card_number;
    const transId = data.trans_id;

    const ticket = await findPendingPayment({ method: 'tarjeta', telefono, monto, tarjeta: tarjetaDestino });
    if (ticket) {
      await activatePlan(ticket.chat_id, ticket.plan_purchased);
      await completePayment(ticket.id, transId);
      await aplicarDescuentoReferido(ticket.chat_id);
      // Notificar
      await bot.telegram.sendMessage(ticket.chat_id, `✅ ¡Pago recibido! Tu plan *${ticket.plan_purchased.toUpperCase()}* está activado.`, { parse_mode: 'Markdown' });
      return res.json({ status: 'ok' });
    }
  } else if (type === 'CUBACEL_SALDO_RECIBIDO') {
    const remitente = data.remitente;
    const monto = data.monto;
    const transId = data.trans_id || `CUBACEL_${Date.now()}`;

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
  if (!auth || auth !== `Bearer ${HELEKET_API_KEY}`) return res.status(401).json({ error: 'Unauthorized' });

  const { invoice_id, status, metadata } = req.body;
  if (status === 'paid' && invoice_id) {
    const ticket = await findPendingPayment({ method: 'usdt', invoiceId: invoice_id });
    if (ticket) {
      await activatePlan(ticket.chat_id, ticket.plan_purchased);
      await completePayment(ticket.id, invoice_id);
      await aplicarDescuentoReferido(ticket.chat_id);
      await bot.telegram.sendMessage(ticket.chat_id, `✅ ¡Pago USDT recibido! Tu plan *${ticket.plan_purchased.toUpperCase()}* está activado.`, { parse_mode: 'Markdown' });
      return res.json({ status: 'ok' });
    }
  }
  res.json({ status: 'ignored' });
});

// API para la WebApp
app.get('/api/user/:chatId', async (req, res) => {
  const user = await getUser(parseInt(req.params.chatId));
  if (!user) return res.status(404).json({ error: 'User not found' });
  res.json({
    plan: user.plan,
    videos_used: user.videos_used,
    limit: getLimit(user.plan),
  });
});

app.get('/api/pending/:chatId', async (req, res) => {
  const pend = await getPendingPayment(parseInt(req.params.chatId));
  res.json({ exists: !!pend });
});

app.post('/api/create-invoice', async (req, res) => {
  const { chat_id, plan } = req.body;
  if (!chat_id || !plan) return res.status(400).json({ error: 'Faltan datos' });
  if (await getPendingPayment(chat_id)) {
    return res.status(400).json({ error: 'Ya tienes una solicitud pendiente' });
  }
  const user = await getUser(chat_id);
  let montoUSDT = PRECIOS[plan].usdt;
  if (user?.promo_end && new Date(user.promo_end) > new Date()) montoUSDT = PROMO_DESCUENTO;

  const invoiceId = uuidv4();
  await createPendingPayment(chat_id, plan, 'usdt', null, montoUSDT, null, invoiceId);

  res.json({
    invoice_id: invoiceId,
    address: USDT_WALLET,
    amount: montoUSDT,
    network: USDT_NETWORK,
    expires: new Date(Date.now() + 30 * 60000).toISOString(),
  });
});

app.post('/api/create-payment-ticket', async (req, res) => {
  const { chat_id, plan, metodo, telefono } = req.body;
  if (!chat_id || !plan || !metodo || !telefono) return res.status(400).json({ error: 'Faltan datos' });
  if (await getPendingPayment(chat_id)) {
    return res.status(400).json({ error: 'Ya tienes una solicitud pendiente' });
  }
  const monto = PRECIOS[plan][metodo];
  await createPendingPayment(chat_id, plan, metodo, telefono, monto, metodo === 'tarjeta' ? TARJETA_NUMERO : null);
  res.json({ status: 'ok' });
});

app.post('/api/cancel-request', async (req, res) => {
  const { chat_id } = req.body;
  await supabase.from('payments').update({ status: 'cancelled' }).eq('chat_id', chat_id).eq('status', 'pending');
  res.json({ status: 'ok' });
});

app.get('/api/admin/stats', async (req, res) => {
  const stats = await getAdminStats();
  res.json(stats);
});

app.get('/api/admin/pending-payments', async (req, res) => {
  const { data } = await supabase.from('payments').select('*').eq('status', 'pending');
  res.json(data);
});

// Keepalive
app.get('/keepalive', (req, res) => res.send('OK'));

// ================== SCHEDULER (recordatorios) ==================
cron.schedule('0 * * * *', async () => { // cada hora
  const { data: users } = await supabase.from('users').select('*').not('promo_end', 'is', null);
  const now = new Date();
  for (const u of users) {
    const end = new Date(u.promo_end);
    const diffHours = (end - now) / 3600000;
    if (diffHours > 4.5 && diffHours < 5.5 && !u.notified_5h) {
      await bot.telegram.sendMessage(u.chat_id, '⏳ ¡Solo quedan 5 horas para tu descuento del 75% en Premium!');
      await updateUser(u.chat_id, { notified_5h: true });
    } else if (diffHours > 0.9 && diffHours < 1.1 && !u.notified_1h) {
      await bot.telegram.sendMessage(u.chat_id, '⚠️ ¡Última hora! Tu descuento expira en 1 hora.');
      await updateUser(u.chat_id, { notified_1h: true });
    } else if (diffHours > 0.4 && diffHours < 0.6 && !u.notified_30m) {
      await bot.telegram.sendMessage(u.chat_id, '⏰ ¡30 minutos! Apresúrate.');
      await updateUser(u.chat_id, { notified_30m: true });
    } else if (diffHours > 0.1 && diffHours < 0.2 && !u.notified_10m) {
      await bot.telegram.sendMessage(u.chat_id, '🔥 ¡10 minutos! Último aviso.');
      await updateUser(u.chat_id, { notified_10m: true });
    } else if (diffHours <= 0 && !u.notified_expired) {
      await bot.telegram.sendMessage(u.chat_id, '⌛ Tu promoción ha expirado. Pero aún puedes contratar planes regulares.');
      await updateUser(u.chat_id, { notified_expired: true });
    }
  }
});

// ================== INICIAR SERVIDOR ==================
app.listen(PORT, () => {
  console.log(`Servidor escuchando en puerto ${PORT}`);
});
