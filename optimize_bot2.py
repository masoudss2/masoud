import os
import logging
import signal
import sys
import time
from functools import lru_cache
from io import BytesIO
from typing import Optional, Dict, Any, List
import json
from datetime import datetime, timedelta
import random  # برای تولید داده‌های ساختگی

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
import requests
from PyPDF2 import PdfReader
import tabula
from cryptography.fernet import Fernet
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from cachetools import TTLCache, cached
import traceback


# --- Configuration ---
# تنظیم مستقیم متغیرها بدون استفاده از فایل .env
TELEGRAM_TOKEN = "7429551898:AAF0BnBcQwNmi7IRA3PPVNf-K-4On2JROgs"  # توکن تلگرام خود را اینجا قرار دهید
DEEPSEEK_API_KEY = "sk-033cc340ba3247f7931a64c5e3d77330"  # کلید API دیپ‌سیک خود را اینجا قرار دهید
ALPHA_VANTAGE_API_KEY = "8RD7DN1R2W5AI9UT"  # کلید API آلفا ونتیج خود را اینجا قرار دهید
FINANCIAL_MODELING_PREP_API_KEY = "jBxtfLbURIAQQnzoQlL1ywKM72hrbAZT"  # کلید API فایننشال مادلینگ پرپ خود را اینجا قرار دهید

# تولید کلید رمزنگاری
ENCRYPTION_KEY = Fernet.generate_key()

# آدرس‌های پایه API
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

CACHE_SIZE = 100  # Maximum cached responses
CACHE_TTL = 259200 # Time to live for cache items (30 days)
MAX_PDF_PAGES = 10  # Prevent processing large PDFs
MAX_TEXT_LENGTH = 3000  # Character limit for API inputs
# Initialize caches with TTL
news_cache = TTLCache(maxsize=50, ttl=CACHE_TTL)
stock_cache = TTLCache(maxsize=50, ttl=CACHE_TTL)
market_cache = TTLCache(maxsize=10, ttl=CACHE_TTL/2)  # Market data expires faster

# Initialize Fernet for encryption
fernet = Fernet(ENCRYPTION_KEY)

# --- Logging --
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Utility Functions ---
def encrypt_data(data: str) -> str:
    """Encrypt sensitive data before storage"""
    return fernet.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    """Decrypt stored data"""
    return fernet.decrypt(encrypted_data.encode()).decode()

def normalize_prompt(prompt: str) -> str:
    """Standardize prompts for effective caching"""
    return prompt.strip().replace("\n", " ").replace("  ", " ")

# --- PDF Processing ---
def process_pdf(pdf_bytes: BytesIO) -> dict:
    """Extract text and tables from PDF with error handling"""
    try:
        # Text extraction
        reader = PdfReader(pdf_bytes)
        text = " ".join([page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES]])
        
        # Table extraction
        tables = tabula.read_pdf(
            pdf_bytes, 
            pages=f"1-{min(MAX_PDF_PAGES, len(reader.pages))}",
            multiple_tables=True,
            pandas_options={'header': None}
        )
        tables_md = "\n\n".join([df.to_markdown() for df in tables if not df.empty])
        
        return {
            "text": text[:MAX_TEXT_LENGTH],
            "tables": tables_md[:MAX_TEXT_LENGTH]
        }
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        return {"error": str(e)}
# --- Excel Processing ---
def process_excel(excel_bytes: BytesIO) -> dict:
    """Extract data from Excel files with error handling"""
    MAX_EXCEL_ROWS = 1000  # Define reasonable limit for rows to process
    
    try:
        # Read Excel file
        df_dict = pd.read_excel(excel_bytes, sheet_name=None)
        
        results = {}
        
        # Process each sheet
        for sheet_name, df in df_dict.items():
            # Limit rows for processing
            df = df.head(MAX_EXCEL_ROWS)
            
            # Basic statistics
            numeric_cols = df.select_dtypes(include=[np.number]).columns
            if len(numeric_cols) > 0:
                stats = df[numeric_cols].describe().to_markdown()
            else:
                stats = "No numeric data found for statistics"
            
            # Convert to markdown for better display
            table_md = df.to_markdown(index=False)
            
            # Generate summary
            summary = {
                "rows": len(df),
                "columns": len(df.columns),
                "column_names": df.columns.tolist(),
                "missing_values": df.isna().sum().to_dict(),
            }
            
            results[sheet_name] = {
                "summary": summary,
                "statistics": stats,
                "table": table_md[:MAX_TEXT_LENGTH]
            }
        
        return {
            "sheets": list(results.keys()),
            "data": results,
            "total_sheets": len(results)
        }
    except Exception as e:
        logger.error(f"Excel processing error: {e}")
        return {"error": str(e)}
# --- AI Integration ---
# کش با زمان انقضا برای پاسخ‌های AI
ai_cache = TTLCache(maxsize=CACHE_SIZE, ttl=CACHE_TTL)

@cached(cache=ai_cache)
def query_deepseek(prompt: str, use_reasoner: bool = False) -> str:
    """Get AI response with TTL caching and improved error handling"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # انتخاب مدل بر اساس پیچیدگی درخواست
    model = "deepseek-chat" # همیشه از مدل deepseek-chat استفاده کنیم
    
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 500
    }
    
    # تعداد تلاش‌های مجدد
    max_retries = 3
    
    for retry in range(max_retries):
        try:
            # افزایش timeout به 60 ثانیه
            response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f"API Error (attempt {retry+1}/{max_retries}): {e}")
            if retry < max_retries - 1:
                # انتظار قبل از تلاش مجدد
                wait_time = 3 * (retry + 1)  # 3, 6, 9 ثانیه
                logger.info(f"Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
            else:
                return "⚠ خطا در ارتباط با سرور هوش مصنوعی. لطفاً دوباره تلاش کنید."


    # --- Financial API Integration ---
@cached(cache=news_cache)
def get_financial_news(keywords: str = "", limit: int = 10) -> list:
    """Get financial news with TTL caching"""
    try:
        params = {
            "function": "NEWS_SENTIMENT",
            "apikey": ALPHA_VANTAGE_API_KEY,
            "limit": limit
        }
        
        if keywords:
            params["tickers"] = keywords
        
        response = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "feed" not in data:
            return [{"title": "خطا در دریافت اخبار", "url": ""}]
        
        news_items = []
        for item in data["feed"][:limit]:
            news_items.append({
                "title": item.get("title", "بدون عنوان"),
                "summary": item.get("summary", "")[:100] + "...",
                "url": item.get("url", ""),
                "time_published": item.get("time_published", ""),
                "sentiment": item.get("overall_sentiment_label", "neutral")
            })
        
        return news_items
    except Exception as e:
        logger.error(f"Financial news API error: {e}")
        return [{"title": f"خطا در دریافت اخبار: {str(e)}", "url": ""}]

@cached(cache=stock_cache)
def get_stock_data(symbol: str) -> dict:
    """Get stock data with TTL caching"""
    try:
        # Get company profile
        profile_url = f"{FMP_BASE_URL}/profile/{symbol}?apikey={FINANCIAL_MODELING_PREP_API_KEY}"
        profile_response = requests.get(profile_url, timeout=10)
        profile_response.raise_for_status()
        profile_data = profile_response.json()
        
        if not profile_data or len(profile_data) == 0:
            return {"error": "نماد یافت نشد"}
        
        # Get financial ratios
        ratios_url = f"{FMP_BASE_URL}/ratios/{symbol}?limit=1&apikey={FINANCIAL_MODELING_PREP_API_KEY}"
        ratios_response = requests.get(ratios_url, timeout=10)
        ratios_response.raise_for_status()
        ratios_data = ratios_response.json()
        
        # Combine data
        result = {
            "profile": profile_data[0],
            "ratios": ratios_data[0] if ratios_data else {},
        }
        
        return result
    except Exception as e:
        logger.error(f"Stock data API error: {e}")
        return {"error": str(e)}

def generate_financial_chart(data: Dict[str, Any], chart_type: str = "price") -> BytesIO:
    """Generate financial charts based on data"""
    try:
        plt.figure(figsize=(10, 6))
        
        if chart_type == "price" and "historical" in data:
            dates = [item["date"] for item in data["historical"]]
            prices = [item["close"] for item in data["historical"]]
            
            plt.plot(dates, prices)
            plt.title(f"Historical Prices: {data.get('profile', {}).get('companyName', 'Unknown')}")
            plt.xlabel("Date")
            plt.ylabel("Price")
            plt.xticks(rotation=45)
            plt.tight_layout()
        
        # Save to buffer
        buf = BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()
        
        return buf
    except Exception as e:
        logger.error(f"Chart generation error: {e}")
        # Return a simple error image
        plt.figure(figsize=(5, 3))
        plt.text(0.5, 0.5, f"Error generating chart: {str(e)}", 
                 horizontalalignment='center', verticalalignment='center')
        plt.axis('off')
        buf = BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()
        return buf

# --- توابع ساختگی برای بازار ایران ---
def get_iran_market_data() -> dict:
    """تولید داده‌های ساختگی برای بازار بورس ایران"""
    # ساخت داده‌های ساختگی
    market_data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_status": "باز" if datetime.now().hour < 12 else "بسته",
        "overall_index": f"{1_700_000 + random.randint(-50_000, 50_000):,}",
        "market_value": f"{12_345_678:,} میلیارد ریال",
        "trade_volume": f"{5_432:,} میلیون سهم",
        "market_trend": random.choice(["مثبت", "منفی", "خنثی"]),
        "positive_symbols": random.randint(150, 300),
        "negative_symbols": random.randint(150, 300),
        "neutral_symbols": random.randint(50, 100),
    }
    
    return market_data

def get_iran_stock_data(symbol: str) -> dict:
    """تولید داده‌های ساختگی برای سهام بورس ایران"""
    # دیکشنری نمادهای معروف
    stock_data_dict = {
        "خودرو": {
            "full_name": "ایران خودرو",
            "price": "2,450",
            "change_percent": "+3.5%",
            "industry": "خودرو و قطعات",
            "market_cap": "98,000 میلیارد ریال",
            "p/e": "6.8",
            "eps": "360"
        },
        "فولاد": {
            "full_name": "فولاد مبارکه اصفهان",
            "price": "3,780",
            "change_percent": "-1.2%",
            "industry": "فلزات اساسی",
            "market_cap": "283,500 میلیارد ریال",
            "p/e": "4.2",
            "eps": "900"
        },
                "شستا": {
            "full_name": "سرمایه‌گذاری تأمین اجتماعی",
            "price": "4,120",
            "change_percent": "+0.8%",
            "industry": "سرمایه‌گذاری چند رشته‌ای",
            "market_cap": "412,000 میلیارد ریال",
            "p/e": "5.3",
            "eps": "778"
        },
        "وبملت": {
            "full_name": "بانک ملت",
            "price": "5,230",
            "change_percent": "+2.1%",
            "industry": "بانک‌ها و موسسات اعتباری",
            "market_cap": "157,000 میلیارد ریال",
            "p/e": "7.1",
            "eps": "736"
        },
        "فارس": {
            "full_name": "صنایع پتروشیمی خلیج فارس",
            "price": "8,640",
            "change_percent": "-0.7%",
            "industry": "محصولات شیمیایی",
            "market_cap": "518,400 میلیارد ریال",
            "p/e": "6.2",
            "eps": "1,393"
        }
    }
    
    # اگر نماد در دیکشنری وجود داشت، اطلاعات آن را استفاده کن
    if symbol in stock_data_dict:
        data = stock_data_dict[symbol]
        stock_data = {
            "symbol": symbol,
            "full_name": data["full_name"],
            "price": data["price"],
            "change_percent": data["change_percent"],
            "industry": data["industry"],
            "market_cap": data["market_cap"],
            "p/e": data["p/e"],
            "eps": data["eps"],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    # در غیر این صورت، اطلاعات ساختگی بساز
    else:
        stock_data = {
            "symbol": symbol,
            "full_name": f"شرکت {symbol}",
            "price": f"{random.randint(1000, 10000):,}",
            "change_percent": f"{random.choice(['+', '-'])}{random.uniform(0.1, 5.0):.1f}%",
            "industry": random.choice(["خودرو و قطعات", "بانک‌ها", "فلزات اساسی", "محصولات شیمیایی", "سیمان", "دارویی"]),
            "market_cap": f"{random.randint(10000, 500000):,} میلیارد ریال",
            "p/e": f"{random.uniform(3.0, 12.0):.1f}",
            "eps": f"{random.randint(100, 2000):,}",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    
    # ساخت تاریخچه قیمت ساختگی
    base_price = int(stock_data["price"].replace(",", "")) if "," in stock_data["price"] else random.randint(1000, 10000)
    history = []
    
    for i in range(7):  # 7 روز اخیر
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        price_change = random.randint(-200, 200)
        price = max(100, base_price + price_change)
        
        history.append({
            "date": day,
            "close_price": f"{price:,}",
            "volume": f"{random.randint(100_000, 1_000_000):,}"
        })
    
    stock_data["history"] = history
    
    return stock_data

def get_codal_reports(symbol: str) -> list:
    """تولید داده‌های ساختگی برای گزارش‌های کدال"""
    # دیکشنری نمادهای معروف
    company_names = {
        "خودرو": "ایران خودرو",
        "فولاد": "فولاد مبارکه اصفهان",
        "شستا": "سرمایه‌گذاری تأمین اجتماعی",
        "وبملت": "بانک ملت",
        "فارس": "صنایع پتروشیمی خلیج فارس"
    }
    
    # اگر نماد در دیکشنری وجود داشت، نام آن را استفاده کن
    company_name = company_names.get(symbol, f"شرکت {symbol}")
    
    # انواع گزارش‌های کدال
    report_types = [
        "صورت‌های مالی میان‌دوره‌ای",
        "گزارش فعالیت ماهانه",
        "اطلاعیه",
        "پیش‌بینی درآمد",
        "تصمیمات مجمع",
        "افشای اطلاعات بااهمیت"
    ]
    
    reports = []
    for i in range(5):  # 5 گزارش اخیر
        # تاریخ تصادفی در 3 ماه اخیر
        days_ago = random.randint(0, 90)
        report_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        
        # نوع گزارش تصادفی
        report_type = report_types[i % len(report_types)]
        
        reports.append({
            "date": report_date,
            "title": f"گزارش {report_type} شرکت {company_name}",
            "category": report_type,
            "url": f"https://www.codal.ir/Reports/Report{i+1}.aspx"
        })
    
    # مرتب‌سازی گزارش‌ها بر اساس تاریخ (جدیدترین اول)
    reports.sort(key=lambda x: x["date"], reverse=True)
    
    return {
        "symbol": symbol,
        "company_name": company_name,
        "reports": reports
    }

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate conversation with knowledge level selection"""
    keyboard = [
        [InlineKeyboardButton("مبتدی", callback_data="level_beginner"),
         InlineKeyboardButton("متوسط", callback_data="level_intermediate"),
         InlineKeyboardButton("حرفه‌ای", callback_data="level_pro")]
    ]
    await update.message.reply_text(
        "🎯 لطفاً سطح دانش مالی خود را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def set_knowledge_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle knowledge level selection"""
    query = update.callback_query
    await query.answer()
    
    level = query.data.split("_")[1]
    context.user_data["knowledge_level"] = level
    
    # تغییر فرمت پیام برای جلوگیری از خطای پارس کردن
    level_names = {
        "beginner": "مبتدی",
        "intermediate": "متوسط",
        "pro": "حرفه‌ای"
    }
    
    message_text = f"✅ سطح دانش شما به {level_names.get(level, level)} تنظیم شد!\n\n"
    message_text += "اکنون می‌توانید سوالات خود را مطرح کنید یا فایل Excel یا PDF صورت مالی خود را ارسال کنید.\n\n"
    message_text += "دستورات اصلی:\n"
    message_text += "/news [کلمات کلیدی] - دریافت آخرین اخبار مالی\n"
    message_text += "/stock [نماد] - دریافت اطلاعات و تحلیل سهام\n"
    message_text += "/market - خلاصه وضعیت بازار\n"
    message_text += "/help - نمایش راهنمای کامل\n\n"
    message_text += "دستورات بورس ایران:\n"
    message_text += "• /iran_market - مشاهده وضعیت کلی بازار\n"
    message_text += "• /iran_stock [نماد] - تحلیل سهام بورس ایران (مثال: /iran_stock خودرو)\n"
    message_text += "• /codal [نماد] - گزارش‌های کدال"
    
    await query.edit_message_text(message_text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process text queries with level-appropriate responses"""
    user_input = update.message.text
    user_id = update.effective_user.id
    
    # Get user's knowledge level
    level = context.user_data.get("knowledge_level", "beginner")
    
    # Create level-specific prompt
    prompt_templates = {
        "beginner": "به زبان ساده و با مثال ملموس توضیح دهید:",
        "intermediate": "با ذکر اصطلاحات تخصصی ولی قابل فهم توضیح دهید:",
        "pro": "با جزئیات فنی کامل و فرمول‌های مرتبط پاسخ دهید:"
    }
    
    base_prompt = f"""
    به عنوان تحلیلگر مالی سطح {level} به این سوال پاسخ دهید:
    سوال: {user_input}
    {prompt_templates[level]}
    """
    
    # Normalize for caching
    clean_prompt = normalize_prompt(base_prompt)
    
    # تعیین استفاده از مدل Reasoner بر اساس سطح دانش کاربر یا محتوای سوال
    use_reasoner = False
    if level == "pro":
        use_reasoner = True
    elif any(keyword in user_input.lower() for keyword in ["تحلیل مالی", "نسبت مالی", "صورت مالی", "سرمایه‌گذاری"]):
        use_reasoner = True
    
    # Get cached or new response with appropriate model
    response = query_deepseek(clean_prompt, use_reasoner=use_reasoner)
    
    # Send response and request feedback
    await update.message.reply_text(
        f"📊 پاسخ تحلیلگر:\n\n{response}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👍 مفید بود", callback_data="feedback_good"),
            InlineKeyboardButton("👎 مفید نبود", callback_data="feedback_bad")
        ]])
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process PDF and Excel financial documents"""
    document = update.message.document
    file_name = document.file_name.lower()
    
    # Check file type
    if file_name.endswith('.pdf'):
        try:
            # Download and process PDF
            await update.message.reply_text("⏳ در حال پردازش فایل PDF...")
            file = await context.bot.get_file(document.file_id)
            pdf_bytes = BytesIO(await file.download_as_bytearray())
            processed = process_pdf(pdf_bytes)
            
            if "error" in processed:
                raise Exception(processed["error"])
            
            # Create analysis prompt
            prompt = f"""
            تحلیل صورت مالی زیر را انجام دهید:
            متن اصلی: {processed['text']}
            جداول: {processed['tables']}
            
            موارد تحلیل:
            1- نقاط قوت/ضعف مالی
            2- نسبت‌های کلیدی
            3- پیشنهادات سرمایه‌گذاری
            """
            
            # Get and send response
            response = query_deepseek(normalize_prompt(prompt), use_reasoner=True)
            await update.message.reply_text(f"📈 تحلیل صورت مالی:\n\n{response}")
            
        except Exception as e:
            logger.error(f"PDF Error: {e}")
            await update.message.reply_text(f"❌ خطا در پردازش سند PDF: {str(e)}")
    
    elif file_name.endswith(('.xls', '.xlsx', '.xlsm')):
        await handle_excel(update, context)
    
    else:
        await update.message.reply_text("❌ فرمت فایل پشتیبانی نمی‌شود. لطفاً فایل PDF یا Excel ارسال کنید.")

async def handle_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process Excel financial data"""
    if not update.message.document.file_name.endswith(('.xls', '.xlsx', '.xlsm')):
        await update.message.reply_text("❌ لطفاً فقط فایل اکسل ارسال کنید.")
        return
    
    try:
        # Download and process Excel
        await update.message.reply_text("⏳ در حال پردازش فایل اکسل...")
        file = await context.bot.get_file(update.message.document.file_id)
        excel_bytes = BytesIO(await file.download_as_bytearray())
        processed = process_excel(excel_bytes)
        
        if "error" in processed:
            raise Exception(processed["error"])
        
        # Create analysis prompt
        sheets_info = "\n".join([f"- {sheet}" for sheet in processed["sheets"]])
        first_sheet = processed["sheets"][0]
        first_sheet_data = processed["data"][first_sheet]
        
        prompt = f"""
        تحلیل داده‌های مالی اکسل زیر را انجام دهید:
        
        اطلاعات فایل:
        - تعداد شیت‌ها: {processed["total_sheets"]}
        - شیت‌های موجود: {sheets_info}
        
        داده‌های شیت اول ({first_sheet}):
        - تعداد سطرها: {first_sheet_data["summary"]["rows"]}
        - تعداد ستون‌ها: {first_sheet_data["summary"]["columns"]}
        - نام ستون‌ها: {", ".join(first_sheet_data["summary"]["column_names"])}
        
        آمار توصیفی:
        {first_sheet_data["statistics"]}
        
        داده‌ها:
        {first_sheet_data["table"]}
        
        لطفاً تحلیل کاملی از این داده‌ها ارائه دهید، شامل:
        1- روندهای کلیدی
        2- نسبت‌های مالی مهم
        3- توصیه‌های سرمایه‌گذاری
        4- هشدارها یا فرصت‌های احتمالی
        """
        
        # Get and send response
        response = query_deepseek(normalize_prompt(prompt), use_reasoner=True)
        await update.message.reply_text(f"📊 تحلیل داده‌های اکسل:\n\n{response}")
        
    except Exception as e:
        logger.error(f"Excel Error: {e}")
        await update.message.reply_text(f"❌ خطا در پردازش فایل اکسل: {str(e)}")

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect user feedback"""
    query = update.callback_query
    await query.answer()
    
    feedback_type = query.data.split("_")[1]
    logger.info(f"Feedback received: {feedback_type}")
    
    # Store feedback (can be extended to database)
    with open("feedback.log", "a") as f:
        f.write(f"{time.time()},{feedback_type}\n")
    
    await query.edit_message_text("🙏 از بازخورد شما متشکریم!")

async def get_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch and send financial news"""
    keywords = " ".join(context.args) if context.args else ""
    
    await update.message.reply_text("⏳ در حال دریافت اخبار مالی...")
    
    news = get_financial_news(keywords)
    
    if not news or "error" in news[0]["title"]:
        await update.message.reply_text("❌ خطا در دریافت اخبار. لطفاً بعداً تلاش کنید.")
        return
    
    # Format news without markdown
    news_text = "📰 آخرین اخبار مالی\n\n"
    for item in news:
        sentiment_emoji = "😐"
        if item["sentiment"] == "positive":
            sentiment_emoji = "🟢"
        elif item["sentiment"] == "negative":
            sentiment_emoji = "🔴"
            
        news_text += f"{item['title']} {sentiment_emoji}\n"
        news_text += f"{item['summary']}\n"
        news_text += f"لینک خبر: {item['url']}\n\n"
    
    await update.message.reply_text(news_text, disable_web_page_preview=True)


async def get_stock_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch and analyze stock information"""
    if not context.args:
        await update.message.reply_text("❌ لطفاً نماد سهام را وارد کنید. مثال: /stock AAPL")
        return
    
    symbol = context.args[0].upper()
    await update.message.reply_text(f"⏳ در حال دریافت اطلاعات سهام {symbol}...")
    
    stock_data = get_stock_data(symbol)
    
    if "error" in stock_data:
        await update.message.reply_text(f"❌ خطا: {stock_data['error']}")
        return
    
    # Format stock information
    profile = stock_data["profile"]
    ratios = stock_data["ratios"]
    
    info_text = f"📈 اطلاعات سهام {symbol}\n\n"
    info_text += f"{profile.get('companyName', 'N/A')}\n"
    info_text += f"قیمت: ${profile.get('price', 'N/A')}\n"
    info_text += f"تغییر: {profile.get('changes', 'N/A')} ({profile.get('changesPercentage', 'N/A')}%)\n"
    info_text += f"صنعت: {profile.get('industry', 'N/A')}\n\n"
    
    info_text += "نسبت‌های مالی:\n"
    if ratios:
        info_text += f"P/E: {ratios.get('priceEarningsRatio', 'N/A')}\n"
        info_text += f"P/B: {ratios.get('priceToBookRatio', 'N/A')}\n"
        info_text += f"ROE: {ratios.get('returnOnEquity', 'N/A')}\n"
        info_text += f"ROA: {ratios.get('returnOnAssets', 'N/A')}\n"
        info_text += f"Debt to Equity: {ratios.get('debtToEquity', 'N/A')}\n"
    else:
        info_text += "اطلاعات نسبت‌های مالی در دسترس نیست.\n"
    
    # Add analysis using AI
    analysis_prompt = f"""
    تحلیل سهام زیر را انجام دهید:
    
    نام شرکت: {profile.get('companyName', 'N/A')}
    قیمت فعلی: ${profile.get('price', 'N/A')}
    تغییر قیمت: {profile.get('changes', 'N/A')} ({profile.get('changesPercentage', 'N/A')}%)
    صنعت: {profile.get('industry', 'N/A')}
    توضیحات: {profile.get('description', 'N/A')}
    
    نسبت‌های مالی:
    P/E: {ratios.get('priceEarningsRatio', 'N/A')}
    P/B: {ratios.get('priceToBookRatio', 'N/A')}
    ROE: {ratios.get('returnOnEquity', 'N/A')}
    ROA: {ratios.get('returnOnAssets', 'N/A')}
    Debt to Equity: {ratios.get('debtToEquity', 'N/A')}
    
    لطفاً یک تحلیل کوتاه و دقیق از وضعیت این سهام ارائه دهید و توصیه‌های سرمایه‌گذاری مناسب را بیان کنید.
    """
    
    analysis = query_deepseek(normalize_prompt(analysis_prompt), use_reasoner=True)
    info_text += f"\nتحلیل هوشمند:\n{analysis}"
    
    await update.message.reply_text(info_text)


async def market_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provide a summary of current market conditions"""
    await update.message.reply_text("⏳ در حال تهیه خلاصه بازار...")
    
    try:
        # Get major indices data
        indices = ["^GSPC", "^DJI", "^IXIC", "^FTSE", "^N225"]
        indices_names = {
            "^GSPC": "S&P 500",
            "^DJI": "Dow Jones",
            "^IXIC": "Nasdaq",
            "^FTSE": "FTSE 100",
            "^N225": "Nikkei 225"
        }
        
        indices_data = {}
        for idx in indices:
            data = get_stock_data(idx)
            if "error" not in data:
                indices_data[indices_names.get(idx, idx)] = data
        
        # Get top gainers and losers
        gainers_url = f"{FMP_BASE_URL}/stock_market/gainers?apikey={FINANCIAL_MODELING_PREP_API_KEY}"
        gainers_response = requests.get(gainers_url, timeout=10)
        gainers_data = gainers_response.json()[:5]  # Top 5 gainers
        
        losers_url = f"{FMP_BASE_URL}/stock_market/losers?apikey={FINANCIAL_MODELING_PREP_API_KEY}"
        losers_response = requests.get(losers_url, timeout=10)
        losers_data = losers_response.json()[:5]  # Top 5 losers
        
        # Format market summary
        summary_text = "🌐 خلاصه وضعیت بازار\n\n"
        
        # Add indices
        summary_text += "شاخص‌های اصلی:\n"
        for name, data in indices_data.items():
            profile = data.get("profile", {})
            summary_text += f"{name}: ${profile.get('price', 'N/A')} ({profile.get('changesPercentage', 'N/A')}%)\n"
        
        # Add gainers
        summary_text += "\nبیشترین رشد:\n"
        for item in gainers_data:
            summary_text += f"{item.get('symbol', 'N/A')} ({item.get('companyName', 'N/A')}): "
            summary_text += f"${item.get('price', 'N/A')} ({item.get('changesPercentage', 'N/A')}%)\n"
        
        # Add losers
        summary_text += "\nبیشترین افت:\n"
        for item in losers_data:
            summary_text += f"{item.get('symbol', 'N/A')} ({item.get('companyName', 'N/A')}): "
            summary_text += f"${item.get('price', 'N/A')} ({item.get('changesPercentage', 'N/A')}%)\n"
        
        # Get AI analysis of market conditions
        market_prompt = f"""
        با توجه به داده‌های زیر، یک تحلیل کوتاه از وضعیت کلی بازار ارائه دهید:
        
        شاخص‌های اصلی:
        {', '.join([f"{name}: {data.get('profile', {}).get('changesPercentage', 'N/A')}%" for name, data in indices_data.items()])}
        
        بیشترین رشد:
        {', '.join([f"{item.get('symbol', 'N/A')}: {item.get('changesPercentage', 'N/A')}%" for item in gainers_data])}
        
        بیشترین افت:
        {', '.join([f"{item.get('symbol', 'N/A')}: {item.get('changesPercentage', 'N/A')}%" for item in losers_data])}
        
        لطفاً یک تحلیل کلی از روند بازار، بخش‌های قوی و ضعیف، و پیش‌بینی کوتاه‌مدت ارائه دهید.
        """
        
        market_analysis = query_deepseek(normalize_prompt(market_prompt), use_reasoner=True)
        summary_text += f"\nتحلیل بازار:\n{market_analysis}"
        
        await update.message.reply_text(summary_text)
        
    except Exception as e:
        logger.error(f"Market summary error: {e}")
        await update.message.reply_text(f"❌ خطا در دریافت خلاصه بازار: {str(e)}")




async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display help information"""
    help_text = "🤖 راهنمای ربات تحلیل مالی\n\n"
    help_text += "دستورات اصلی:\n"
    help_text += "/start - شروع کار با ربات و انتخاب سطح دانش\n"
    help_text += "/help - نمایش این راهنما\n"
    help_text += "/news [کلمات کلیدی] - دریافت آخرین اخبار مالی\n"
    help_text += "/stock [نماد] - دریافت اطلاعات و تحلیل سهام\n"
    help_text += "/market - خلاصه وضعیت بازار\n\n"
    help_text += "دستورات بورس ایران:\n"
    help_text += "/iran_market - وضعیت کلی بازار بورس ایران\n"
    help_text += "/iran_stock [نماد] - تحلیل سهام بورس ایران (مثال: /iran_stock خودرو)\n"
    help_text += "/codal [نماد] - دریافت گزارش‌های کدال یک شرکت (مثال: /codal خودرو)\n\n"
    help_text += "قابلیت‌های ربات:\n"
    help_text += "• پاسخ به سوالات مالی با توجه به سطح دانش شما\n"
    help_text += "• تحلیل فایل‌های PDF صورت‌های مالی\n"
    help_text += "• تحلیل فایل‌های Excel داده‌های مالی\n"
    help_text += "• دریافت اخبار مالی جهانی\n"
    help_text += "• تحلیل سهام و بازارهای جهانی\n"
    help_text += "• دریافت اطلاعات بازار بورس ایران\n"
    help_text += "• تحلیل سهام بورس ایران\n"
    help_text += "• دسترسی به گزارش‌های کدال\n\n"
    help_text += "برای استفاده از قابلیت تحلیل فایل، کافیست فایل PDF یا Excel خود را ارسال کنید.\n"
    help_text += "برای پرسش سوالات مالی، متن سوال خود را بنویسید."
    
    await update.message.reply_text(help_text)


async def iran_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش وضعیت کلی بازار بورس ایران"""
    try:
        await update.message.reply_text("⏳ در حال دریافت اطلاعات بازار بورس ایران...")
        
        # دریافت داده‌های ساختگی بازار
        market_data = get_iran_market_data()
        
        response = f"📊 وضعیت بازار بورس ایران\n\n"
        response += f"🕒 زمان: {market_data['timestamp']}\n"
        response += f"🏛 وضعیت بازار: {market_data['market_status']}\n"
        response += f"📈 شاخص کل: {market_data['overall_index']}\n"
        response += f"💰 ارزش بازار: {market_data['market_value']}\n"
        response += f"📊 حجم معاملات: {market_data['trade_volume']}\n"
        response += f"🔄 روند کلی بازار: {market_data['market_trend']}\n\n"
        response += f"وضعیت نمادها:\n"
        response += f"🟢 نمادهای مثبت: {market_data['positive_symbols']}\n"
        response += f"🔴 نمادهای منفی: {market_data['negative_symbols']}\n"
        response += f"⚪ نمادهای بدون تغییر: {market_data['neutral_symbols']}\n"
        
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"خطا در اجرای دستور iran_market: {e}")
        await update.message.reply_text("❌ خطا در دریافت اطلاعات بازار. لطفاً بعداً تلاش کنید.")

async def iran_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تحلیل سهام بورس ایران"""
    try:
        if not context.args:
            await update.message.reply_text("❌ لطفاً نماد سهام را وارد کنید. مثال: /iran_stock خودرو")
            return
        
        symbol = context.args[0]
        await update.message.reply_text(f"⏳ در حال دریافت اطلاعات سهام {symbol}...")
        
        # دریافت داده‌های ساختگی سهام
        stock_data = get_iran_stock_data(symbol)
        
        # ساخت متن تاریخچه
        history_text = "\n📅 تاریخچه قیمت (7 روز اخیر):\n"
        for item in stock_data["history"]:
            history_text += f"- {item['date']}: {item['close_price']} (حجم: {item['volume']})\n"
        
        response = f"🔍 اطلاعات سهام {stock_data['symbol']}\n\n"
        response += f"📝 نام کامل: {stock_data['full_name']}\n"
        response += f"💰 قیمت: {stock_data['price']} ریال\n"
        response += f"📊 تغییرات: {stock_data['change_percent']}\n"
        response += f"🏭 صنعت: {stock_data['industry']}\n"
        response += f"💼 ارزش بازار: {stock_data['market_cap']}\n"
        response += f"📈 نسبت P/E: {stock_data['p/e']}\n"
        response += f"💵 EPS: {stock_data['eps']} ریال\n"
        response += f"🕒 زمان: {stock_data['timestamp']}\n"
        response += history_text
        
        # تحلیل هوشمند با استفاده از AI
        analysis_prompt = f"""
        تحلیل سهام زیر از بازار بورس ایران را انجام دهید:
        
        نام شرکت: {stock_data['full_name']}
        نماد: {stock_data['symbol']}
        قیمت فعلی: {stock_data['price']} ریال
        تغییرات: {stock_data['change_percent']}
        صنعت: {stock_data['industry']}
        ارزش بازار: {stock_data['market_cap']}
        نسبت P/E: {stock_data['p/e']}
        EPS: {stock_data['eps']} ریال
        
        تاریخچه قیمت (7 روز اخیر):
        {', '.join([f"{item['date']}: {item['close_price']}" for item in stock_data["history"]])}
        
        لطفاً یک تحلیل کوتاه و دقیق از وضعیت این سهام ارائه دهید و توصیه‌های سرمایه‌گذاری مناسب را بیان کنید.
        """
        
        analysis = query_deepseek(normalize_prompt(analysis_prompt), use_reasoner=True)
        response += f"\nتحلیل هوشمند:\n{analysis}"
        
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"خطا در اجرای دستور iran_stock: {e}")
        await update.message.reply_text("❌ خطا در دریافت اطلاعات سهام. لطفاً بعداً تلاش کنید.")


async def codal_reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت گزارش‌های کدال برای یک شرکت"""
    try:
        if not context.args:
            await update.message.reply_text("❌ لطفاً نماد شرکت را وارد کنید. مثال: /codal خودرو")
            return
        
        symbol = context.args[0]
        await update.message.reply_text(f"⏳ در حال دریافت گزارش‌های کدال برای {symbol}...")
        
        # دریافت داده‌های ساختگی گزارش‌های کدال
        codal_data = get_codal_reports(symbol)
        
        response = f"📑 گزارش‌های کدال برای {codal_data['company_name']} ({codal_data['symbol']})\n\n"
        response += f"تعداد گزارش‌های یافت شده: {len(codal_data['reports'])}\n\n"
        response += "گزارش‌های اخیر:\n"
        
        for i, report in enumerate(codal_data['reports'], 1):
            response += f"{i}. {report['date']} - {report['title']} ({report['category']})\n"
            response += f"   لینک گزارش: {report['url']}\n"
        
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"خطا در اجرای دستور codal: {e}")
        await update.message.reply_text("❌ خطا در دریافت گزارش‌های کدال. لطفاً بعداً تلاش کنید.")


# --- Main Execution ---
def run_bot():
    """Configure and run the bot"""
    try:
        print(f"Attempting to create bot with token: {TELEGRAM_TOKEN[:5]}...")
        
        # تست اتصال به دیپ‌سیک
        if not test_deepseek_connection():
            print("⚠ هشدار: اتصال به API دیپ‌سیک با مشکل مواجه شد. ربات با قابلیت‌های محدود اجرا می‌شود.")
        
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("news", get_news))
        application.add_handler(CommandHandler("stock", get_stock_info))
        application.add_handler(CommandHandler("market", market_summary))
        
        # دستورات بورس ایران با هر دو فرمت
        application.add_handler(CommandHandler("iran_market", iran_market))
        application.add_handler(CommandHandler("iranmarket", iran_market))  # بدون آندرلاین
        application.add_handler(CommandHandler("iran_stock", iran_stock))
        application.add_handler(CommandHandler("iranstock", iran_stock))  # بدون آندرلاین
        
        # دستورات کدال
        application.add_handler(CommandHandler("codal", codal_reports_command))
        
        # سایر هندلرها
        application.add_handler(CallbackQueryHandler(set_knowledge_level, pattern="^level_"))
        application.add_handler(CallbackQueryHandler(handle_feedback, pattern="^feedback_"))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        
        # توقف مطمئن
        def signal_handler(sig, frame):
            print("\nدریافت سیگنال توقف. در حال خروج از برنامه...")
            application.stop()
            sys.exit(0)        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        print("ربات در حال اجراست. برای توقف، Ctrl+C را فشار دهید.")
        application.run_polling()
    except Exception as e:
        print(f"Error starting bot: {e}")
        sys.exit(1)

if __name__ == "__main__":    run_bot()
 