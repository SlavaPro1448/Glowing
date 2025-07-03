
from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
import os
import json
import sqlite3
from datetime import datetime, timezone
import threading
import time

app = Flask(__name__)
CORS(app)  # Разрешаем CORS для всех доменов

# Ваши API credentials
api_id = 24914656
api_hash = '126107e0e53e49d94b3d3512d0715198'

OPERATORS_FILE = 'operators.json'
lock = threading.Lock()

def load_operators_safe():
    """Безопасная загрузка операторов с retry логикой"""
    max_retries = 5
    retry_delay = 0.1
    
    for attempt in range(max_retries):
        try:
            with lock:
                if os.path.exists(OPERATORS_FILE):
                    with open(OPERATORS_FILE, 'r') as f:
                        return json.load(f)
                return []
        except (json.JSONDecodeError, IOError) as e:
            print(f"Attempt {attempt + 1} failed to load operators: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                print("Failed to load operators after all retries")
                return []

def save_operators_safe(operators):
    """Безопасное сохранение операторов с retry логикой"""
    max_retries = 5
    retry_delay = 0.1
    
    for attempt in range(max_retries):
        try:
            with lock:
                # Сначала записываем во временный файл
                temp_file = OPERATORS_FILE + '.tmp'
                with open(temp_file, 'w') as f:
                    json.dump(operators, f)
                
                # Атомарно заменяем оригинальный файл
                if os.path.exists(OPERATORS_FILE):
                    os.remove(OPERATORS_FILE)
                os.rename(temp_file, OPERATORS_FILE)
                return True
        except IOError as e:
            print(f"Attempt {attempt + 1} failed to save operators: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                print("Failed to save operators after all retries")
                return False
    return False

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
    operators = [op for op in operators if op != operator]
    if save_operators_safe(operators):
        return jsonify({'success': True, 'operators': operators})
    else:
        return jsonify({'success': False, 'error': 'Failed to delete operator'})

def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

@app.route('/api/auth/send-code', methods=['POST'])
def send_code():
    try:
        data = request.get_json()
        phone = data.get('phone')
        operator = data.get('operator')
        
        print(f"Received request to send code to {phone} for operator {operator}")
        
        async def send_code_async():
            os.makedirs("sessions", exist_ok=True)
            client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            await client.disconnect()
            return sent
        
        sent = run_async(send_code_async())
        
        return jsonify({
            'success': True,
            'phone_code_hash': sent.phone_code_hash,
            'message': 'Код отправлен в Telegram'
        })
    except Exception as e:
        print(f"Error in send_code: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/auth/verify', methods=['POST'])
def verify_code():
    try:
        data = request.get_json()
        phone = data.get('phone')
        code = data.get('code')
        phone_code_hash = data.get('phone_code_hash')
        operator = data.get('operator')
        
        async def verify_code_async():
            os.makedirs("sessions", exist_ok=True)
            client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
            await client.connect()
            
            try:
                await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                await client.disconnect()
                return {'success': True, 'needs_password': False}
            except SessionPasswordNeededError:
                await client.disconnect()
                return {'success': True, 'needs_password': True}
        
        result = run_async(verify_code_async())
        return jsonify(result)
            
    except Exception as e:
        print(f"Error in verify_code: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/auth/password', methods=['POST'])
def verify_password():
    try:
        data = request.get_json()
        password = data.get('password')
        operator = data.get('operator')
        
        async def verify_password_async():
            os.makedirs("sessions", exist_ok=True)
            client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
            await client.connect()
            await client.sign_in(password=password)
            await client.disconnect()
        
        run_async(verify_password_async())
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error in verify_password: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/chats/<operator>', methods=['GET'])
def get_chats(operator):
    try:
        async def get_chats_async():
            os.makedirs("sessions", exist_ok=True)
            client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
            await client.connect()
            
            if not await client.is_user_authorized():
                return {'success': False, 'error': 'Not authorized'}
            
            dialogs = await client.get_dialogs()
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            
            chats = []
            for dialog in dialogs:
                if not getattr(dialog.entity, 'bot', False) and dialog.entity.__class__.__name__ != 'UserEmpty':
                    # Получаем последнее сообщение
                    last_message = dialog.message.message if dialog.message and dialog.message.message else 'Нет сообщений'
                    
                    # Подсчитываем непрочитанные
                    unread_count = 0
                    today_messages = 0
                    
                    async for msg in client.iter_messages(dialog.id, limit=100):
                        if msg.date >= today:
                            today_messages += 1
                            if not msg.out and not getattr(msg, 'read', True):
                                unread_count += 1
                    
                    # Безопасно получаем имя
                    name = ""
                    if hasattr(dialog.entity, 'first_name') and dialog.entity.first_name:
                        name += dialog.entity.first_name
                    if hasattr(dialog.entity, 'last_name') and dialog.entity.last_name:
                        if name:
                            name += " "
                        name += dialog.entity.last_name
                    if not name and hasattr(dialog.entity, 'title') and dialog.entity.title:
                        name = dialog.entity.title
                    if not name:
                        name = f"User {dialog.id}"
                    
                    chat_info = {
                        'id': str(dialog.id),
                        'name': name,
                        'lastMessage': last_message[:100] + '...' if len(last_message) > 100 else last_message,
                        'timestamp': dialog.message.date.strftime('%H:%M') if dialog.message else '',
                        'unreadCount': unread_count,
                        'todayMessages': today_messages,
                        'totalMessages': dialog.message.id if dialog.message else 0,
                        'type': 'group' if hasattr(dialog.entity, 'megagroup') else 'private'
                    }
                    chats.append(chat_info)
            
            await client.disconnect()
            return {'success': True, 'chats': chats}
        
        result = run_async(get_chats_async())
        return jsonify(result)
        
    except Exception as e:
        print(f"Error in get_chats: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/messages/<operator>/<chat_id>', methods=['GET'])
def get_messages(operator, chat_id):
    try:
        chat_id = int(chat_id)
        
        async def get_messages_async():
            os.makedirs("sessions", exist_ok=True)
            client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
            await client.connect()
            
            messages = []
            async for msg in client.iter_messages(chat_id, limit=50, reverse=True):
                message_text = msg.message if msg.message else ''
                
                message_data = {
                    'id': str(msg.id),
                    'text': message_text,
                    'timestamp': msg.date.strftime('%H:%M'),
                    'isIncoming': not msg.out,
                    'isRead': getattr(msg, 'read', True),
                    'type': 'voice' if msg.voice else 'text',
                    'sender': 'Вы' if msg.out else 'Собеседник'
                }
                
                if msg.voice:
                    message_data['voiceDuration'] = f"0:{msg.voice.duration // 60:02d}"
                    message_data['voiceUrl'] = f"voice_{msg.id}.ogg"
                
                messages.append(message_data)
            
            await client.disconnect()
            return {'success': True, 'messages': messages}
        
        result = run_async(get_messages_async())
        return jsonify(result)
        
    except Exception as e:
        print(f"Error in get_messages: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    # Railway автоматически предоставляет переменную PORT
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting Flask app on port {port}")
    print(f"Available routes:")
    for rule in app.url_map.iter_rules():
        print(f"  {rule.methods} {rule.rule}")
    
    # Важно: bind на 0.0.0.0 для Railway
    app.run(host='0.0.0.0', port=port, debug=False)
