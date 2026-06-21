from typing import Dict, Optional, List
import os
import re
import json
import time
import threading
import asyncio
import requests
import logging
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
from telethon.tl.types import Channel
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# ==================== تنظیمات پایه ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 🔐 مقادیر خودت رو اینجا وارد کن
BOT_TOKEN = "8807306409:AAG7rsRNlxnbe1jayn66cKHYsUKVKvvLOR8"
INITIAL_ADMIN_ID = 5698242770  # آیدی عددی خودت

# 📁 مسیرها
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
SESSION_DIR = os.path.join(BASE_DIR, "accounts")
MEDIA_DIR = os.path.join(BASE_DIR, "media")

# ایجاد دایرکتوری‌ها
for dir_path in [CONFIG_DIR, SESSION_DIR, MEDIA_DIR]:
    os.makedirs(dir_path, exist_ok=True)

# 📄 فایل‌های کانفیگ
ACCOUNTS_INDEX = os.path.join(CONFIG_DIR, "accounts.json")
ADMINS_FILE = os.path.join(CONFIG_DIR, "admins.json")
GLOBAL_FILE = os.path.join(CONFIG_DIR, "global.json")
API_CONFIG_FILE = os.path.join(CONFIG_DIR, "api_config.json")

# ==================== فایل‌های پیش‌فرض ====================
if not os.path.exists(ADMINS_FILE):
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump({"admins": [INITIAL_ADMIN_ID]}, f, ensure_ascii=False, indent=2)

if not os.path.exists(ACCOUNTS_INDEX):
    with open(ACCOUNTS_INDEX, "w", encoding="utf-8") as f:
        json.dump({"accounts": []}, f, ensure_ascii=False, indent=2)

if not os.path.exists(GLOBAL_FILE):
    with open(GLOBAL_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "active": False,  # 👈 دیگه پیش‌فرض غیرفعاله
            "interval": 60,
            "banner": {
                "type": "text",
                "content": ""  # 👈 خالی خالی
            }
        }, f, ensure_ascii=False, indent=2)

if not os.path.exists(API_CONFIG_FILE):
    with open(API_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "api_id": None,
            "api_hash": None
        }, f, ensure_ascii=False, indent=2)

# ==================== توابع کمکی ====================
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        save_json(path, default)
        return default
    except json.JSONDecodeError:
        logger.error(f"فایل {path} خراب است. ریست شد.")
        save_json(path, default)
        return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"خطا در ذخیره {path}: {e}")

def sanitize_name(base: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", base)

def is_group_or_mega(dialog) -> bool:
    try:
        if dialog.is_group:
            return True
        ent = dialog.entity
        if isinstance(ent, Channel) and getattr(ent, "megagroup", False):
            return True
        return False
    except:
        return False

def ensure_media_dir(name: str) -> str:
    d = os.path.join(MEDIA_DIR, name)
    os.makedirs(d, exist_ok=True)
    return d

def now_ts():
    return int(time.time())

# ==================== Rate Limiter ====================
class RateLimiter:
    """محدود کننده نرخ ارسال برای جلوگیری از بن شدن"""
    
    def __init__(self, max_per_second=1):
        self.max_per_second = max_per_second
        self.tokens = max_per_second
        self.updated_at = time.time()
        self.lock = threading.Lock()
    
    def acquire(self):
        with self.lock:
            now = time.time()
            self.tokens += (now - self.updated_at) * self.max_per_second
            self.updated_at = now
            if self.tokens > self.max_per_second:
                self.tokens = self.max_per_second
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False
    
    async def wait_if_needed(self):
        while not self.acquire():
            await asyncio.sleep(0.1)

# ==================== مدیریت API ====================
class APIManager:
    """مدیریت متمرکز API ID و Hash"""
    
    def __init__(self):
        self.config_file = API_CONFIG_FILE
        self._load_config()
        self.lock = threading.Lock()
    
    def _load_config(self):
        self.config = load_json(self.config_file, {
            "api_id": None,
            "api_hash": None
        })
    
    def save_config(self):
        with self.lock:
            save_json(self.config_file, self.config)
    
    def set_api(self, api_id: int, api_hash: str):
        try:
            self.config["api_id"] = int(api_id)
            self.config["api_hash"] = str(api_hash).strip()
            self.save_config()
            logger.info("API ID و Hash با موفقیت ذخیره شدند")
            return True
        except Exception as e:
            logger.error(f"خطا در ذخیره API: {e}")
            return False
    
    def get_api(self):
        return self.config.get("api_id"), self.config.get("api_hash")
    
    def is_configured(self):
        api_id = self.config.get("api_id")
        api_hash = self.config.get("api_hash")
        return api_id is not None and api_hash is not None and api_id > 0 and len(api_hash) > 5
    
    def clear_api(self):
        self.config["api_id"] = None
        self.config["api_hash"] = None
        self.save_config()
        logger.info("API پاک شد")

# ==================== کلاس تبچی ====================
class TabchiAccount:
    def __init__(self, name: str, api_id: int, api_hash: str, phone: str, 
                 client: TelegramClient, loop: asyncio.AbstractEventLoop):
        self.name = name
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.client = client
        self.loop = loop
        self.config_path = os.path.join(CONFIG_DIR, f"{name}.json")
        self.media_dir = ensure_media_dir(name)
        self.rate_limiter = RateLimiter(max_per_second=1)  # 👈 محدود کننده
        
        # بارگذاری تنظیمات - کاملاً خالی
        default_settings = {
            "active": False,  # 👈 پیش‌فرض غیرفعال
            "interval": 30,
            "banner": {
                "type": "text",
                "content": ""  # 👈 خالی
            }
        }
        self.settings = load_json(self.config_path, default_settings)
        
        # اگه فایل قبلی بود و محتوای پیش‌فرض داشت، پاکش کن
        if self.settings.get("banner", {}).get("content") == "نود رایگان پی":
            self.settings = default_settings
            self.save_settings()
        
        self._task_personal: Optional[asyncio.Task] = None
        self._running = True

    def save_settings(self):
        save_json(self.config_path, self.settings)

    async def start(self):
        try:
            if not self.client.is_connected():
                await self.client.connect()
            if self._task_personal is None or self._task_personal.done():
                self._task_personal = self.loop.create_task(self._personal_loop())
            logger.info(f"اکانت {self.name} شروع به کار کرد")
        except Exception as e:
            logger.error(f"خطا در شروع اکانت {self.name}: {e}")

    async def stop(self):
        self._running = False
        if self._task_personal and not self._task_personal.done():
            self._task_personal.cancel()
            try:
                await self._task_personal
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"خطا در توقف اکانت {self.name}: {e}")
        logger.info(f"اکانت {self.name} متوقف شد")

    async def _personal_loop(self):
        while self._running:
            try:
                if self.settings.get("active", False):
                    async for dialog in self.client.iter_dialogs():
                        if not self._running:
                            break
                        if is_group_or_mega(dialog):
                            await self._send_banner(dialog.id)
                            await asyncio.sleep(2)  # 👈 تأخیر بین ارسال‌ها
                
                # خواب بر اساس تنظیمات (تبدیل به ثانیه)
                sleep_time = self.settings.get("interval", 30) * 60
                for _ in range(int(sleep_time / 5)):
                    if not self._running:
                        break
                    await asyncio.sleep(5)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"خطا در حلقه شخصی {self.name}: {e}")
                await asyncio.sleep(10)

    async def _send_banner(self, chat_id):
        """ارسال بنر با محدودیت نرخ"""
        banner = self.settings.get("banner", {"type": "text", "content": ""})
        btype = banner.get("type", "text")
        
        # اگه محتوا خالی بود، هیچی نفرست
        if btype == "text" and not banner.get("content", "").strip():
            return
        if btype in ("photo", "video") and not banner.get("file_path"):
            return
        
        try:
            # اعمال محدودیت نرخ
            await self.rate_limiter.wait_if_needed()
            
            if btype == "text":
                content = banner.get("content", "").strip()
                if content:
                    await self.client.send_message(chat_id, content)
                    logger.debug(f"بنر متنی به {chat_id} ارسال شد")
                    
            elif btype == "photo":
                file_path = banner.get("file_path")
                caption = banner.get("caption", "")
                if file_path and os.path.exists(file_path):
                    await self.client.send_file(chat_id, file_path, caption=caption)
                    logger.debug(f"بنر عکس به {chat_id} ارسال شد")
                    
            elif btype == "video":
                file_path = banner.get("file_path")
                caption = banner.get("caption", "")
                if file_path and os.path.exists(file_path):
                    await self.client.send_file(chat_id, file_path, caption=caption)
                    logger.debug(f"بنر ویدیو به {chat_id} ارسال شد")
                    
        except Exception as e:
            logger.error(f"خطا در ارسال بنر به {chat_id}: {e}")

    async def send_now_to_all(self):
        """ارسال دستی به همه گروه‌ها"""
        try:
            sent_count = 0
            async for dialog in self.client.iter_dialogs():
                if is_group_or_mega(dialog):
                    await self._send_banner(dialog.id)
                    sent_count += 1
                    await asyncio.sleep(2)  # تأخیر بین ارسال‌ها
            logger.info(f"{self.name}: {sent_count} بنر ارسال شد")
            return True
        except Exception as e:
            logger.error(f"خطا در ارسال دستی {self.name}: {e}")
            return False

# ==================== کلاس مدیریت تبچی‌ها ====================
class TabchiManager:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.accounts: Dict[str, TabchiAccount] = {}
        self.pending: Dict[str, TelegramClient] = {}
        self.api_manager = APIManager()
        
        # بارگذاری تنظیمات عمومی
        default_global = {
            "active": False,
            "interval": 60,
            "banner": {
                "type": "text",
                "content": ""
            }
        }
        self.global_settings = load_json(GLOBAL_FILE, default_global)
        
        # پاکسازی محتوای پیش‌فرض قدیمی
        if self.global_settings.get("banner", {}).get("content") == "نود رایگان پی":
            self.global_settings = default_global
            save_json(GLOBAL_FILE, self.global_settings)
        
        self._task_global: Optional[asyncio.Task] = None
        self._running = True

    def list_accounts_index(self) -> List[dict]:
        return load_json(ACCOUNTS_INDEX, {"accounts": []}).get("accounts", [])

    def save_accounts_index(self, accounts: List[dict]):
        save_json(ACCOUNTS_INDEX, {"accounts": accounts})

    async def load_existing(self):
        """بارگذاری اکانت‌های موجود"""
        idx = self.list_accounts_index()
        api_id, api_hash = self.api_manager.get_api()
        
        for rec in idx:
            name = rec["name"]
            phone = rec["phone"]
            
            acc_api_id = rec.get("api_id", api_id)
            acc_api_hash = rec.get("api_hash", api_hash)
            
            if not acc_api_id or not acc_api_hash:
                logger.warning(f"اکانت {name}: اطلاعات API ناقص")
                continue
                
            session_path = os.path.join(SESSION_DIR, name)
            client = TelegramClient(session_path, acc_api_id, acc_api_hash)
            
            try:
                await client.connect()
                if await client.is_user_authorized():
                    acc = TabchiAccount(name, acc_api_id, acc_api_hash, phone, client, self.loop)
                    self.accounts[name] = acc
                    await acc.start()
                    logger.info(f"اکانت {name} بارگذاری شد")
                else:
                    logger.warning(f"اکانت {name}: نیاز به ورود مجدد")
                    await client.disconnect()
            except Exception as e:
                logger.error(f"خطا در بارگذاری اکانت {name}: {e}")
        
        # شروع حلقه عمومی
        if self._task_global is None or self._task_global.done():
            self._task_global = self.loop.create_task(self._global_loop())

    async def _global_loop(self):
        """حلقه ارسال عمومی"""
        while self._running:
            try:
                if self.global_settings.get("active", False):
                    for name, acc in list(self.accounts.items()):
                        if not self._running:
                            break
                        if acc.settings.get("active", False):
                            try:
                                async for dialog in acc.client.iter_dialogs():
                                    if not self._running:
                                        break
                                    if is_group_or_mega(dialog):
                                        await self._send_global_banner(acc.client, dialog.id)
                                        await asyncio.sleep(2)
                            except Exception as e:
                                logger.error(f"خطا در ارسال عمومی با {name}: {e}")
                
                sleep_time = self.global_settings.get("interval", 60) * 60
                for _ in range(int(sleep_time / 5)):
                    if not self._running:
                        break
                    await asyncio.sleep(5)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"خطا در حلقه عمومی: {e}")
                await asyncio.sleep(10)

    async def _send_global_banner(self, client: TelegramClient, chat_id):
        """ارسال بنر عمومی"""
        banner = self.global_settings.get("banner", {"type": "text", "content": ""})
        btype = banner.get("type", "text")
        
        # اگه محتوا خالی بود، هیچی نفرست
        if btype == "text" and not banner.get("content", "").strip():
            return
        if btype in ("photo", "video") and not banner.get("file_path"):
            return
        
        try:
            if btype == "text":
                content = banner.get("content", "").strip()
                if content:
                    await client.send_message(chat_id, content)
            elif btype in ("photo", "video"):
                file_path = banner.get("file_path")
                caption = banner.get("caption", "")
                if file_path and os.path.exists(file_path):
                    await client.send_file(chat_id, file_path, caption=caption)
        except Exception as e:
            logger.error(f"خطا در ارسال بنر عمومی به {chat_id}: {e}")

    async def start_code_request(self, name: str, phone: str):
        """درخواست کد ورود"""
        api_id, api_hash = self.api_manager.get_api()
        if not api_id or not api_hash:
            return "API_NOT_CONFIGURED"
        
        try:
            session_path = os.path.join(SESSION_DIR, name)
            client = TelegramClient(session_path, api_id, api_hash)
            await client.connect()
            await client.send_code_request(phone)
            self.pending[name] = client
            logger.info(f"کد برای {phone} درخواست شد")
            return "OK"
        except Exception as e:
            logger.error(f"خطا در درخواست کد: {e}")
            return "ERROR"

    async def finish_sign_in(self, name: str, phone: str, code: str):
        """تکمیل ورود"""
        api_id, api_hash = self.api_manager.get_api()
        if not api_id or not api_hash:
            return "API_NOT_CONFIGURED"
        
        client = self.pending.get(name)
        if not client:
            session_path = os.path.join(SESSION_DIR, name)
            client = TelegramClient(session_path, api_id, api_hash)
            await client.connect()
        
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            return "PASSWORD_REQUIRED"
        except PhoneCodeInvalidError:
            return "CODE_INVALID"
        except PhoneCodeExpiredError:
            return "CODE_EXPIRED"
        except Exception as e:
            logger.error(f"خطا در ورود: {e}")
            return "ERROR"
        
        # ساخت اکانت جدید
        acc = TabchiAccount(name, api_id, api_hash, phone, client, self.loop)
        self.accounts[name] = acc
        
        # ذخیره در ایندکس
        idx = self.list_accounts_index()
        idx.append({
            "name": name, 
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash
        })
        self.save_accounts_index(idx)
        
        if name in self.pending:
            del self.pending[name]
        
        await acc.start()
        logger.info(f"اکانت {name} با موفقیت اضافه شد")
        return "OK"

    async def finish_two_factor(self, name: str, password: str):
        """ورود دو مرحله‌ای"""
        client = self.pending.get(name)
        if not client:
            return "ERROR"
        
        try:
            await client.sign_in(password=password)
        except Exception as e:
            logger.error(f"خطا در 2FA: {e}")
            return "ERROR"
        
        idx = self.list_accounts_index()
        rec = next((r for r in idx if r["name"] == name), None)
        if not rec:
            return "ERROR"
        
        api_id, api_hash = self.api_manager.get_api()
        acc = TabchiAccount(name, api_id, api_hash, rec["phone"], client, self.loop)
        self.accounts[name] = acc
        
        if name in self.pending:
            del self.pending[name]
        
        await acc.start()
        logger.info(f"ورود 2FA برای {name} موفقیت‌آمیز بود")
        return "OK"

    async def toggle_active(self, name: str) -> bool:
        """تغییر وضعیت فعال/غیرفعال"""
        acc = self.accounts.get(name)
        if not acc:
            return False
        acc.settings["active"] = not acc.settings.get("active", False)
        acc.save_settings()
        status = "فعال" if acc.settings["active"] else "غیرفعال"
        logger.info(f"اکانت {name} {status} شد")
        return acc.settings["active"]

    async def set_interval(self, name: str, minutes: int) -> bool:
        """تنظیم فاصله ارسال"""
        acc = self.accounts.get(name)
        if not acc:
            return False
        minutes = max(1, min(1440, int(minutes)))  # بین 1 دقیقه تا 24 ساعت
        acc.settings["interval"] = minutes
        acc.save_settings()
        logger.info(f"تایم {name} روی {minutes} دقیقه تنظیم شد")
        return True

    async def reset_account(self, name: str) -> bool:
        """ریست کامل اکانت"""
        acc = self.accounts.get(name)
        if not acc:
            return False
        
        await acc.stop()
        
        try:
            await acc.client.disconnect()
        except:
            pass
        
        # پاک کردن فایل‌ها
        try:
            files_to_remove = [
                acc.config_path,
                os.path.join(SESSION_DIR, name + ".session"),
                os.path.join(SESSION_DIR, name)
            ]
            for f in files_to_remove:
                if os.path.exists(f):
                    os.remove(f)
        except Exception as e:
            logger.error(f"خطا در پاک کردن فایل‌های {name}: {e}")
        
        if name in self.accounts:
            del self.accounts[name]
        
        idx = self.list_accounts_index()
        idx = [r for r in idx if r["name"] != name]
        self.save_accounts_index(idx)
        
        logger.info(f"اکانت {name} ریست شد")
        return True

    async def manual_send(self, name: str) -> bool:
        """ارسال دستی"""
        acc = self.accounts.get(name)
        if not acc:
            return False
        return await acc.send_now_to_all()

    async def global_toggle(self) -> bool:
        """تغییر وضعیت عمومی"""
        self.global_settings["active"] = not self.global_settings.get("active", False)
        save_json(GLOBAL_FILE, self.global_settings)
        status = "فعال" if self.global_settings["active"] else "غیرفعال"
        logger.info(f"ارسال عمومی {status} شد")
        return self.global_settings["active"]

    async def global_set_interval(self, minutes: int) -> bool:
        """تنظیم تایم عمومی"""
        minutes = max(1, min(1440, int(minutes)))
        self.global_settings["interval"] = minutes
        save_json(GLOBAL_FILE, self.global_settings)
        logger.info(f"تایم عمومی روی {minutes} دقیقه تنظیم شد")
        return True

    def global_set_banner(self, banner: dict):
        """تنظیم بنر عمومی"""
        self.global_settings["banner"] = banner
        save_json(GLOBAL_FILE, self.global_settings)

    async def shutdown(self):
        """خاموش کردن تمیز"""
        self._running = False
        
        if self._task_global and not self._task_global.done():
            self._task_global.cancel()
            try:
                await self._task_global
            except:
                pass
        
        for acc in self.accounts.values():
            await acc.stop()
        
        logger.info("همه اکانت‌ها متوقف شدند")

# ==================== بات تلگرام ====================
bot = telebot.TeleBot(BOT_TOKEN)
admin_states: Dict[int, dict] = {}

def is_admin(chat_id: int) -> bool:
    admins = load_json(ADMINS_FILE, {"admins": [INITIAL_ADMIN_ID]}).get("admins", [])
    return chat_id in admins

# ==================== کیبوردها ====================
def kb_main():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ افزودن تبچی", callback_data="add_account"),
        InlineKeyboardButton("📋 لیست تبچی‌ها", callback_data="list_accounts"),
        InlineKeyboardButton("🧷 بنر عمومی", callback_data="global_menu"),
        InlineKeyboardButton("🔧 تنظیمات API", callback_data="api_settings"),
        InlineKeyboardButton("👥 مدیریت ادمین", callback_data="admins_menu"),
        InlineKeyboardButton("📚 راهنما", callback_data="help_menu"),
    )
    return kb

def kb_account_list(names: List[str]):
    kb = InlineKeyboardMarkup()
    for n in names:
        kb.add(InlineKeyboardButton(f"👤 {n}", callback_data=f"acc_panel:{n}"))
    kb.add(InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    return kb

def kb_account_panel(name: str, active: bool, interval: int, banner_type: str):
    kb = InlineKeyboardMarkup(row_width=1)
    buttons = [
        InlineKeyboardButton("📝 تنظیم بنر", callback_data=f"acc:{name}:set_banner"),
        InlineKeyboardButton("📄 نمایش بنر", callback_data=f"acc:{name}:show_banner"),
        InlineKeyboardButton("⏱ تنظیم تایم", callback_data=f"acc:{name}:set_interval"),
        InlineKeyboardButton(f"{'🔴 غیرفعال' if active else '🟢 فعال'} کردن", 
                           callback_data=f"acc:{name}:toggle_active"),
        InlineKeyboardButton("🚀 ارسال دستی", callback_data=f"acc:{name}:manual_send"),
        InlineKeyboardButton("🗑 حذف اکانت", callback_data=f"acc:{name}:reset"),
        InlineKeyboardButton("🔙 بازگشت", callback_data="list_accounts"),
    ]
    kb.add(*buttons)
    return kb

def kb_global_menu(active: bool, interval: int):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📝 تنظیم بنر", callback_data="global:set_banner"),
        InlineKeyboardButton("📄 نمایش بنر", callback_data="global:show_banner"),
        InlineKeyboardButton("⏱ تنظیم تایم", callback_data="global:set_interval"),
        InlineKeyboardButton(f"{'🔴 غیرفعال' if active else '🟢 فعال'} کردن عمومی", 
                           callback_data="global:toggle"),
        InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"),
    )
    return kb

def kb_api_settings(configured: bool):
    kb = InlineKeyboardMarkup(row_width=1)
    if configured:
        kb.add(
            InlineKeyboardButton("✏️ تغییر API", callback_data="api:set"),
            InlineKeyboardButton("👁 نمایش API", callback_data="api:show"),
            InlineKeyboardButton("🗑 پاک کردن API", callback_data="api:clear"),
        )
    else:
        kb.add(InlineKeyboardButton("⚙️ تنظیم API", callback_data="api:set"))
    kb.add(InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    return kb

def kb_code_pad(name: str, current_code: str):
    kb = InlineKeyboardMarkup()
    # ردیف 1-3
    kb.row(
        InlineKeyboardButton("1", callback_data=f"code:{name}:digit:1"),
        InlineKeyboardButton("2", callback_data=f"code:{name}:digit:2"),
        InlineKeyboardButton("3", callback_data=f"code:{name}:digit:3"),
    )
    # ردیف 4-6
    kb.row(
        InlineKeyboardButton("4", callback_data=f"code:{name}:digit:4"),
        InlineKeyboardButton("5", callback_data=f"code:{name}:digit:5"),
        InlineKeyboardButton("6", callback_data=f"code:{name}:digit:6"),
    )
    # ردیف 7-9
    kb.row(
        InlineKeyboardButton("7", callback_data=f"code:{name}:digit:7"),
        InlineKeyboardButton("8", callback_data=f"code:{name}:digit:8"),
        InlineKeyboardButton("9", callback_data=f"code:{name}:digit:9"),
    )
    # ردیف صفر
    kb.row(InlineKeyboardButton("0", callback_data=f"code:{name}:digit:0"))
    # ردیف عملیات
    kb.row(
        InlineKeyboardButton("⌫", callback_data=f"code:{name}:backspace"),
        InlineKeyboardButton("❌ پاک", callback_data=f"code:{name}:clear"),
        InlineKeyboardButton("✅ تایید", callback_data=f"code:{name}:submit"),
    )
    kb.add(InlineKeyboardButton("🔙 لغو", callback_data="cancel_add_account"))
    return kb

def kb_cancel():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 لغو", callback_data="cancel_add_account"))
    return kb

def kb_admins_menu():
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("➕ افزودن ادمین", callback_data="admins:add"),
        InlineKeyboardButton("➖ حذف ادمین", callback_data="admins:remove"),
        InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"),
    )
    return kb

# ==================== هندلرهای پیام ====================
@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "video"])
def on_message(msg: Message):
    chat_id = msg.chat.id
    if not is_admin(chat_id):
        return
    
    state = admin_states.get(chat_id, {"state": None, "data": {}})
    st = state.get("state")

    # تنظیم API ID
    if st == "SET_API_ID":
        try:
            api_id = int(msg.text.strip())
            state["data"]["api_id"] = api_id
            state["state"] = "SET_API_HASH"
            admin_states[chat_id] = state
            bot.send_message(chat_id, "🔑 API Hash را ارسال کن:", reply_markup=kb_cancel())
        except:
            bot.reply_to(msg, "❌ API ID باید عدد باشد.", reply_markup=kb_cancel())
        return

    # تنظیم API Hash
    if st == "SET_API_HASH":
        api_hash = msg.text.strip()
        if len(api_hash) < 5:
            bot.reply_to(msg, "❌ API Hash نامعتبر است.", reply_markup=kb_cancel())
            return
        
        api_id = state["data"]["api_id"]
        if manager.api_manager.set_api(api_id, api_hash):
            bot.send_message(chat_id, "✅ API با موفقیت ذخیره شد!")
        else:
            bot.send_message(chat_id, "❌ خطا در ذخیره API")
        
        admin_states[chat_id] = {"state": None, "data": {}}
        bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())
        return

    # افزودن تبچی - دریافت شماره
    if st == "ADD_PHONE":
        phone = msg.text.strip()
        if not phone.startswith("+") or not re.fullmatch(r"\+[\d]{8,15}", phone):
            bot.reply_to(msg, "❌ شماره نامعتبر. فرمت: +98912...", reply_markup=kb_cancel())
            return
        
        if not manager.api_manager.is_configured():
            bot.reply_to(msg, "❌ ابتدا API را تنظیم کن!", reply_markup=kb_main())
            admin_states[chat_id] = {"state": None, "data": {}}
            return
        
        # ساخت نام یکتا
        base_name = sanitize_name(phone.replace("+", ""))
        name = base_name
        idx = 1
        existing = [a.get("name") for a in manager.list_accounts_index()]
        while os.path.exists(os.path.join(SESSION_DIR, name + ".session")) or name in existing:
            name = f"{base_name}_{idx}"
            idx += 1
        
        state["data"]["phone"] = phone
        state["data"]["name"] = name
        admin_states[chat_id] = state
        
        bot.send_message(chat_id, f"⏳ درخواست کد برای {phone}...")
        
        future = asyncio.run_coroutine_threadsafe(
            manager.start_code_request(name, phone), loop
        )
        
        try:
            res = future.result(timeout=30)
            if res == "API_NOT_CONFIGURED":
                bot.send_message(chat_id, "❌ API تنظیم نشده!")
                admin_states[chat_id] = {"state": None, "data": {}}
                return
            elif res != "OK":
                bot.send_message(chat_id, f"❌ خطا: {res}")
                admin_states[chat_id] = {"state": None, "data": {}}
                return
        except Exception as e:
            bot.send_message(chat_id, f"❌ خطا: {e}")
            admin_states[chat_id] = {"state": None, "data": {}}
            return
        
        state["state"] = "ADD_CODE"
        state["data"]["code"] = ""
        admin_states[chat_id] = state
        
        bot.send_message(
            chat_id,
            f"🔐 کد رو وارد کن:\nاکانت: {name}\nکد: {state['data']['code']}",
            reply_markup=kb_code_pad(name, state["data"]["code"])
        )
        return

    # تنظیم بنر اختصاصی
    if st and st.startswith("SET_BANNER_ACC:"):
        name = st.split(":", 1)[1]
        acc = manager.accounts.get(name)
        if not acc:
            bot.reply_to(msg, "❌ اکانت پیدا نشد")
            admin_states[chat_id] = {"state": None, "data": {}}
            return
        
        if msg.content_type == "text":
            text = msg.text.strip()
            if text:
                acc.settings["banner"] = {"type": "text", "content": text}
                acc.save_settings()
                bot.send_message(chat_id, f"✅ بنر متنی برای {name} ذخیره شد")
            else:
                bot.reply_to(msg, "❌ متن نمی‌تونه خالی باشه")
                return
                
        elif msg.content_type == "photo":
            photo = msg.photo[-1]
            file_info = bot.get_file(photo.file_id)
            filename = f"photo_{now_ts()}.jpg"
            dest = os.path.join(acc.media_dir, filename)
            
            dl = bot.download_file(file_info.file_path)
            with open(dest, "wb") as f:
                f.write(dl)
            
            caption = msg.caption or ""
            acc.settings["banner"] = {"type": "photo", "file_path": dest, "caption": caption}
            acc.save_settings()
            bot.send_message(chat_id, f"✅ بنر عکس برای {name} ذخیره شد")
            
        elif msg.content_type == "video":
            video = msg.video
            file_info = bot.get_file(video.file_id)
            filename = f"video_{now_ts()}.mp4"
            dest = os.path.join(acc.media_dir, filename)
            
            dl = bot.download_file(file_info.file_path)
            with open(dest, "wb") as f:
                f.write(dl)
            
            caption = msg.caption or ""
            acc.settings["banner"] = {"type": "video", "file_path": dest, "caption": caption}
            acc.save_settings()
            bot.send_message(chat_id, f"✅ بنر ویدیو برای {name} ذخیره شد")
            
        else:
            bot.reply_to(msg, "❌ فقط متن، عکس یا ویدیو بفرست")
            return
        
        admin_states[chat_id] = {"state": None, "data": {}}
        return

    # تنظیم بنر عمومی
    if st == "SET_GLOBAL_BANNER":
        if msg.content_type == "text":
            text = msg.text.strip()
            if text:
                manager.global_set_banner({"type": "text", "content": text})
                bot.send_message(chat_id, "✅ بنر عمومی متنی ذخیره شد")
            else:
                bot.reply_to(msg, "❌ متن نمی‌تونه خالی باشه")
                return
                
        elif msg.content_type == "photo":
            photo = msg.photo[-1]
            file_info = bot.get_file(photo.file_id)
            fname = f"global_photo_{now_ts()}.jpg"
            dest = os.path.join(MEDIA_DIR, fname)
            
            dl = bot.download_file(file_info.file_path)
            with open(dest, "wb") as f:
                f.write(dl)
            
            caption = msg.caption or ""
            manager.global_set_banner({"type": "photo", "file_path": dest, "caption": caption})
            bot.send_message(chat_id, "✅ بنر عمومی عکس ذخیره شد")
            
        elif msg.content_type == "video":
            file_info = bot.get_file(msg.video.file_id)
            fname = f"global_video_{now_ts()}.mp4"
            dest = os.path.join(MEDIA_DIR, fname)
            
            dl = bot.download_file(file_info.file_path)
            with open(dest, "wb") as f:
                f.write(dl)
            
            caption = msg.caption or ""
            manager.global_set_banner({"type": "video", "file_path": dest, "caption": caption})
            bot.send_message(chat_id, "✅ بنر عمومی ویدیو ذخیره شد")
            
        else:
            bot.reply_to(msg, "❌ فقط متن، عکس یا ویدیو بفرست")
            return
        
        admin_states[chat_id] = {"state": None, "data": {}}
        return

    # تنظیم تایم اختصاصی
    if st and st.startswith("SET_INTERVAL_ACC:"):
        name = st.split(":", 1)[1]
        try:
            minutes = int(msg.text.strip())
            future = asyncio.run_coroutine_threadsafe(manager.set_interval(name, minutes), loop)
            if future.result(timeout=10):
                bot.send_message(chat_id, f"✅ تایم {name} روی {minutes} دقیقه تنظیم شد")
            else:
                bot.send_message(chat_id, "❌ خطا در تنظیم تایم")
        except:
            bot.reply_to(msg, "❌ عدد دقیقه نامعتبر")
        
        admin_states[chat_id] = {"state": None, "data": {}}
        return

    # تنظیم تایم عمومی
    if st == "SET_INTERVAL_GLOBAL":
        try:
            minutes = int(msg.text.strip())
            future = asyncio.run_coroutine_threadsafe(manager.global_set_interval(minutes), loop)
            future.result(timeout=10)
            bot.send_message(chat_id, f"✅ تایم عمومی روی {minutes} دقیقه تنظیم شد")
        except:
            bot.reply_to(msg, "❌ عدد دقیقه نامعتبر")
        
        admin_states[chat_id] = {"state": None, "data": {}}
        return

    # رمز دو مرحله‌ای
    if st and st.startswith("TWOFA:"):
        name = st.split(":", 1)[1]
        password = msg.text.strip()
        
        future = asyncio.run_coroutine_threadsafe(manager.finish_two_factor(name, password), loop)
        try:
            res = future.result(timeout=30)
            if res == "OK":
                bot.send_message(chat_id, f"✅ اکانت {name} با موفقیت اضافه شد!")
            else:
                bot.send_message(chat_id, "❌ خطا در ورود 2FA")
        except:
            bot.send_message(chat_id, "❌ خطا در پردازش")
        
        admin_states[chat_id] = {"state": None, "data": {}}
        bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())
        return

    # منوی اصلی
    bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())

# ==================== هندلرهای Callback ====================
@bot.callback_query_handler(func=lambda c: True)
def on_callback(cq: CallbackQuery):
    chat_id = cq.message.chat.id
    if not is_admin(chat_id):
        bot.answer_callback_query(cq.id, "⛔ دسترسی ندارید")
        return

    data = cq.data or ""
    
    # بازگشت به منو
    if data == "back_main":
        bot.edit_message_text("📋 پنل مدیریت:", chat_id, cq.message.message_id, reply_markup=kb_main())
        return

    # لغو عملیات
    if data == "cancel_add_account":
        admin_states[chat_id] = {"state": None, "data": {}}
        bot.edit_message_text("❌ عملیات لغو شد", chat_id, cq.message.message_id)
        bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())
        return

    # راهنما
    if data == "help_menu":
        help_text = (
            "📚 **راهنمای ربات تبچی**\n\n"
            "1️⃣ **تنظیم API**\n"
            "   • اول برو تو بخش تنظیمات API\n"
            "   • API ID و Hash رو وارد کن\n\n"
            "2️⃣ **افزودن تبچی**\n"
            "   • فقط شماره موبایل رو بده\n"
            "   • کد تأیید رو با دکمه‌ها وارد کن\n\n"
            "3️⃣ **مدیریت تبچی**\n"
            "   • تنظیم بنر (متن/عکس/ویدیو)\n"
            "   • تنظیم زمان ارسال\n"
            "   • فعال/غیرفعال کردن\n"
            "   • ارسال دستی\n\n"
            "4️⃣ **بنر عمومی**\n"
            "   • یه بنر مشترک برای همه\n\n"
            "⚠️ **نکات مهم**\n"
            "• فقط به گروه‌ها ارسال میشه\n"
            "• برای جلوگیری از بن، با تأخیر ارسال میشه"
        )
        bot.edit_message_text(help_text, chat_id, cq.message.message_id, 
                            reply_markup=kb_main(), parse_mode="Markdown")
        return

    # افزودن تبچی
    if data == "add_account":
        if not manager.api_manager.is_configured():
            bot.answer_callback_query(cq.id, "❌ اول API رو تنظیم کن!", show_alert=True)
            return
        
        admin_states[chat_id] = {"state": "ADD_PHONE", "data": {}}
        bot.edit_message_text("📱 شماره موبایل با کد کشور:\nمثال: +989123456789", 
                            chat_id, cq.message.message_id, reply_markup=kb_cancel())
        return

    # تنظیمات API
    if data == "api_settings":
        configured = manager.api_manager.is_configured()
        api_id, api_hash = manager.api_manager.get_api()
        
        text = "🔧 **تنظیمات API**\n\n"
        if configured:
            text += f"✅ API تنظیم شده\n"
            text += f"🆔 API ID: `{api_id}`\n"
            text += f"🔑 API Hash: `{api_hash[:5]}...`"
        else:
            text += "❌ API تنظیم نشده"
        
        bot.edit_message_text(text, chat_id, cq.message.message_id,
                            reply_markup=kb_api_settings(configured), parse_mode="Markdown")
        return

    # عملیات API
    if data.startswith("api:"):
        action = data.split(":", 1)[1]
        
        if action == "set":
            admin_states[chat_id] = {"state": "SET_API_ID", "data": {}}
            bot.edit_message_text("🆔 API ID رو بفرست (عدد):", 
                                chat_id, cq.message.message_id, reply_markup=kb_cancel())
            
        elif action == "show":
            api_id, api_hash = manager.api_manager.get_api()
            if api_id and api_hash:
                bot.send_message(chat_id, f"🔧 **API اطلاعات**\n\nID: `{api_id}`\nHash: `{api_hash}`", 
                               parse_mode="Markdown")
            else:
                bot.send_message(chat_id, "❌ API تنظیم نشده")
            bot.answer_callback_query(cq.id)
            
        elif action == "clear":
            manager.api_manager.clear_api()
            bot.answer_callback_query(cq.id, "✅ API پاک شد")
            bot.edit_message_text("🔧 **تنظیمات API**\n\n❌ API پاک شد", 
                                chat_id, cq.message.message_id,
                                reply_markup=kb_api_settings(False), parse_mode="Markdown")
        return

    # لیست تبچی‌ها
    if data == "list_accounts":
        names = list(manager.accounts.keys())
        if not names:
            bot.answer_callback_query(cq.id, "📭 هیچ اکانتی وجود نداره")
            bot.edit_message_text("📋 لیست تبچی‌ها خالیه!", chat_id, cq.message.message_id)
            return
        bot.edit_message_text("👥 لیست تبچی‌ها:", chat_id, cq.message.message_id,
                            reply_markup=kb_account_list(names))
        return

    # منوی عمومی
    if data == "global_menu":
        gs = manager.global_settings
        status = "✅ فعال" if gs.get("active") else "❌ غیرفعال"
        text = f"🌍 **بنر عمومی**\n\nوضعیت: {status}\nتایم: {gs.get('interval')} دقیقه"
        bot.edit_message_text(text, chat_id, cq.message.message_id,
                            reply_markup=kb_global_menu(gs.get("active"), gs.get("interval")),
                            parse_mode="Markdown")
        return

    # عملیات عمومی
    if data.startswith("global:"):
        action = data.split(":", 1)[1]
        
        if action == "set_banner":
            admin_states[chat_id] = {"state": "SET_GLOBAL_BANNER", "data": {}}
            bot.answer_callback_query(cq.id, "بنر عمومی رو بفرست")
            bot.send_message(chat_id, "🧷 بنر عمومی رو بفرست (متن/عکس/ویدیو):")
            
        elif action == "show_banner":
            b = manager.global_settings.get("banner", {})
            btype = b.get("type", "text")
            
            if btype == "text":
                content = b.get("content", "")
                if content:
                    bot.send_message(chat_id, f"🧷 **بنر عمومی (متن)**\n\n{content}", parse_mode="Markdown")
                else:
                    bot.send_message(chat_id, "❌ بنری تنظیم نشده")
                    
            elif btype in ("photo", "video"):
                fp = b.get("file_path")
                caption = b.get("caption", "")
                if fp and os.path.exists(fp):
                    with open(fp, "rb") as f:
                        if btype == "photo":
                            bot.send_photo(chat_id, f, caption=caption)
                        else:
                            bot.send_video(chat_id, f, caption=caption)
                else:
                    bot.send_message(chat_id, "❌ فایل یافت نشد")
            bot.answer_callback_query(cq.id)
            
        elif action == "set_interval":
            admin_states[chat_id] = {"state": "SET_INTERVAL_GLOBAL", "data": {}}
            bot.answer_callback_query(cq.id, "عدد دقیقه رو بفرست")
            bot.send_message(chat_id, "⏱ عدد دقیقه برای ارسال عمومی:")
            
        elif action == "toggle":
            future = asyncio.run_coroutine_threadsafe(manager.global_toggle(), loop)
            try:
                active = future.result(timeout=10)
                status = "✅ فعال" if active else "❌ غیرفعال"
                bot.answer_callback_query(cq.id, f"وضعیت: {status}")
            except:
                active = manager.global_settings.get("active")
            
            text = f"🌍 **بنر عمومی**\n\nوضعیت: {'✅ فعال' if active else '❌ غیرفعال'}\nتایم: {manager.global_settings.get('interval')} دقیقه"
            bot.edit_message_text(text, chat_id, cq.message.message_id,
                                reply_markup=kb_global_menu(active, manager.global_settings.get("interval")),
                                parse_mode="Markdown")
        return

    # پنل اکانت
    if data.startswith("acc_panel:"):
        name = data.split(":", 1)[1]
        acc = manager.accounts.get(name)
        if not acc:
            bot.answer_callback_query(cq.id, "❌ اکانت یافت نشد")
            return
        
        settings = acc.settings
        status = "✅ فعال" if settings.get("active") else "❌ غیرفعال"
        btype = settings.get("banner", {}).get("type", "text")
        
        text = f"👤 **مدیریت {name}**\n\n"
        text += f"وضعیت: {status}\n"
        text += f"تایم: {settings.get('interval')} دقیقه\n"
        text += f"نوع بنر: {btype}"
        
        bot.edit_message_text(text, chat_id, cq.message.message_id,
                            reply_markup=kb_account_panel(name, settings.get("active"), 
                                                         settings.get("interval"), btype),
                            parse_mode="Markdown")
        return

    # عملیات اکانت
    if data.startswith("acc:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            return
        _, name, action = parts
        
        acc = manager.accounts.get(name)
        if not acc:
            bot.answer_callback_query(cq.id, "❌ اکانت یافت نشد")
            return
        
        if action == "set_banner":
            admin_states[chat_id] = {"state": f"SET_BANNER_ACC:{name}", "data": {}}
            bot.answer_callback_query(cq.id, "بنر رو بفرست")
            bot.send_message(chat_id, f"📝 بنر {name} رو بفرست (متن/عکس/ویدیو):")
            
        elif action == "show_banner":
            b = acc.settings.get("banner", {})
            btype = b.get("type", "text")
            
            if btype == "text":
                content = b.get("content", "")
                if content:
                    bot.send_message(chat_id, f"📄 **بنر {name}**\n\n{content}", parse_mode="Markdown")
                else:
                    bot.send_message(chat_id, "❌ بنری تنظیم نشده")
                    
            elif btype in ("photo", "video"):
                fp = b.get("file_path")
                caption = b.get("caption", "")
                if fp and os.path.exists(fp):
                    with open(fp, "rb") as f:
                        if btype == "photo":
                            bot.send_photo(chat_id, f, caption=caption)
                        else:
                            bot.send_video(chat_id, f, caption=caption)
                else:
                    bot.send_message(chat_id, "❌ فایل یافت نشد")
            bot.answer_callback_query(cq.id)
            
        elif action == "set_interval":
            admin_states[chat_id] = {"state": f"SET_INTERVAL_ACC:{name}", "data": {}}
            bot.answer_callback_query(cq.id, "عدد دقیقه رو بفرست")
            bot.send_message(chat_id, f"⏱ عدد دقیقه برای {name}:")
            
        elif action == "toggle_active":
            future = asyncio.run_coroutine_threadsafe(manager.toggle_active(name), loop)
            try:
                active = future.result(timeout=10)
                status = "✅ فعال" if active else "❌ غیرفعال"
                bot.send_message(chat_id, f"🔘 وضعیت {name}: {status}")
            except:
                bot.send_message(chat_id, f"❌ خطا در تغییر وضعیت")
                
        elif action == "manual_send":
            future = asyncio.run_coroutine_threadsafe(manager.manual_send(name), loop)
            try:
                if future.result(timeout=30):
                    bot.send_message(chat_id, f"🚀 بنر برای {name} ارسال شد")
                else:
                    bot.send_message(chat_id, f"❌ خطا در ارسال")
            except:
                bot.send_message(chat_id, f"❌ خطا در ارسال")
                
        elif action == "reset":
            future = asyncio.run_coroutine_threadsafe(manager.reset_account(name), loop)
            try:
                if future.result(timeout=20):
                    bot.send_message(chat_id, f"🗑 اکانت {name} حذف شد")
                    # برگشت به لیست
                    names = list(manager.accounts.keys())
                    if names:
                        bot.send_message(chat_id, "👥 لیست تبچی‌ها:", reply_markup=kb_account_list(names))
                    else:
                        bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())
                else:
                    bot.send_message(chat_id, f"❌ خطا در حذف")
            except:
                bot.send_message(chat_id, f"❌ خطا در حذف")
        return

    # صفحه کلید عددی
    if data.startswith("code:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        
        name = parts[1]
        action = parts[2]
        
        if chat_id not in admin_states or name != admin_states[chat_id]["data"].get("name"):
            bot.answer_callback_query(cq.id, "❌ اطلاعات یافت نشد")
            return
        
        current_code = admin_states[chat_id]["data"].get("code", "")
        
        if action == "digit":
            digit = parts[3]
            if len(current_code) < 6:  # محدودیت طول کد
                current_code += digit
                admin_states[chat_id]["data"]["code"] = current_code
                
        elif action == "backspace":
            if current_code:
                current_code = current_code[:-1]
                admin_states[chat_id]["data"]["code"] = current_code
                
        elif action == "clear":
            current_code = ""
            admin_states[chat_id]["data"]["code"] = current_code
            
        elif action == "submit":
            if not current_code:
                bot.answer_callback_query(cq.id, "❌ کد رو وارد کن")
                return
            
            bot.edit_message_text("⏳ در حال بررسی کد...", chat_id, cq.message.message_id)
            
            phone = admin_states[chat_id]["data"]["phone"]
            
            future = asyncio.run_coroutine_threadsafe(
                manager.finish_sign_in(name, phone, current_code), loop
            )
            
            try:
                res = future.result(timeout=30)
                if res == "OK":
                    bot.send_message(chat_id, f"✅ اکانت {name} اضافه شد!")
                    admin_states[chat_id] = {"state": None, "data": {}}
                    bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())
                    
                elif res == "PASSWORD_REQUIRED":
                    admin_states[chat_id]["state"] = f"TWOFA:{name}"
                    bot.send_message(chat_id, "🔒 رمز دومرحله‌ای رو وارد کن:")
                    
                else:
                    bot.send_message(chat_id, f"❌ خطا: {res}")
                    admin_states[chat_id] = {"state": None, "data": {}}
                    bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())
                    
            except Exception as e:
                bot.send_message(chat_id, f"❌ خطا: {e}")
                admin_states[chat_id] = {"state": None, "data": {}}
                bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())
            return
        
        # به‌روزرسانی نمایش
        bot.edit_message_text(
            f"🔐 کد رو وارد کن:\nاکانت: {name}\nکد: {current_code}",
            chat_id,
            cq.message.message_id,
            reply_markup=kb_code_pad(name, current_code)
        )
        return

    # منوی ادمین
    if data == "admins_menu":
        admins = load_json(ADMINS_FILE, {"admins": []}).get("admins", [])
        text = "👥 **لیست ادمین‌ها:**\n\n"
        for aid in admins:
            text += f"• `{aid}`\n"
        bot.edit_message_text(text, chat_id, cq.message.message_id,
                            reply_markup=kb_admins_menu(), parse_mode="Markdown")
        return

    if data.startswith("admins:"):
        action = data.split(":", 1)[1]
        
        if action == "add":
            msg = bot.send_message(chat_id, "➕ آیدی عددی ادمین جدید رو بفرست:")
            bot.register_next_step_handler(msg, process_add_admin)
            
        elif action == "remove":
            msg = bot.send_message(chat_id, "➖ آیدی عددی ادمین مورد نظر رو بفرست:")
            bot.register_next_step_handler(msg, process_remove_admin)
        
        bot.answer_callback_query(cq.id)
        return

def process_add_admin(msg):
    chat_id = msg.chat.id
    try:
        new_admin = int(msg.text.strip())
        admins = load_json(ADMINS_FILE, {"admins": []})
        
        if new_admin in admins["admins"]:
            bot.send_message(chat_id, "❌ این کاربر قبلاً ادمینه")
        else:
            admins["admins"].append(new_admin)
            save_json(ADMINS_FILE, admins)
            bot.send_message(chat_id, f"✅ ادمین `{new_admin}` اضافه شد", parse_mode="Markdown")
    except:
        bot.send_message(chat_id, "❌ آیدی نامعتبر")
    
    bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())

def process_remove_admin(msg):
    chat_id = msg.chat.id
    try:
        rem_admin = int(msg.text.strip())
        admins = load_json(ADMINS_FILE, {"admins": []})
        
        if rem_admin == INITIAL_ADMIN_ID:
            bot.send_message(chat_id, "❌ نمی‌تونی ادمین اصلی رو حذف کنی")
        elif rem_admin in admins["admins"]:
            admins["admins"].remove(rem_admin)
            save_json(ADMINS_FILE, admins)
            bot.send_message(chat_id, f"✅ ادمین `{rem_admin}` حذف شد", parse_mode="Markdown")
        else:
            bot.send_message(chat_id, "❌ این کاربر ادمین نیست")
    except:
        bot.send_message(chat_id, "❌ آیدی نامعتبر")
    
    bot.send_message(chat_id, "📋 پنل مدیریت:", reply_markup=kb_main())

# ==================== اجرای بات ====================
def run_bot():
    """اجرای بات در یک نخ جدا"""
    bot.remove_webhook()
    retry_count = 0
    max_retries = 10
    
    while retry_count < max_retries:
        try:
            logger.info(f"شروع بات (تلاش {retry_count + 1})...")
            bot.polling(none_stop=True, timeout=30, long_polling_timeout=30, interval=1)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"خطای اتصال: {e}")
            retry_count += 1
            wait_time = min(2 ** retry_count, 60)
            logger.info(f"تلاش مجدد بعد از {wait_time} ثانیه...")
            time.sleep(wait_time)
        except Exception as e:
            logger.error(f"خطای غیرمنتظره: {e}")
            retry_count += 1
            time.sleep(10)
    
    logger.error("بات متوقف شد (بیش از حد تلاش)")

if __name__ == "__main__":
    print("=" * 50)
    print("🚀 ربات تبچی در حال اجرا...")
    print("=" * 50)
    
    # ایجاد event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # ساخت manager
    manager = TabchiManager(loop)
    
    # بارگذاری اکانت‌ها
    try:
        loop.run_until_complete(manager.load_existing())
        logger.info("اکانت‌های موجود بارگذاری شدند")
    except Exception as e:
        logger.error(f"خطا در بارگذاری اکانت‌ها: {e}")
    
    # اجرای بات در نخ جدا
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # اجرای event loop
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("خاموش شدن...")
    finally:
        # خاموش کردن تمیز
        loop.run_until_complete(manager.shutdown())
        loop.close()
        logger.info("ربات خاموش شد")
        print("👋 خدا نگهدار!")