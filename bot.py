import asyncio
import logging
import os
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
SEND_TIME = "07:00"  # Время отправки сводки по Москве
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_HOST = "worker-production-f7e9.up.railway.app"

# ID пользователя — заполняется автоматически при первом /start
USER_CHAT_ID = None
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_CHAT_ID_FILE = os.path.join(BASE_DIR, "chat_id.txt")

# ========================
# ЛОГИРОВАНИЕ
# ========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========================
# ЗАГРУЗКА CHAT ID
# ========================
def load_chat_id():
    global USER_CHAT_ID
    # Сначала проверяем переменную окружения
    env_chat_id = os.environ.get("USER_CHAT_ID", "")
    if env_chat_id:
        try:
            USER_CHAT_ID = int(env_chat_id)
            logger.info(f"Загружен chat_id из env: {USER_CHAT_ID}")
            return
        except:
            pass
    # Иначе из файла
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
        logger.warning(f"Не удалось сохранить chat_id в файл: {e}")

# ========================
# КУРС ВАЛЮТ
# ========================
def get_exchange_rates():
    try:
        url = "https://api.exchangerate-api.com/v4/latest/RUB"
        response = requests.get(url, timeout=10)
        data = response.json()
        rates = data.get("rates", {})
        usd_rate = round(1 / rates.get("USD", 1), 2)
        eur_rate = round(1 / rates.get("EUR", 1), 2)
        cny_rate = round(1 / rates.get("CNY", 1), 2)
        return usd_rate, eur_rate, cny_rate
    except Exception as e:
        logger.error(f"Ошибка получения курсов: {e}")
        return None, None, None

# ========================
# НОВОСТИ
# ========================
def get_news():
    try:
        url = "https://newsapi.org/v2/top-headlines"
        params = {
            "apiKey": NEWS_API_KEY,
            "language": "en",
            "pageSize": 10,
            "category": "general"
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        articles = data.get("articles", [])
        news_list = []
        for article in articles:
            title = article.get("title", "")
            description = article.get("description", "")
            url_link = article.get("url", "")
            if title and "[Removed]" not in title:
                news_list.append({
                    "title": title,
                    "description": description or "",
                    "url": url_link
                })
        return news_list
    except Exception as e:
        logger.error(f"Ошибка получения новостей: {e}")
        return []

# ========================
# GROQ — анализ и перевод
# ========================
def analyze_news_with_openrouter(news_list, usd, eur, cny):
    if not OPENROUTER_API_KEY:
        return format_news_without_ai(news_list, usd, eur, cny)
    try:
        client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1"
        )

        news_text = "\n\n".join([
            f"{i+1}. {n['title']}\n{n['description']}"
            for i, n in enumerate(news_list)
        ])

        prompt = f"""Ты профессиональный аналитик. Я дам тебе список новостей на английском языке.

Твоя задача:
1. Переведи каждую новость на русский язык
2. Сократи до 1-2 предложений — только самое важное
3. Если новость несущественная — пропусти её
4. Выбери 5-7 самых важных новостей для итоговой сводки
5. В конце добавь 1-2 предложения общего вывода о том, что происходит в мире

Формат ответа:
📰 *Мировые новости*

1. [краткое описание на русском]
2. [краткое описание на русском]
...

💡 *Вывод:* [общий вывод]

Вот новости:
{news_text}"""

        response = client.chat.completions.create(
            model="nvidia/nemotron-3-super-120b-a12b:free",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка OpenRouter: {e}")
        return format_news_without_ai(news_list, usd, eur, cny)

def format_news_without_ai(news_list, usd, eur, cny):
    text = "📰 *Мировые новости*\n\n"
    for i, n in enumerate(news_list[:7], 1):
        text += f"{i}. {n['title']}\n"
    return text

def answer_question_with_openrouter(question, context=""):
    if not OPENROUTER_API_KEY:
        return "OpenRouter API ключ не настроен."
    try:
        client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1"
        )
        prompt = f"""Ты личный аналитик-ассистент. Отвечай на русском языке, кратко и по делу.

{f'Контекст: {context}' if context else ''}

Вопрос пользователя: {question}"""
        response = client.chat.completions.create(
            model="nvidia/nemotron-3-super-120b-a12b:free",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка OpenRouter: {e}")
        return "Произошла ошибка при обработке запроса. Попробуй позже."

# ========================
# ФОРМИРОВАНИЕ СВОДКИ
# ========================
def build_morning_digest():
    usd, eur, cny = get_exchange_rates()
    news_list = get_news()

    now_moscow = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")

    # Курсы валют
    rates_text = f"💱 *Курс валют на {now_moscow}*\n"
    if usd:
        rates_text += f"🇺🇸 USD: {usd} ₽\n"
        rates_text += f"🇪🇺 EUR: {eur} ₽\n"
        rates_text += f"🇨🇳 CNY: {cny} ₽\n"
    else:
        rates_text += "_(не удалось загрузить курсы)_\n"

    # Новости через OpenRouter
    news_text = analyze_news_with_openrouter(news_list, usd, eur, cny)

    digest = f"🌅 *Доброе утро! Ваша сводка на {now_moscow}*\n\n"
    digest += rates_text + "\n"
    digest += news_text + "\n\n"
    digest += "💬 _Если хочешь узнать подробности — просто напиши мне вопрос._"

    return digest

# ========================
# TELEGRAM ОБРАБОТЧИКИ
# ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    keyboard = [[InlineKeyboardButton("📊 Получить сводку сейчас", callback_data="digest")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Привет! Я твой личный аналитик.\n\n"
        "Каждое утро в *7:00 по Москве* я буду присылать тебе:\n"
        "• 💱 Курсы валют (USD, EUR, CNY)\n"
        "• 📰 Топ мировых новостей (кратко)\n\n"
        "Можешь задать мне любой вопрос прямо сейчас — я отвечу как аналитик.",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def send_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    await update.message.reply_text("⏳ Собираю сводку, подожди немного...")
    digest = build_morning_digest()
    await update.message.reply_text(digest, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "digest":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏳ Собираю сводку, подожди немного...")
        digest = build_morning_digest()
        await query.message.reply_text(digest, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    user_message = update.message.text
    await update.message.reply_text("🤔 Анализирую...")
    answer = answer_question_with_openrouter(user_message)
    await update.message.reply_text(answer, parse_mode="Markdown")

# ========================
# ПЛАНИРОВЩИК
# ========================
async def scheduler_job(app):
    """Асинхронный планировщик — проверяет время каждые 30 секунд."""
    logger.info(f"Планировщик запущен. Сводка будет отправляться в {SEND_TIME} МСК")
    sent_today = None
    while True:
        now_moscow = datetime.now(MOSCOW_TZ)
        current_time = now_moscow.strftime("%H:%M")
        today = now_moscow.strftime("%Y-%m-%d")

        if current_time == SEND_TIME and sent_today != today:
            if USER_CHAT_ID:
                logger.info("Отправка утренней сводки...")
                digest = build_morning_digest()
                await app.bot.send_message(
                    chat_id=USER_CHAT_ID,
                    text=digest,
                    parse_mode="Markdown"
                )
                sent_today = today
                logger.info("Сводка отправлена!")

        await asyncio.sleep(30)

# ========================
# ЗАПУСК
# ========================
async def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан! Установите переменную окружения.")
        return

    load_chat_id()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("digest", send_digest_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()

    # Устанавливаем webhook
    webhook_url = f"https://{WEBHOOK_HOST}/webhook"
    await app.bot.set_webhook(webhook_url, drop_pending_updates=True)
    logger.info(f"Webhook установлен: {webhook_url}")

    # aiohttp сервер для приёма webhook
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
    logger.info(f"Сервер запущен на порту {PORT}")

    # Запускаем планировщик
    asyncio.ensure_future(scheduler_job(app))

    logger.info("Бот запущен! (webhook mode)")

    # Держим процесс живым
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
