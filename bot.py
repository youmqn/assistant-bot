import asyncio
import json
import logging
import os
import hashlib
import requests
from datetime import datetime
import pytz
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from aiohttp import web

# ========================
# НАСТРОЙКИ
# ========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_HOST = "worker-production-f7e9.up.railway.app"

# Интервал проверки новостей (секунды)
NEWS_CHECK_INTERVAL = 300  # 5 минут

# ID пользователя
USER_CHAT_ID = None
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_CHAT_ID_FILE = os.path.join(BASE_DIR, "chat_id.txt")
BUDGET_FILE = os.path.join(BASE_DIR, "budget.json")
SEEN_NEWS_FILE = os.path.join(BASE_DIR, "seen_news.json")

# ========================
# ЛОГИРОВАНИЕ
# ========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========================
# БЮДЖЕТ
# ========================
DEFAULT_BUDGET = {
    "initial_deposit": 100.0,
    "current_balance": 100.0,
    "risk_per_trade": 0.02,  # 2% от баланса на сделку
    "trades": [],
    "total_trades": 0,
    "winning_trades": 0,
    "losing_trades": 0,
    "total_pnl": 0.0
}

def load_budget():
    env_budget = os.environ.get("BUDGET_DATA", "")
    if env_budget:
        try:
            return json.loads(env_budget)
        except:
            pass
    if os.path.exists(BUDGET_FILE):
        try:
            with open(BUDGET_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return DEFAULT_BUDGET.copy()

def save_budget(budget):
    try:
        with open(BUDGET_FILE, "w") as f:
            json.dump(budget, f, indent=2)
    except Exception as e:
        logger.warning(f"Не удалось сохранить бюджет: {e}")

# ========================
# ЗАГРУЗКА CHAT ID
# ========================
def load_chat_id():
    global USER_CHAT_ID
    env_chat_id = os.environ.get("USER_CHAT_ID", "")
    if env_chat_id:
        try:
            USER_CHAT_ID = int(env_chat_id)
            logger.info(f"Загружен chat_id из env: {USER_CHAT_ID}")
            return
        except:
            pass
    if os.path.exists(USER_CHAT_ID_FILE):
        with open(USER_CHAT_ID_FILE, "r") as f:
            try:
                USER_CHAT_ID = int(f.read().strip())
                logger.info(f"Загружен chat_id из файла: {USER_CHAT_ID}")
            except:
                pass

def save_chat_id(chat_id):
    global USER_CHAT_ID
    USER_CHAT_ID = chat_id
    try:
        with open(USER_CHAT_ID_FILE, "w") as f:
            f.write(str(chat_id))
    except Exception as e:
        logger.warning(f"Не удалось сохранить chat_id: {e}")

# ========================
# ОТСЛЕЖИВАНИЕ НОВОСТЕЙ (дубликаты)
# ========================
seen_news_hashes = set()

def load_seen_news():
    global seen_news_hashes
    if os.path.exists(SEEN_NEWS_FILE):
        try:
            with open(SEEN_NEWS_FILE, "r") as f:
                seen_news_hashes = set(json.load(f))
        except:
            seen_news_hashes = set()

def save_seen_news():
    try:
        # Храним только последние 500 хешей
        recent = list(seen_news_hashes)[-500:]
        with open(SEEN_NEWS_FILE, "w") as f:
            json.dump(recent, f)
    except:
        pass

def news_hash(title):
    return hashlib.md5(title.encode()).hexdigest()

def is_new_news(title):
    h = news_hash(title)
    if h in seen_news_hashes:
        return False
    seen_news_hashes.add(h)
    return True

# ========================
# ПОЛУЧЕНИЕ КРИПТО-НОВОСТЕЙ
# ========================
def get_crypto_news():
    """Получает свежие крипто/финансовые новости из нескольких источников."""
    all_news = []

    # CryptoPanic API (бесплатный, крипто-специфичный)
    try:
        url = "https://cryptopanic.com/api/free/v1/posts/"
        params = {"auth_token": "free", "filter": "important", "public": "true"}
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            for post in data.get("results", [])[:10]:
                title = post.get("title", "")
                url_link = post.get("url", "")
                source = post.get("source", {}).get("title", "")
                if title and is_new_news(title):
                    all_news.append({
                        "title": title,
                        "url": url_link,
                        "source": source,
                        "type": "crypto"
                    })
    except Exception as e:
        logger.error(f"Ошибка CryptoPanic: {e}")

    # NewsAPI — крипто и финансы
    try:
        url = "https://newsapi.org/v2/everything"
        params = {
            "apiKey": NEWS_API_KEY,
            "q": "bitcoin OR ethereum OR crypto OR cryptocurrency",
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10
        }
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            for article in data.get("articles", []):
                title = article.get("title", "")
                url_link = article.get("url", "")
                source = article.get("source", {}).get("name", "")
                if title and "[Removed]" not in title and is_new_news(title):
                    all_news.append({
                        "title": title,
                        "url": url_link,
                        "source": source,
                        "type": "finance"
                    })
    except Exception as e:
        logger.error(f"Ошибка NewsAPI: {e}")

    return all_news

# ========================
# ПОЛУЧЕНИЕ ЦЕН КРИПТО
# ========================
def get_crypto_prices():
    """Получает текущие цены BTC, ETH, SOL, BNB."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": "bitcoin,ethereum,solana,binancecoin",
            "vs_currencies": "usd",
            "include_24hr_change": "true"
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        prices = {}
        name_map = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "binancecoin": "BNB"}
        for coin_id, symbol in name_map.items():
            if coin_id in data:
                prices[symbol] = {
                    "price": data[coin_id].get("usd", 0),
                    "change_24h": data[coin_id].get("usd_24h_change", 0)
                }
        return prices
    except Exception as e:
        logger.error(f"Ошибка получения цен: {e}")
        return {}

# ========================
# AI АНАЛИЗ НОВОСТИ
# ========================
def analyze_news_for_trading(news_item, prices, budget):
    """AI анализирует новость и даёт торговую рекомендацию."""
    if not OPENROUTER_API_KEY:
        return None

    try:
        client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1"
        )

        prices_text = "\n".join([
            f"{sym}: ${p['price']:,.2f} ({p['change_24h']:+.1f}% за 24ч)"
            for sym, p in prices.items()
        ])

        trade_amount = round(budget["current_balance"] * budget["risk_per_trade"], 2)

        prompt = f"""Ты профессиональный крипто-трейдер и аналитик. Проанализируй новость и дай торговую рекомендацию.

НОВОСТЬ: {news_item['title']}
ИСТОЧНИК: {news_item.get('source', 'N/A')}

ТЕКУЩИЕ ЦЕНЫ:
{prices_text}

БЮДЖЕТ ТРЕЙДЕРА:
- Баланс: ${budget['current_balance']:.2f}
- Максимум на сделку (2%): ${trade_amount:.2f}
- Всего сделок: {budget['total_trades']} (W: {budget['winning_trades']} / L: {budget['losing_trades']})

ЗАДАНИЕ:
1. Оцени важность новости для рынка (1-10)
2. Определи какой актив затронет больше всего
3. Дай рекомендацию: LONG (покупка), SHORT (продажа) или SKIP (пропустить)
4. Если не SKIP — укажи:
   - Актив (BTC, ETH, SOL, BNB)
   - Направление (LONG/SHORT)
   - Рекомендуемый размер позиции ($)
   - Примерный Take Profit (%)
   - Примерный Stop Loss (%)
   - Уверенность (1-10)

ФОРМАТ ОТВЕТА (строго):
📊 *Анализ новости*

🔥 Важность: X/10
💰 Актив: [SYMBOL]
📈 Рекомендация: [LONG/SHORT/SKIP]
💵 Размер позиции: $XX
🎯 Take Profit: +X%
🛑 Stop Loss: -X%
📊 Уверенность: X/10

💡 *Объяснение:* [2-3 предложения почему]

Если новость незначительная или не влияет на рынок — рекомендуй SKIP и объясни почему.
Отвечай на русском."""

        response = client.chat.completions.create(
            model="nvidia/nemotron-3-super-120b-a12b:free",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка AI анализа: {e}")
        return None

# ========================
# AI ДЛЯ ВОПРОСОВ
# ========================
def answer_question(question, budget):
    if not OPENROUTER_API_KEY:
        return "OpenRouter API не настроен."
    try:
        client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1"
        )
        prompt = f"""Ты крипто-трейдер и аналитик. Отвечай на русском, кратко и по делу.

Бюджет пользователя: ${budget['current_balance']:.2f}
Всего сделок: {budget['total_trades']}
PnL: ${budget['total_pnl']:.2f}

Вопрос: {question}"""

        response = client.chat.completions.create(
            model="nvidia/nemotron-3-super-120b-a12b:free",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка AI: {e}")
        return "Ошибка при обработке запроса."

# ========================
# BYBIT ССЫЛКИ
# ========================
def get_bybit_link(symbol, direction):
    """Генерирует ссылку на Bybit для открытия позиции."""
    pair = f"{symbol}USDT"
    return f"https://www.bybit.com/trade/usdt/{pair}"

# ========================
# TELEGRAM ОБРАБОТЧИКИ
# ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    budget = load_budget()

    keyboard = [
        [InlineKeyboardButton("📊 Рынок сейчас", callback_data="market")],
        [InlineKeyboardButton("💰 Мой бюджет", callback_data="budget")],
        [InlineKeyboardButton("🔍 Проверить новости", callback_data="check_news")],
        [InlineKeyboardButton("⚙️ Настроить бюджет", callback_data="setup_budget")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🤖 *Крипто-Аналитик бот*\n\n"
        "Я слежу за рынком 24/7 и присылаю торговые рекомендации.\n\n"
        "📰 Мониторю крипто-новости каждые 5 минут\n"
        "🧠 AI анализирует влияние на рынок\n"
        "💰 Рассчитываю размер позиции по risk management\n"
        "🔗 Даю готовые ссылки на Bybit\n\n"
        f"💼 Текущий баланс: *${budget['current_balance']:.2f}*\n\n"
        "📋 *Команды:*\n"
        "/market — цены и рынок\n"
        "/budget — мой бюджет\n"
        "/set\\_budget 100 — установить бюджет\n"
        "/trade WIN 5.50 — записать прибыльную сделку\n"
        "/trade LOSS 3.20 — записать убыточную сделку\n"
        "/check — проверить новости сейчас\n"
        "/stats — статистика торговли",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю данные рынка...")
    prices = get_crypto_prices()
    if not prices:
        await update.message.reply_text("❌ Не удалось получить цены.")
        return

    now = datetime.now(MOSCOW_TZ).strftime("%H:%M МСК")
    text = f"📊 *Рынок крипто* ({now})\n\n"
    for sym, data in prices.items():
        emoji = "🟢" if data["change_24h"] >= 0 else "🔴"
        text += f"{emoji} *{sym}*: ${data['price']:,.2f} ({data['change_24h']:+.1f}%)\n"

    text += f"\n🔗 [Открыть Bybit](https://www.bybit.com/trade/usdt/BTCUSDT)"

    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def budget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    budget = load_budget()
    winrate = 0
    if budget["total_trades"] > 0:
        winrate = round(budget["winning_trades"] / budget["total_trades"] * 100, 1)

    trade_amount = round(budget["current_balance"] * budget["risk_per_trade"], 2)
    pnl_emoji = "🟢" if budget["total_pnl"] >= 0 else "🔴"

    text = (
        "💰 *Мой торговый бюджет*\n\n"
        f"💵 Начальный депозит: ${budget['initial_deposit']:.2f}\n"
        f"💼 Текущий баланс: *${budget['current_balance']:.2f}*\n"
        f"{pnl_emoji} Общий PnL: ${budget['total_pnl']:+.2f}\n\n"
        f"📊 Всего сделок: {budget['total_trades']}\n"
        f"✅ Прибыльных: {budget['winning_trades']}\n"
        f"❌ Убыточных: {budget['losing_trades']}\n"
        f"🎯 Винрейт: {winrate}%\n\n"
        f"⚡ Риск на сделку: {budget['risk_per_trade']*100:.0f}%\n"
        f"💵 Макс. на следующую сделку: *${trade_amount:.2f}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def set_budget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /set_budget 100")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            await update.message.reply_text("❌ Сумма должна быть больше 0")
            return
        budget = load_budget()
        budget["initial_deposit"] = amount
        budget["current_balance"] = amount
        budget["total_pnl"] = 0
        budget["trades"] = []
        budget["total_trades"] = 0
        budget["winning_trades"] = 0
        budget["losing_trades"] = 0
        save_budget(budget)
        await update.message.reply_text(
            f"✅ Бюджет установлен: *${amount:.2f}*\n"
            f"💵 На сделку (2%): *${amount * 0.02:.2f}*",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Укажи число. Пример: /set_budget 100")

async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "/trade WIN 5.50 — прибыльная сделка (+$5.50)\n"
            "/trade LOSS 3.20 — убыточная сделка (-$3.20)"
        )
        return
    try:
        result = context.args[0].upper()
        amount = float(context.args[1])
        if result not in ("WIN", "LOSS"):
            await update.message.reply_text("❌ Первый аргумент: WIN или LOSS")
            return

        budget = load_budget()
        now = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")

        if result == "WIN":
            budget["current_balance"] += amount
            budget["winning_trades"] += 1
            budget["total_pnl"] += amount
            emoji = "✅"
        else:
            budget["current_balance"] -= amount
            budget["losing_trades"] += 1
            budget["total_pnl"] -= amount
            emoji = "❌"

        budget["total_trades"] += 1
        budget["trades"].append({
            "date": now,
            "result": result,
            "amount": amount
        })
        # Храним только последние 50 сделок
        budget["trades"] = budget["trades"][-50:]
        save_budget(budget)

        trade_amount = round(budget["current_balance"] * budget["risk_per_trade"], 2)
        await update.message.reply_text(
            f"{emoji} Сделка записана: {result} ${amount:.2f}\n\n"
            f"💼 Баланс: *${budget['current_balance']:.2f}*\n"
            f"📊 PnL: ${budget['total_pnl']:+.2f}\n"
            f"💵 На след. сделку: *${trade_amount:.2f}*",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Укажи число. Пример: /trade WIN 5.50")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    budget = load_budget()
    if budget["total_trades"] == 0:
        await update.message.reply_text("📊 Пока нет записанных сделок. Используй /trade WIN/LOSS сумма")
        return

    winrate = round(budget["winning_trades"] / budget["total_trades"] * 100, 1)
    roi = round((budget["current_balance"] - budget["initial_deposit"]) / budget["initial_deposit"] * 100, 1)

    text = (
        "📊 *Статистика торговли*\n\n"
        f"💵 Депозит: ${budget['initial_deposit']:.2f}\n"
        f"💼 Баланс: ${budget['current_balance']:.2f}\n"
        f"📈 ROI: {roi:+.1f}%\n\n"
        f"Всего сделок: {budget['total_trades']}\n"
        f"✅ Win: {budget['winning_trades']} | ❌ Loss: {budget['losing_trades']}\n"
        f"🎯 Винрейт: {winrate}%\n"
        f"💰 PnL: ${budget['total_pnl']:+.2f}\n\n"
    )

    # Последние 5 сделок
    if budget["trades"]:
        text += "*Последние сделки:*\n"
        for t in budget["trades"][-5:]:
            e = "✅" if t["result"] == "WIN" else "❌"
            sign = "+" if t["result"] == "WIN" else "-"
            text += f"{e} {t['date']} — {sign}${t['amount']:.2f}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def check_news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Проверяю свежие новости...")
    news = get_crypto_news()
    if not news:
        await update.message.reply_text("📭 Новых важных новостей нет. Проверю снова через 5 минут.")
        return

    prices = get_crypto_prices()
    budget = load_budget()

    for item in news[:3]:  # Макс 3 новости за раз
        analysis = analyze_news_for_trading(item, prices, budget)
        if analysis:
            # Добавляем ссылки
            text = f"📰 *{item['title']}*\n"
            text += f"🔗 [Источник]({item['url']})\n\n"
            text += analysis + "\n\n"
            text += f"🔗 [Открыть Bybit (BTC)](https://www.bybit.com/trade/usdt/BTCUSDT) | "
            text += f"[ETH](https://www.bybit.com/trade/usdt/ETHUSDT) | "
            text += f"[SOL](https://www.bybit.com/trade/usdt/SOLUSDT)"

            try:
                await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)
            except:
                await update.message.reply_text(text, disable_web_page_preview=True)

    save_seen_news()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "market":
        await query.edit_message_reply_markup(reply_markup=None)
        prices = get_crypto_prices()
        if prices:
            now = datetime.now(MOSCOW_TZ).strftime("%H:%M МСК")
            text = f"📊 *Рынок крипто* ({now})\n\n"
            for sym, data in prices.items():
                emoji = "🟢" if data["change_24h"] >= 0 else "🔴"
                text += f"{emoji} *{sym}*: ${data['price']:,.2f} ({data['change_24h']:+.1f}%)\n"
            text += f"\n🔗 [Открыть Bybit](https://www.bybit.com/trade/usdt/BTCUSDT)"
            await query.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await query.message.reply_text("❌ Не удалось получить данные.")

    elif query.data == "budget":
        await query.edit_message_reply_markup(reply_markup=None)
        budget = load_budget()
        trade_amount = round(budget["current_balance"] * budget["risk_per_trade"], 2)
        pnl_emoji = "🟢" if budget["total_pnl"] >= 0 else "🔴"
        text = (
            f"💰 Баланс: *${budget['current_balance']:.2f}*\n"
            f"{pnl_emoji} PnL: ${budget['total_pnl']:+.2f}\n"
            f"💵 На сделку: *${trade_amount:.2f}*\n"
            f"📊 Сделок: {budget['total_trades']}"
        )
        await query.message.reply_text(text, parse_mode="Markdown")

    elif query.data == "check_news":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🔍 Проверяю новости...")
        news = get_crypto_news()
        if not news:
            await query.message.reply_text("📭 Новых важных новостей нет.")
            return
        prices = get_crypto_prices()
        budget = load_budget()
        for item in news[:2]:
            analysis = analyze_news_for_trading(item, prices, budget)
            if analysis:
                text = f"📰 *{item['title']}*\n\n{analysis}"
                try:
                    await query.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)
                except:
                    await query.message.reply_text(text, disable_web_page_preview=True)
        save_seen_news()

    elif query.data == "setup_budget":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "⚙️ Для настройки бюджета отправь:\n"
            "/set_budget 100 — установить $100 как депозит"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    user_message = update.message.text
    await update.message.reply_text("🤔 Анализирую...")
    budget = load_budget()
    answer = answer_question(user_message, budget)
    try:
        await update.message.reply_text(answer, parse_mode="Markdown")
    except:
        await update.message.reply_text(answer)

# ========================
# ФОНОВЫЙ МОНИТОРИНГ НОВОСТЕЙ
# ========================
async def news_monitor(app):
    """Проверяет новости каждые 5 минут и шлёт алерты."""
    logger.info(f"Мониторинг новостей запущен (каждые {NEWS_CHECK_INTERVAL}с)")
    load_seen_news()

    # Первый запуск — ждём 60 секунд чтобы бот полностью стартовал
    await asyncio.sleep(60)

    while True:
        try:
            if USER_CHAT_ID:
                news = get_crypto_news()
                if news:
                    prices = get_crypto_prices()
                    budget = load_budget()

                    for item in news[:2]:  # Макс 2 новости за цикл
                        analysis = analyze_news_for_trading(item, prices, budget)
                        if analysis and "SKIP" not in analysis.upper():
                            text = f"🚨 *НОВАЯ НОВОСТЬ*\n\n"
                            text += f"📰 *{item['title']}*\n"
                            text += f"🔗 [Источник]({item['url']})\n\n"
                            text += analysis + "\n\n"
                            text += f"🔗 [BTC](https://www.bybit.com/trade/usdt/BTCUSDT) | "
                            text += f"[ETH](https://www.bybit.com/trade/usdt/ETHUSDT) | "
                            text += f"[SOL](https://www.bybit.com/trade/usdt/SOLUSDT)"

                            try:
                                await app.bot.send_message(
                                    chat_id=USER_CHAT_ID,
                                    text=text,
                                    parse_mode="Markdown",
                                    disable_web_page_preview=True
                                )
                            except Exception as e:
                                logger.error(f"Ошибка отправки алерта: {e}")
                                try:
                                    await app.bot.send_message(
                                        chat_id=USER_CHAT_ID,
                                        text=text,
                                        disable_web_page_preview=True
                                    )
                                except:
                                    pass

                    save_seen_news()
        except Exception as e:
            logger.error(f"Ошибка мониторинга: {e}")

        await asyncio.sleep(NEWS_CHECK_INTERVAL)

# ========================
# ЗАПУСК
# ========================
async def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан!")
        return

    load_chat_id()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("market", market_command))
    app.add_handler(CommandHandler("budget", budget_command))
    app.add_handler(CommandHandler("set_budget", set_budget_command))
    app.add_handler(CommandHandler("trade", trade_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("check", check_news_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()

    # Webhook
    webhook_url = f"https://{WEBHOOK_HOST}/webhook"
    await app.bot.set_webhook(webhook_url, drop_pending_updates=True)
    logger.info(f"Webhook: {webhook_url}")

    # aiohttp сервер
    async def handle_webhook(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="ok")

    async def handle_health(request):
        return web.Response(text="ok")

    aiohttp_app = web.Application()
    aiohttp_app.router.add_post("/webhook", handle_webhook)
    aiohttp_app.router.add_get("/", handle_health)

    runner = web.AppRunner(aiohttp_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Сервер на порту {PORT}")

    # Фоновый мониторинг
    asyncio.ensure_future(news_monitor(app))

    logger.info("Бот запущен! (webhook + мониторинг)")

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
