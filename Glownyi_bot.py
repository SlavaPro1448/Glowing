
from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError, AuthKeyDuplicatedError
import os
import json
from datetime import datetime, timezone
import threading
import time
import hashlib
import fcntl
import atexit

app = Flask(__name__)
CORS(app)  # Разрешаем CORS для всех доменов

# Ваши API credentials
api_id = 24914656
api_hash = '126107e0e53e49d94b3d3512d0715198'

OPERATORS_FILE = 'operators.json'
lock = threading.Lock()

# ГЛОБАЛЬНЫЙ ПУЛ КЛИЕНТОВ - КЛЮЧЕВОЕ ИЗМЕНЕНИЕ!
client_pool = {}
client_lock = threading.Lock()

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
    КЛЮЧЕВАЯ ФУНКЦИЯ: Получает существующий клиент или создает новый
    Клиент создается ОДИН РАЗ и переиспользуется!
    """
    client_key = f"{operator_id}_{phone_number}"
    
    with client_lock:
        # Если клиент уже существует и подключен, возвращаем его
        if client_key in client_pool:
            client = client_pool[client_key]
            if client.is_connected():
                print(f"♻️ ПЕРЕИСПОЛЬЗУЕМ СУЩЕСТВУЮЩИЙ КЛИЕНТ для {phone_number}")
                return client
            else:
                print(f"🔄 ПЕРЕПОДКЛЮЧАЕМ КЛИЕНТ для {phone_number}")
                try:
                    await client.connect()
                    if await client.is_user_authorized():
                        return client
                    else:
                        # Удаляем неавторизованный клиент
                        del client_pool[client_key]
                except Exception as e:
                    print(f"❌ Ошибка переподключения клиента: {e}")
                    if client_key in client_pool:
                        del client_pool[client_key]
        
        # Создаем новый клиент
        print(f"🆕 СОЗДАЕМ НОВЫЙ ДОЛГОЖИВУЩИЙ КЛИЕНТ для {phone_number}")
        os.makedirs("sessions", exist_ok=True)
        session_name = get_session_name(operator_id, phone_number)
        session_path = f"sessions/{session_name}"
        
        client = TelegramClient(session_path, api_id, api_hash)
        
        try:
            await client.connect()
            
            if not await client.is_user_authorized():
                await client.disconnect()
                raise Exception('Клиент не авторизован - требуется повторная авторизация')
            
            # Сохраняем клиент в пул для переиспользования
            client_pool[client_key] = client
            print(f"✅ КЛИЕНТ СОЗДАН И СОХРАНЕН В ПУЛ для {phone_number}")
            
            return client
            
        except Exception as e:
            print(f"❌ Ошибка создания клиента: {e}")
            try:
                await client.disconnect()
            except:
                pass
            raise e

def close_all_clients():
    """Закрываем все клиенты при завершении приложения"""
    print("🔄 ЗАКРЫВАЕМ ВСЕ КЛИЕНТЫ...")
    with client_lock:
        for client_key, client in list(client_pool.items()):
            try:
                if client.is_connected():
                    asyncio.run(client.disconnect())
                    print(f"✅ Клиент {client_key} закрыт")
            except Exception as e:
                print(f"❌ Ошибка закрытия клиента {client_key}: {e}")
        client_pool.clear()

# Регистрируем закрытие клиентов при завершении приложения
atexit.register(close_all_clients)

# Словарь для хранения phone_code_hash
phone_code_hashes = {}

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'message': 'Telegram API service is running'})

@app.route('/api/operators', methods=['GET'])
def get_operators():
    operators = load_operators_safe()
    return jsonify({'operators': operators})

@app.route('/api/operators', methods=['POST'])
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
def get_chats(operator):
    try:
        phone = request.args.get('phone')
        if not phone:
            return jsonify({'success': False, 'error': 'Phone number is required'})
        
        print(f"🔥 БЫСТРАЯ ЗАГРУЗКА ЧАТОВ для {operator} с телефоном {phone}")
        
        async def get_chats_async():
            # ИСПОЛЬЗУЕМ ПЕРЕИСПОЛЬЗУЕМЫЙ КЛИЕНТ!
            client = await get_or_create_client(operator, phone)
            
            print("🚀 БЫСТРАЯ ЗАГРУЗКА ДИАЛОГОВ БЕЗ ПЕРЕПОДКЛЮЧЕНИЙ...")
            
            all_dialogs = []
            dialog_count = 0
            
            # Загружаем все диалоги БЕЗ лимитов (как в твоем коде с ChatGPT)
            async for dialog in client.iter_dialogs():
                dialog_count += 1
                all_dialogs.append(dialog)
                
                # Минимальная задержка только каждые 50 диалогов
                if dialog_count % 50 == 0:
                    await asyncio.sleep(0.05)  # Очень маленькая задержка
            
            print(f"✅ БЫСТРО ЗАГРУЖЕНО {len(all_dialogs)} диалогов")
            
            chats = []
            for dialog in all_dialogs:
                try:
                    if (hasattr(dialog.entity, 'bot') and dialog.entity.bot) or \
                       dialog.entity.__class__.__name__ == 'UserEmpty':
                        continue
                    
                    last_message = ''
                    if dialog.message:
                        if hasattr(dialog.message, 'message') and dialog.message.message:
                            last_message = dialog.message.message
                        elif hasattr(dialog.message, 'media'):
                            last_message = 'Медиа файл'
                        else:
                            last_message = 'Сообщение'
                    
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
                        'timestamp': dialog.message.date.strftime('%H:%M') if dialog.message and hasattr(dialog.message, 'date') else '',
                        'unreadCount': unread_count,
                        'type': 'group' if hasattr(dialog.entity, 'megagroup') or hasattr(dialog.entity, 'broadcast') else 'private'
                    }
                    chats.append(chat_info)
                    
                except Exception as e:
                    print(f"⚠️ Ошибка обработки диалога {dialog.id}: {e}")
                    continue
            
            # НЕ ЗАКРЫВАЕМ КЛИЕНТ! Он остается в пуле для переиспользования
            print(f"🎯 БЫСТРО ЗАГРУЖЕНО {len(chats)} ЧАТОВ БЕЗ ПЕРЕПОДКЛЮЧЕНИЙ")
            return {'success': True, 'chats': chats}
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(get_chats_async())
        return jsonify(result)
        
    except Exception as e:
        print(f"💥 ОШИБКА: {str(e)}")
        return jsonify({
            'success': False, 
            'error': f'Ошибка загрузки чатов: {str(e)}'
        }), 500

@app.route('/api/messages/<operator>/<chat_id>', methods=['GET'])
def get_messages(operator, chat_id):
    try:
        chat_id = int(chat_id)
        phone = request.args.get('phone')
        if not phone:
            return jsonify({'success': False, 'error': 'Phone number is required'})
        
        print(f"🔥 БЫСТРАЯ ЗАГРУЗКА СООБЩЕНИЙ для чата {chat_id}")
        
        async def get_messages_async():
            # ИСПОЛЬЗУЕМ ПЕРЕИСПОЛЬЗУЕМЫЙ КЛИЕНТ!
            client = await get_or_create_client(operator, phone)
            
            print("🚀 БЫСТРАЯ ЗАГРУЗКА СООБЩЕНИЙ БЕЗ ПЕРЕПОДКЛЮЧЕНИЙ...")
            
            messages = []
            message_count = 0
            
            # Загружаем ВСЕ сообщения БЕЗ лимитов (как в твоем коде с ChatGPT)  
            async for msg in client.iter_messages(chat_id, reverse=True):
                try:
                    message_count += 1
                    
                    # Минимальная задержка только каждые 100 сообщений
                    if message_count % 100 == 0:
                        await asyncio.sleep(0.02)  # Очень маленькая задержка
                    
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
            
            # НЕ ЗАКРЫВАЕМ КЛИЕНТ! Он остается в пуле для переиспользования
            print(f"🎯 БЫСТРО ЗАГРУЖЕНО {len(messages)} СООБЩЕНИЙ БЕЗ ПЕРЕПОДКЛЮЧЕНИЙ")
            return {'success': True, 'messages': messages, 'chatTitle': f'Чат {chat_id}'}
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(get_messages_async())
        return jsonify(result)
        
    except Exception as e:
        print(f"💥 ОШИБКА: {str(e)}")
        return jsonify({
            'success': False, 
            'error': f'Ошибка загрузки сообщений: {str(e)}'
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting Flask app on port {port}")
    print(f"♻️ ДОЛГОЖИВУЩИЕ КЛИЕНТЫ: Аккаунты больше НЕ БУДУТ замораживаться!")
    print(f"📋 Available routes:")
    for rule in app.url_map.iter_rules():
        print(f"  {rule.methods} {rule.rule}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
