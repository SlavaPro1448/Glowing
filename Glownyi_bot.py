from flask import Flask, render_template, request, redirect, session, url_for, jsonify
import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Message
import os
import json
import sqlite3

api_id = 24914656
api_hash = '126107e0e53e49d94b3d3512d0715198'

OPERATORS_FILE = 'operators.json'

def load_operators():
    if os.path.exists(OPERATORS_FILE):
        with open(OPERATORS_FILE, 'r') as f:
            return json.load(f)
    return []

def save_operators(operators):
    with open(OPERATORS_FILE, 'w') as f:
        json.dump(operators, f)

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'

@app.route('/', methods=['GET', 'POST'])
def list_operators():
    operators = load_operators()
    if request.method == 'POST':
        operator = request.form.get('operator')
        if operator:
            return redirect(url_for('login', operator=operator))
    return {'operators': operators}

@app.route('/add_operator', methods=['POST'])
def add_operator():
    operator = request.form.get('operator')
    operators = load_operators()
    if operator and operator not in operators:
        operators.append(operator)
        save_operators(operators)
    return redirect('/')

@app.route('/delete_operator/<operator>')
def delete_operator(operator):
    operators = load_operators()
    operators = [op for op in operators if op != operator]
    save_operators(operators)
    return redirect('/')

@app.route('/login/<operator>', methods=['GET', 'POST'])
def login(operator):
    if request.method == 'POST':
        phone = request.form.get('phone')
        if phone:
            session['phone'] = phone
            session['operator'] = operator
            session.pop('phone_code_hash', None)
            session.pop('password_required', None)
            return redirect(url_for('verify'))
    return f"Login page for operator: {operator}"

@app.route('/verify', methods=['GET', 'POST'])
async def verify():
    from telethon.errors import SessionPasswordNeededError
    phone = session.get('phone')
    operator = session.get('operator')
    phone_code_hash = session.get('phone_code_hash')
    error_message = None
    password_required = session.get('password_required', False)

    if not phone or not operator:
        return redirect('/')

    if request.method == 'POST':
        if request.form.get('resend'):
            try:
                os.makedirs("sessions", exist_ok=True)
                client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
                await client.connect()
                sent = await client.send_code_request(phone)
                session['phone_code_hash'] = sent.phone_code_hash
                await client.disconnect()
                error_message = "Код отправлен повторно."
            except Exception as e:
                error_message = f"Ошибка при повторной отправке кода: {str(e)}"
            return {'status': 'verification', 'operator': operator, 'phone': phone, 'error': error_message, 'password_required': password_required}
        code = request.form.get('code')
        # Save the code in session for later use (for password step)
        if not password_required:
            session['saved_code'] = code
        password = request.form.get('password')
        try:
            os.makedirs("sessions", exist_ok=True)
            client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
            await client.connect()

            if password_required:
                password = request.form.get('password')
                try:
                    await client.sign_in(password=password)
                except Exception:
                    # Если это первый вход с паролем (сессия еще не авторизована)
                    await client.sign_in(phone=phone, code=session.get('saved_code'), phone_code_hash=phone_code_hash)
                    await client.sign_in(password=password)

                session.pop('phone_code_hash', None)
                session.pop('password_required', None)
                session.pop('saved_code', None)
                await client.disconnect()
                return redirect(f'/operator/{operator}')
            else:
                if not phone_code_hash:
                    error_message = "Код подтверждения устарел. Попробуйте снова."
                else:
                    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                    session.pop('phone_code_hash', None)
                    await client.disconnect()
                    return redirect(f'/operator/{operator}')

        except SessionPasswordNeededError:
            session['password_required'] = True
            return {'status': 'verification', 'operator': operator, 'phone': phone, 'error': error_message, 'password_required': True}

        except Exception as e:
            error_message = f"Ошибка при входе: {str(e)}"

    elif not password_required and not session.get('phone_code_hash'):
        # Только если ещё не начат вход
        try:
            os.makedirs("sessions", exist_ok=True)
            client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
            print(">> Попытка отправки кода в Telegram")
            await client.connect()
            sent = await client.send_code_request(phone)
            print(f">> Код отправлен. phone_code_hash: {sent.phone_code_hash}")
            session['phone_code_hash'] = sent.phone_code_hash
            await client.disconnect()
        except Exception as e:
            error_message = f"Ошибка при отправке кода: {str(e)}"

    return {'status': 'verification', 'operator': operator, 'phone': phone, 'error': error_message, 'password_required': password_required}

@app.route('/operator/<operator>')
async def operator_chats(operator):
    os.makedirs("sessions", exist_ok=True)
    client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
    await client.disconnect()
    await client.connect()
    if not await client.is_user_authorized():
        return redirect(url_for('verify'))
    try:
        try:
            dialogs = await client.get_dialogs()
            from datetime import datetime, timezone

            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

            unique_senders = set()
            for dialog in dialogs:
                if not getattr(dialog.entity, 'bot', False) and not getattr(dialog.entity, 'verified', False) and dialog.entity.__class__.__name__ != 'UserEmpty':
                    async for msg in client.iter_messages(dialog.id, reverse=True, offset_date=today):
                        if msg.out is False and msg.date >= today:
                            unique_senders.add(dialog.id)
                            break
            incoming_dialogs = len(unique_senders)

            unread_today = 0
            for dialog in dialogs:
                if not getattr(dialog.entity, 'bot', False) and not getattr(dialog.entity, 'verified', False) and dialog.entity.__class__.__name__ != 'UserEmpty':
                    async for msg in client.iter_messages(dialog.id, reverse=True, offset_date=today):
                        if msg.out is False and getattr(msg, 'read', True) is False and msg.date >= today:
                            unread_today += 1

            return {
                'dialogs': [d.id for d in dialogs],
                'incoming_dialogs': incoming_dialogs,
                'unread_today': unread_today,
                'operator': operator
            }

        except sqlite3.OperationalError as e:
            return f"Ошибка доступа к базе данных: {e}", 500
    finally:
        try:
            client.session.save()
            await client.disconnect()
        except sqlite3.OperationalError:
            pass

@app.route('/chat/<operator>/<chat_id>')
async def chat(operator, chat_id):
    import os
    chat_id = int(chat_id)
    os.makedirs("sessions", exist_ok=True)
    client = TelegramClient(f"sessions/{operator}", api_id, api_hash)
    try:
        await client.connect()
        messages = []
        unread_count = 0
        # Check if the message is a voice message and save it to static folder
        async for msg in client.iter_messages(chat_id, reverse=True):
            # Пример в функции, где ты проходишься по messages
            if msg.voice:
                filename = f"voice_{msg.id}.ogg"
                path = os.path.join('static', 'voice', filename)
                if not os.path.exists(path):
                    await client.download_media(msg, file=path)
                msg.voice_path = filename
            else:
                msg.voice_path = None
            messages.append(msg)
        from datetime import datetime, timedelta, timezone

        # Статистика за сегодня
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        received_today = 0
        replied_today = 0

        for msg in messages:
            if msg.date >= today:
                if not msg.out and (not msg.read if hasattr(msg, 'read') else False):
                    unread_count += 1
                received_today += 1
                if msg.reply_to:
                    replied_today += 1

        stats = {
            'received_today': received_today,
            'replied_today': replied_today,
            'unreplied_today': received_today - replied_today,
            'unread_today': unread_count,
        }
        return {
            'messages': [m.id for m in messages],
            'stats': stats
        }
    finally:
        await client.disconnect()

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=False, host='0.0.0.0', port=port)
