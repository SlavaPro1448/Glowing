from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_cors import CORS
from flask_login import LoginManager, login_required, current_user
import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError, AuthKeyDuplicatedError
import os
import json
from datetime import datetime, timezone, date
import threading
import time
import hashlib
import fcntl
import atexit
import traceback

# Импорты для авторизации
from models import db, User
from auth import auth_bp, admin_required, operator_required

app = Flask(__name__)
CORS(app)  # Разрешаем CORS для всех доменов

# Конфигурация Flask
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///telegram_dashboard.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Инициализация расширений
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Для доступа к этой странице необходимо войти в систему.'
login_manager.login_message_category = 'info'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)

# Регистрация Blueprint'ов
app.register_blueprint(auth_bp)

# Ваши API credentials
api_id = 24914656
api_hash = '126107e0e53e49d94b3d3512d0715198'

OPERATORS_FILE = 'operators.json'
lock = threading.Lock()

# Глобальный пул клиентов для мониторинга
clients_pool = {}
clients_lock = threading.Lock()

# Словарь для хранения phone_code_hash
phone_code_hashes = {}

# Глобальный event loop для asyncio
global_loop = None
loop_thread = None

def setup_global_event_loop():
    """Настройка глобального event loop в отдельном потоке"""
    global global_loop, loop_thread
    
    def run_event_loop():
        global global_loop
        global_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(global_loop)
        print("🔄 ГЛОБАЛЬНЫЙ EVENT LOOP ЗАПУЩЕН")
        global_loop.run_forever()
    
    if global_loop is None or global_loop.is_closed():
        loop_thread = threading.Thread(target=run_event_loop, daemon=True)
        loop_thread.start()
        time.sleep(0.5)  # Даем время на запуск
        print("✅ ГЛОБАЛЬНЫЙ EVENT LOOP НАСТРОЕН")

def run_async_in_global_loop(coro):
    """Запускает корутину в глобальном event loop"""
    global global_loop
    if global_loop is None or global_loop.is_closed():
        setup_global_event_loop()
    
    future = asyncio.run_coroutine_threadsafe(coro, global_loop)
    return future.result(timeout=30)  # 30 секунд таймаут

def check_operator_access(operator_name):
    """Проверяет, имеет ли текущий пользователь доступ к оператору"""
    if not current_user.is_authenticated:
        return False
    
    # Админ имеет доступ ко всем операторам
    if current_user.is_admin():
        return True
    
    # Оператор имеет доступ только к своему assigned_operator_name
    return current_user.assigned_operator_name == operator_name

def load_operators_safe():
    """Безопасная загрузка операторов из файла с файловой блокировкой"""
    with lock:
        try:
            if os.path.exists(OPERATORS_FILE):
                with open(OPERATORS_FILE, 'r', encoding='utf-8') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        data = json.load(f)
                        return data if isinstance(data, list) else []
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:
                return []
        except Exception as e:
            print(f"Error loading operators: {e}")
            return []

def save_operators_safe(operators):
    """Безопасное сохранение операторов в файл с файловой блокировкой"""
    with lock:
        try:
            temp_file = OPERATORS_FILE + '.tmp'
            with open(temp_file, 'w', encoding='utf-8') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(operators, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            
            os.rename(temp_file, OPERATORS_FILE)
            return True
        except Exception as e:
            print(f"Error saving operators: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
            return False

def get_session_name(operator_id, phone_number):
    """Создает уникальное имя сессии на основе operator_id и номера телефона"""
    unique_string = f"{operator_id}_{phone_number}"
    hash_object = hashlib.md5(unique_string.encode())
    return f"session_{hash_object.hexdigest()}"

async def get_or_create_client(operator_id, phone_number):
    """
    Получает существующий клиент из пула или создает новый
    """
    client_key = f"{operator_id}_{phone_number}"
    
    with clients_lock:
        if client_key in clients_pool:
            client = clients_pool[client_key]
            if client.is_connected():
                print(f"♻️ ИСПОЛЬЗУЕМ СУЩЕСТВУЮЩИЙ КЛИЕНТ для {phone_number}")
                return client
            else:
                print(f"🔄 ПЕРЕПОДКЛЮЧАЕМ КЛИЕНТА для {phone_number}")
                try:
                    await client.connect()
                    return client
                except Exception as e:
                    print(f"❌ Ошибка переподключения: {e}")
                    # Удаляем неработающий клиент
                    del clients_pool[client_key]
    
    # Создаем новый клиент
    print(f"🆕 СОЗДАЕМ НОВЫЙ КЛИЕНТ для {phone_number}")
    
    os.makedirs("sessions", exist_ok=True)
    session_name = get_session_name(operator_id, phone_number)
    session_path = f"sessions/{session_name}"
    
    client = TelegramClient(
        session_path, 
        api_id, 
        api_hash,
        system_version="4.16.30-vxCUSTOM"
    )
    
    try:
        await client.connect()
        print(f"✅ КЛИЕНТ ПОДКЛЮЧЕН для {phone_number}")
        
        # Добавляем в пул
        with clients_lock:
            clients_pool[client_key] = client
        
        return client
    except Exception as e:
        print(f"❌ Ошибка создания клиента: {e}")
        try:
            await client.disconnect()
        except:
            pass
        raise e

def cleanup_clients():
    """Очистка клиентов при завершении работы"""
    print("🧹 ОЧИСТКА КЛИЕНТОВ...")
    with clients_lock:
        for client_key, client in clients_pool.items():
            try:
                if hasattr(client, 'disconnect'):
                    # Запускаем отключение в глобальном event loop
                    if global_loop and not global_loop.is_closed():
                        future = asyncio.run_coroutine_threadsafe(client.disconnect(), global_loop)
                        future.result(timeout=5)
                    print(f"✅ Клиент {client_key} отключен")
            except Exception as e:
                print(f"⚠️ Ошибка отключения клиента {client_key}: {e}")
        clients_pool.clear()
    
    # Останавливаем глобальный event loop
    if global_loop and not global_loop.is_closed():
        global_loop.call_soon_threadsafe(global_loop.stop)
    
    print("✅ ОЧИСТКА ЗАВЕРШЕНА")

# Регистрируем функцию очистки
atexit.register(cleanup_clients)

@app.route('/')
def index():
    """Главная страница с перенаправлением на соответствующую панель"""
    if current_user.is_authenticated:
        if current_user.is_admin():
            return redirect(url_for('auth.admin_dashboard'))
        else:
            return redirect(url_for('auth.operator_dashboard'))
    return redirect(url_for('auth.login'))

# ============= ЭНДПОИНТЫ АВТОРИЗАЦИИ (защищенные) =============

@app.route('/api/auth/send-code', methods=['POST'])
@login_required
def send_code():
    try:
        print("📥 Получен запрос на отправку кода")
        data = request.get_json()
        print(f"📋 Данные запроса: {json.dumps(data, indent=2)}")
        
        phone = data.get('phone')
        operator = data.get('operator')
        
        if not phone or not operator:
            print("❌ Отсутствуют обязательные параметры")
            return jsonify({'success': False, 'error': 'Phone and operator are required'})
        
        # Проверяем права доступа
        if not check_operator_access(operator):
            return jsonify({'success': False, 'error': 'Доступ к этому оператору запрещен'}), 403
        
        print(f"📞 ОТПРАВКА КОДА для {phone}, оператор: {operator}")
        
        async def send_code_async():
            try:
                print("🔧 Получаем клиент из пула...")
                client = await get_or_create_client(operator, phone)
                
                print(f"🚀 ОТПРАВЛЯЕМ КОД через Telegram API...")
                result = await client.send_code_request(phone)
                phone_code_hash = result.phone_code_hash
                
                print(f"✅ Код отправлен. phone_code_hash: {phone_code_hash[:20]}...")
                
                # Сохраняем phone_code_hash для последующего использования
                phone_code_hashes[f"{operator}_{phone}"] = phone_code_hash
                
                print(f"✅ КОД ОТПРАВЛЕН для {phone}")
                return {
                    'success': True, 
                    'message': 'Код отправлен в Telegram',
                    'phone_code_hash': phone_code_hash
                }
                
            except Exception as e:
                print(f"❌ ОШИБКА ОТПРАВКИ КОДА: {e}")
                print(f"❌ TRACEBACK: {traceback.format_exc()}")
                return {'success': False, 'error': str(e)}
        
        result = run_async_in_global_loop(send_code_async())
        print(f"🎯 Результат отправки кода: {json.dumps(result, indent=2)}")
        return jsonify(result)
        
    except Exception as e:
        print(f"💥 КРИТИЧЕСКАЯ ОШИБКА в send_code: {str(e)}")
        print(f"💥 TRACEBACK: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/verify', methods=['POST'])
@login_required
def verify_code():
    try:
        print("📥 Получен запрос на проверку кода")
        data = request.get_json()
        print(f"📋 Данные запроса проверки: {json.dumps(data, indent=2)}")
        
        phone = data.get('phone')
        code = data.get('code')
        phone_code_hash = data.get('phone_code_hash')
        operator = data.get('operator')
        
        if not all([phone, code, phone_code_hash, operator]):
            print("❌ Отсутствуют обязательные параметры для проверки")
            return jsonify({'success': False, 'error': 'All fields are required'})
        
        print(f"🔐 ПРОВЕРКА КОДА {code} для {phone}")
        
        async def verify_code_async():
            try:
                print("🔧 Получаем клиент из пула...")
                client = await get_or_create_client(operator, phone)
                
                print(f"🚀 ПРОВЕРЯЕМ КОД через Telegram API...")
                print(f"🔐 Параметры: phone={phone}, code={code}, phone_code_hash={phone_code_hash[:20] if phone_code_hash else 'None'}...")
                
                try:
                    user = await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                    print(f"✅ КОД ПРИНЯТ для {phone}")
                    print(f"✅ Пользователь: {user.first_name if hasattr(user, 'first_name') else 'Неизвестно'}")
                    
                    # Получаем данные сессии
                    session_data = client.session.save()
                    print(f"✅ Сессия сохранена, длина: {len(session_data) if session_data else 0}")
                    
                    return {
                        'success': True,
                        'message': 'Успешная авторизация',
                        'session_data': session_data,
                        'needs_password': False
                    }
                    
                except SessionPasswordNeededError:
                    print(f"🛡️ ТРЕБУЕТСЯ 2FA для {phone}")
                    return {
                        'success': True,
                        'message': 'Требуется пароль двухфакторной авторизации',
                        'needs_password': True
                    }
                
            except Exception as e:
                print(f"❌ ОШИБКА ПРОВЕРКИ КОДА: {e}")
                print(f"❌ TRACEBACK: {traceback.format_exc()}")
                return {'success': False, 'error': str(e)}
        
        result = run_async_in_global_loop(verify_code_async())
        print(f"🎯 Результат проверки кода: {json.dumps(result, indent=2)}")
        return jsonify(result)
        
    except Exception as e:
        print(f"💥 КРИТИЧЕСКАЯ ОШИБКА в verify_code: {str(e)}")
        print(f"💥 TRACEBACK: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/password', methods=['POST'])
@login_required
def check_password():
    try:
        print("📥 Получен запрос на проверку пароля 2FA")
        data = request.get_json()
        phone = data.get('phone')
        password = data.get('password')
        operator = data.get('operator')
        
        if not all([phone, password, operator]):
            return jsonify({'success': False, 'error': 'All fields are required'})
        
        print(f"🛡️ ПРОВЕРКА 2FA для {phone}")
        
        async def check_password_async():
            try:
                client = await get_or_create_client(operator, phone)
                
                print(f"🚀 ПРОВЕРЯЕМ ПАРОЛЬ через Telegram API...")
                
                user = await client.sign_in(password=password)
                print(f"✅ 2FA ПРИНЯТ для {phone}")
                
                # Получаем данные сессии
                session_data = client.session.save()
                
                return {
                    'success': True,
                    'message': 'Успешная авторизация',
                    'session_data': session_data
                }
                
            except Exception as e:
                print(f"❌ ОШИБКА ПРОВЕРКИ 2FA: {e}")
                print(f"❌ TRACEBACK: {traceback.format_exc()}")
                return {'success': False, 'error': str(e)}
        
        result = run_async_in_global_loop(check_password_async())
        return jsonify(result)
        
    except Exception as e:
        print(f"💥 КРИТИЧЕСКАЯ ОШИБКА в check_password: {str(e)}")
        print(f"💥 TRACEBACK: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/operators', methods=['GET'])
@login_required
def get_operators():
    """Получение списка операторов (админ - все, оператор - только свой)"""
    if current_user.is_admin():
        operators = load_operators_safe()
        return jsonify({'operators': operators})
    else:
        # Оператор видит только себя
        if current_user.assigned_operator_name:
            return jsonify({'operators': [current_user.assigned_operator_name]})
        else:
            return jsonify({'operators': []})

@app.route('/api/operators', methods=['POST'])
@admin_required
def add_operator():
    data = request.get_json()
    operator = data.get('operator')
    
    operators = load_operators_safe()
    if operator and operator not in operators:
        operators.append(operator)
        if save_operators_safe(operators):
            return jsonify({'success': True, 'operators': operators})
        else:
            return jsonify({'success': False, 'error': 'Failed to save operator'})
    return jsonify({'success': False, 'error': 'Operator already exists or invalid'})

@app.route('/api/operators/<operator>', methods=['DELETE'])
@admin_required
def delete_operator(operator):
    operators = load_operators_safe()
    original_count = len(operators)
    operators = [op for op in operators if op != operator]
    
    if len(operators) < original_count:
        if save_operators_safe(operators):
            return jsonify({'success': True, 'operators': operators})
        else:
            return jsonify({'success': False, 'error': 'Failed to delete operator'})
    else:
        return jsonify({'success': False, 'error': 'Operator not found'})

@app.route('/api/chats/<operator>', methods=['GET'])
@login_required
def get_chats(operator):
    try:
        # Проверяем права доступа
        if not check_operator_access(operator):
            return jsonify({'success': False, 'error': 'Доступ к этому оператору запрещен'}), 403
        
        phone = request.args.get('phone')
        if not phone:
            return jsonify({'success': False, 'error': 'Phone number is required'})
        
        print(f"🔥 ЗАГРУЗКА ЧАТОВ для {operator} с телефоном {phone}")
        
        async def get_chats_async():
            try:
                client = await get_or_create_client(operator, phone)
                
                print("🚀 ЗАГРУЗКА ДИАЛОГОВ...")
                
                all_dialogs = []
                dialog_count = 0
                today = date.today()
                today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
                
                async for dialog in client.iter_dialogs():
                    dialog_count += 1
                    all_dialogs.append(dialog)
                    
                    if dialog_count % 50 == 0:
                        await asyncio.sleep(0.05)
                
                print(f"✅ ЗАГРУЖЕНО {len(all_dialogs)} диалогов")
                
                chats = []
                total_today_incoming = 0
                
                for dialog in all_dialogs:
                    try:
                        if (hasattr(dialog.entity, 'bot') and dialog.entity.bot) or \
                           dialog.entity.__class__.__name__ == 'UserEmpty':
                            continue
                        
                        last_message = ''
                        message_time = ''
                        today_incoming_count = 0
                        
                        if dialog.message:
                            if hasattr(dialog.message, 'message') and dialog.message.message:
                                last_message = dialog.message.message
                            elif hasattr(dialog.message, 'media'):
                                last_message = 'Медиа файл'
                            else:
                                last_message = 'Сообщение'
                            
                            # Правильное время последнего сообщения
                            if hasattr(dialog.message, 'date') and dialog.message.date:
                                msg_date = dialog.message.date
                                if msg_date.date() == today:
                                    message_time = msg_date.strftime('%H:%M')
                                else:
                                    message_time = msg_date.strftime('%d.%m')
                        
                        # Подсчет входящих сообщений за сегодня
                        try:
                            async for msg in client.iter_messages(dialog.id, limit=50):
                                if msg.date and msg.date >= today_start:
                                    if not msg.out:  # Входящее сообщение
                                        today_incoming_count += 1
                                else:
                                    break  # Сообщения старше сегодняшнего дня
                        except Exception as e:
                            print(f"⚠️ Ошибка подсчета сообщений для {dialog.id}: {e}")
                        
                        total_today_incoming += today_incoming_count
                        
                        unread_count = getattr(dialog, 'unread_count', 0)
                        
                        name = ""
                        try:
                            if hasattr(dialog.entity, 'first_name') and dialog.entity.first_name:
                                name += dialog.entity.first_name
                            if hasattr(dialog.entity, 'last_name') and dialog.entity.last_name:
                                if name:
                                    name += " "
                                name += dialog.entity.last_name
                            if not name and hasattr(dialog.entity, 'title') and dialog.entity.title:
                                name = dialog.entity.title
                            if not name:
                                name = f"Чат {dialog.id}"
                        except Exception as e:
                            print(f"⚠️ Ошибка получения имени для {dialog.id}: {e}")
                            name = f"Чат {dialog.id}"
                        
                        chat_info = {
                            'id': str(dialog.id),
                            'name': name,
                            'lastMessage': last_message[:100] + '...' if len(last_message) > 100 else last_message,
                            'timestamp': message_time,
                            'unreadCount': unread_count,
                            'todayIncoming': today_incoming_count,
                            'type': 'group' if hasattr(dialog.entity, 'megagroup') or hasattr(dialog.entity, 'broadcast') else 'private'
                        }
                        chats.append(chat_info)
                        
                    except Exception as e:
                        print(f"⚠️ Ошибка обработки диалога {dialog.id}: {e}")
                        continue
                
                print(f"🎯 ЗАГРУЖЕНО {len(chats)} ЧАТОВ")
                print(f"📊 ВСЕГО ВХОДЯЩИХ ЗА СЕГОДНЯ: {total_today_incoming}")
                
                return {
                    'success': True, 
                    'chats': chats,
                    'todayStats': {
                        'totalIncoming': total_today_incoming,
                        'accountPhone': phone
                    }
                }
                
            except Exception as e:
                print(f"❌ ОШИБКА ЗАГРУЗКИ ЧАТОВ: {e}")
                return {'success': False, 'error': str(e)}
        
        result = run_async_in_global_loop(get_chats_async())
        return jsonify(result)
        
    except Exception as e:
        print(f"💥 ОШИБКА: {str(e)}")
        return jsonify({
            'success': False, 
            'error': f'Ошибка загрузки чатов: {str(e)}'
        }), 500

@app.route('/api/messages/<operator>/<chat_id>', methods=['GET'])
@login_required
def get_messages(operator, chat_id):
    try:
        # Проверяем права доступа
        if not check_operator_access(operator):
            return jsonify({'success': False, 'error': 'Доступ к этому оператору запрещен'}), 403
        
        chat_id = int(chat_id)
        phone = request.args.get('phone')
        if not phone:
            return jsonify({'success': False, 'error': 'Phone number is required'})
        
        print(f"🔥 ЗАГРУЗКА СООБЩЕНИЙ для чата {chat_id}")
        
        async def get_messages_async():
            try:
                client = await get_or_create_client(operator, phone)
                
                print("🚀 ЗАГРУЗКА СООБЩЕНИЙ...")
                
                messages = []
                message_count = 0
                
                async for msg in client.iter_messages(chat_id, reverse=True):
                    try:
                        message_count += 1
                        
                        if message_count % 100 == 0:
                            await asyncio.sleep(0.02)
                        
                        message_text = ''
                        message_type = 'text'
                        voice_data = None
                        
                        if msg.message:
                            message_text = msg.message
                        elif msg.media:
                            if hasattr(msg.media, 'document'):
                                doc = msg.media.document
                                if doc and hasattr(doc, 'mime_type'):
                                    if 'audio/ogg' in doc.mime_type or 'audio/mpeg' in doc.mime_type:
                                        message_type = 'voice'
                                        message_text = 'Голосовое сообщение'
                                        duration = 0
                                        if hasattr(doc, 'attributes'):
                                            for attr in doc.attributes:
                                                if hasattr(attr, 'duration'):
                                                    duration = attr.duration
                                                    break
                                        voice_data = {
                                            'voiceDuration': f"0:{duration//60:02d}:{duration%60:02d}" if duration > 0 else "0:00",
                                            'voiceUrl': f"voice_{msg.id}.ogg"
                                        }
                                    else:
                                        message_text = 'Документ'
                                else:
                                    message_text = 'Файл'
                            elif hasattr(msg.media, 'photo'):
                                message_text = 'Фото'
                            else:
                                message_text = 'Медиа'
                        else:
                            message_text = 'Системное сообщение'
                        
                        message_data = {
                            'id': str(msg.id),
                            'text': message_text,
                            'timestamp': msg.date.strftime('%H:%M') if hasattr(msg, 'date') and msg.date else '',
                            'isIncoming': not msg.out,
                            'isRead': True,
                            'type': message_type,
                            'sender': 'Вы' if msg.out else 'Собеседник'
                        }
                        
                        if voice_data:
                            message_data.update(voice_data)
                        
                        messages.append(message_data)
                        
                    except Exception as e:
                        print(f"⚠️ Ошибка обработки сообщения {msg.id}: {e}")
                        continue
                
                print(f"🎯 ЗАГРУЖЕНО {len(messages)} СООБЩЕНИЙ")
                return {'success': True, 'messages': messages, 'chatTitle': f'Чат {chat_id}'}
                
            except Exception as e:
                print(f"❌ ОШИБКА ЗАГРУЗКИ СООБЩЕНИЙ: {e}")
                return {'success': False, 'error': str(e)}
        
        result = run_async_in_global_loop(get_messages_async())
        return jsonify(result)
        
    except Exception as e:
        print(f"💥 ОШИБКА: {str(e)}")
        return jsonify({
            'success': False, 
            'error': f'Ошибка загрузки сообщений: {str(e)}'
        }), 500

def create_admin_user():
    """Создает администратора по умолчанию"""
    with app.app_context():
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            admin = User(
                username='admin',
                role='admin',
                assigned_operator_name='admin'
            )
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print("✅ СОЗДАН АДМИНИСТРАТОР ПО УМОЛЧАНИЮ: admin/admin123")

if __name__ == '__main__':
    # Настраиваем глобальный event loop перед запуском Flask
    setup_global_event_loop()
    
    # Создаем таблицы и администратора
    with app.app_context():
        db.create_all()
        create_admin_user()
    
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting Flask app on port {port}")
    print(f"✅ ГЛОБАЛЬНЫЙ ПУЛ КЛИЕНТОВ ВОССТАНОВЛЕН!")
    print(f"🔄 ASYNCIO EVENT LOOP ИСПРАВЛЕН!")
    print(f"📡 НЕПРЕРЫВНЫЙ МОНИТОРИНГ ДОСТУПЕН!")
    print(f"🔐 СИСТЕМА АВТОРИЗАЦИИ АКТИВНА!")
    print(f"👤 АДМИН ПО УМОЛЧАНИЮ: admin/admin123")
    print(f"📋 Available routes:")
    for rule in app.url_map.iter_rules():
        print(f"  {rule.methods} {rule.rule}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
