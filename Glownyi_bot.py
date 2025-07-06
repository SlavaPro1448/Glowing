
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, flash, session
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from telethon import TelegramClient
from telethon.sessions import StringSession
import asyncio
import os
import json
import uuid
from datetime import datetime, timedelta

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///users.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app)

# Инициализация базы данных
db = SQLAlchemy(app)

# Модель пользователя
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='operator')  # admin/operator
    assigned_operator_name = db.Column(db.String(100), nullable=True)  # Для привязки к Telegram-сессии
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    is_active = db.Column(db.Boolean, default=True)
    
    def set_password(self, password):
        """Устанавливает хеш пароля"""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Проверяет пароль"""
        return check_password_hash(self.password_hash, password)
    
    def is_admin(self):
        """Проверяет, является ли пользователь админом"""
        return self.role == 'admin'
    
    def is_operator(self):
        """Проверяет, является ли пользователь оператором"""
        return self.role == 'operator'
    
    def get_id(self):
        """Возвращает ID пользователя для Flask-Login"""
        return str(self.id)
    
    def __repr__(self):
        return f'<User {self.username}>'

# Настройка Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите в систему для доступа к этой странице.'
login_manager.login_message_category = 'info'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)

# Декораторы авторизации
def admin_required(f):
    """Декоратор для проверки прав администратора"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash('Доступ запрещен. Требуются права администратора.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def operator_required(f):
    """Декоратор для проверки прав оператора или админа"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Требуется авторизация.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Главная страница - перенаправление на авторизацию
@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.is_admin():
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('operator_dashboard'))
    return redirect(url_for('login'))

# Авторизация
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.is_admin():
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('operator_dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if not username or not password:
            flash('Введите логин и пароль.', 'error')
        else:
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password) and user.is_active:
                login_user(user)
                flash(f'Добро пожаловать, {user.username}!', 'success')
                
                # Перенаправляем в зависимости от роли
                if user.is_admin():
                    return redirect(url_for('admin_dashboard'))
                else:
                    return redirect(url_for('operator_dashboard'))
            else:
                flash('Неверный логин или пароль.', 'error')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Вы вышли из системы.', 'info')
    return redirect(url_for('login'))

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    users = User.query.all()
    return render_template_string(ADMIN_DASHBOARD_TEMPLATE, users=users)

@app.route('/operator/dashboard')
@operator_required
def operator_dashboard():
    return render_template_string(OPERATOR_DASHBOARD_TEMPLATE)

@app.route('/admin/add_user', methods=['GET', 'POST'])
@admin_required
def add_user():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')
        assigned_operator_name = request.form.get('assigned_operator_name')
        
        # Валидация
        if not username or len(username) < 3:
            flash('Логин должен содержать минимум 3 символа.', 'error')
        elif not password or len(password) < 6:
            flash('Пароль должен содержать минимум 6 символов.', 'error')
        elif not role or role not in ['operator', 'admin']:
            flash('Выберите корректную роль.', 'error')
        elif User.query.filter_by(username=username).first():
            flash('Пользователь с таким логином уже существует.', 'error')
        else:
            user = User(
                username=username,
                role=role,
                assigned_operator_name=assigned_operator_name
            )
            user.set_password(password)
            
            try:
                db.session.add(user)
                db.session.commit()
                flash(f'Пользователь {user.username} успешно добавлен.', 'success')
                return redirect(url_for('admin_dashboard'))
            except Exception as e:
                db.session.rollback()
                flash(f'Ошибка при добавлении пользователя: {str(e)}', 'error')
    
    return render_template_string(ADD_USER_TEMPLATE)

@app.route('/admin/delete_user/<user_id>')
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin() and User.query.filter_by(role='admin').count() <= 1:
        flash('Нельзя удалить последнего администратора.', 'error')
        return redirect(url_for('admin_dashboard'))
    
    try:
        db.session.delete(user)
        db.session.commit()
        flash(f'Пользователь {user.username} удален.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при удалении пользователя: {str(e)}', 'error')
    
    return redirect(url_for('admin_dashboard'))

# Получение Telegram учетных данных из переменных окружения
API_ID = os.environ.get('TELEGRAM_API_ID')
API_HASH = os.environ.get('TELEGRAM_API_HASH')

if not API_ID or not API_HASH:
    print("Внимание: TELEGRAM_API_ID и TELEGRAM_API_HASH не установлены")

# Хранилище клиентов Telegram
clients = {}

def get_session_file(operator_name, account_name=None):
    """Получить путь к файлу сессии"""
    sessions_dir = 'sessions'
    if not os.path.exists(sessions_dir):
        os.makedirs(sessions_dir)
    
    if account_name:
        filename = f"{operator_name}_{account_name}.session"
    else:
        filename = f"{operator_name}.session"
    
    return os.path.join(sessions_dir, filename)

async def create_client(operator_name, account_name=None):
    """Создать клиент Telegram"""
    if not API_ID or not API_HASH:
        raise ValueError("TELEGRAM_API_ID и TELEGRAM_API_HASH должны быть установлены")
    
    session_file = get_session_file(operator_name, account_name)
    client_key = f"{operator_name}_{account_name}" if account_name else operator_name
    
    if client_key not in clients:
        client = TelegramClient(session_file, API_ID, API_HASH)
        clients[client_key] = client
    
    return clients[client_key]

# API методы для Telegram
@app.route('/api/send_code', methods=['POST'])
def send_code():
    try:
        data = request.json
        phone = data.get('phone')
        operator = data.get('operator')
        account = data.get('account', 'main')
        
        if not phone or not operator:
            return jsonify({'error': 'Номер телефона и оператор обязательны'}), 400
        
        async def _send_code():
            client = await create_client(operator, account)
            await client.connect()
            
            result = await client.send_code_request(phone)
            phone_code_hash = result.phone_code_hash
            
            return {
                'success': True,
                'phone_code_hash': phone_code_hash,
                'message': f'Код отправлен на номер {phone}'
            }
        
        result = asyncio.run(_send_code())
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/verify_code', methods=['POST'])
def verify_code():
    try:
        data = request.json
        phone = data.get('phone')
        code = data.get('code')
        phone_code_hash = data.get('phone_code_hash')
        operator = data.get('operator')
        account = data.get('account', 'main')
        
        if not all([phone, code, phone_code_hash, operator]):
            return jsonify({'error': 'Все поля обязательны'}), 400
        
        async def _verify_code():
            client = await create_client(operator, account)
            await client.connect()
            
            try:
                await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                return {
                    'success': True,
                    'message': 'Авторизация успешна'
                }
            except Exception as e:
                if 'Two-steps verification is enabled' in str(e):
                    return {
                        'success': False,
                        'two_factor_required': True,
                        'message': 'Требуется двухфакторная аутентификация'
                    }
                else:
                    raise e
        
        result = asyncio.run(_verify_code())
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/verify_password', methods=['POST'])
def verify_password():
    try:
        data = request.json
        password = data.get('password')
        operator = data.get('operator')
        account = data.get('account', 'main')
        
        if not all([password, operator]):
            return jsonify({'error': 'Пароль и оператор обязательны'}), 400
        
        async def _verify_password():
            client = await create_client(operator, account)
            await client.connect()
            
            await client.sign_in(password=password)
            return {
                'success': True,
                'message': 'Двухфакторная аутентификация пройдена'
            }
        
        result = asyncio.run(_verify_password())
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chats/<operator_name>')
def get_chats(operator_name):
    try:
        account = request.args.get('account', 'main')
        
        async def _get_chats():
            client = await create_client(operator_name, account)
            await client.connect()
            
            if not await client.is_user_authorized():
                return {'error': 'Пользователь не авторизован'}
            
            chats = []
            async for dialog in client.iter_dialogs():
                chat_info = {
                    'id': dialog.id,
                    'name': dialog.name,
                    'type': 'channel' if dialog.is_channel else 'group' if dialog.is_group else 'user',
                    'unread_count': dialog.unread_count,
                    'last_message': {
                        'text': dialog.message.text if dialog.message else '',
                        'date': dialog.message.date.isoformat() if dialog.message else None
                    }
                }
                chats.append(chat_info)
            
            return {'chats': chats}
        
        result = asyncio.run(_get_chats())
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat_messages/<operator_name>/<int:chat_id>')
def get_chat_messages(operator_name, chat_id):
    try:
        account = request.args.get('account', 'main')
        limit = int(request.args.get('limit', 50))
        
        async def _get_messages():
            client = await create_client(operator_name, account)
            await client.connect()
            
            if not await client.is_user_authorized():
                return {'error': 'Пользователь не авторизован'}
            
            messages = []
            async for message in client.iter_messages(chat_id, limit=limit):
                msg_info = {
                    'id': message.id,
                    'text': message.text,
                    'date': message.date.isoformat(),
                    'sender_id': message.sender_id,
                    'sender_name': getattr(message.sender, 'first_name', '') if message.sender else '',
                    'is_outgoing': message.out
                }
                messages.append(msg_info)
            
            return {'messages': messages}
        
        result = asyncio.run(_get_messages())
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/send_message', methods=['POST'])
def send_message():
    try:
        data = request.json
        operator = data.get('operator')
        account = data.get('account', 'main')
        chat_id = data.get('chat_id')
        message_text = data.get('message')
        
        if not all([operator, chat_id, message_text]):
            return jsonify({'error': 'Все поля обязательны'}), 400
        
        async def _send_message():
            client = await create_client(operator, account)
            await client.connect()
            
            if not await client.is_user_authorized():
                return {'error': 'Пользователь не авторизован'}
            
            await client.send_message(chat_id, message_text)
            return {
                'success': True,
                'message': 'Сообщение отправлено'
            }
        
        result = asyncio.run(_send_message())
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/operators')
def get_operators():
    try:
        operators = []
        sessions_dir = 'sessions'
        
        if os.path.exists(sessions_dir):
            for filename in os.listdir(sessions_dir):
                if filename.endswith('.session'):
                    operator_name = filename.replace('.session', '')
                    operators.append({
                        'name': operator_name,
                        'session_file': filename
                    })
        
        return jsonify({'operators': operators})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/check_auth/<operator_name>')
def check_auth(operator_name):
    try:
        account = request.args.get('account', 'main')
        
        async def _check_auth():
            client = await create_client(operator_name, account)
            await client.connect()
            
            is_authorized = await client.is_user_authorized()
            return {
                'is_authorized': is_authorized,
                'operator': operator_name,
                'account': account
            }
        
        result = asyncio.run(_check_auth())
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logout/<operator_name>', methods=['POST'])
def logout_telegram(operator_name):
    try:
        account = request.args.get('account', 'main')
        
        async def _logout():
            client = await create_client(operator_name, account)
            await client.connect()
            
            await client.log_out()
            
            # Удаляем клиент из памяти
            client_key = f"{operator_name}_{account}" if account != 'main' else operator_name
            if client_key in clients:
                del clients[client_key]
            
            # Удаляем файл сессии
            session_file = get_session_file(operator_name, account)
            if os.path.exists(session_file):
                os.remove(session_file)
            
            return {
                'success': True,
                'message': 'Выход выполнен успешно'
            }
        
        result = asyncio.run(_logout())
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def create_admin_user():
    """Создание администратора по умолчанию"""
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(
            username='admin',
            role='admin'
        )
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        print("Создан пользователь admin с паролем admin123")

# HTML шаблоны
BASE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Telegram Dashboard{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
        <div class="container">
            <a class="navbar-brand" href="#">
                <i class="fab fa-telegram-plane"></i> Telegram Dashboard
            </a>
            
            {% if current_user.is_authenticated %}
            <div class="navbar-nav ms-auto">
                <div class="nav-item dropdown">
                    <a class="nav-link dropdown-toggle" href="#" role="button" data-bs-toggle="dropdown">
                        <i class="fas fa-user"></i> {{ current_user.username }}
                        <span class="badge bg-{% if current_user.is_admin() %}danger{% else %}info{% endif %} ms-1">
                            {{ current_user.role }}
                        </span>
                    </a>
                    <ul class="dropdown-menu">
                        <li><a class="dropdown-item" href="{{ url_for('logout') }}">
                            <i class="fas fa-sign-out-alt"></i> Выйти
                        </a></li>
                    </ul>
                </div>
            </div>
            {% endif %}
        </div>
    </nav>

    <div class="container mt-4">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{% if category == 'error' %}danger{% elif category == 'success' %}success{% elif category == 'info' %}info{% else %}warning{% endif %} alert-dismissible fade show" role="alert">
                        {{ message }}
                        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                    </div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        {% block content %}{% endblock %}
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
'''

LOGIN_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
<div class="row justify-content-center">
    <div class="col-md-6 col-lg-4">
        <div class="card shadow">
            <div class="card-header bg-primary text-white text-center">
                <h4><i class="fas fa-sign-in-alt"></i> Вход в систему</h4>
            </div>
            <div class="card-body">
                <form method="POST">
                    <div class="mb-3">
                        <label for="username" class="form-label">Логин</label>
                        <input type="text" class="form-control" id="username" name="username" required minlength="3" maxlength="80">
                    </div>
                    <div class="mb-3">
                        <label for="password" class="form-label">Пароль</label>
                        <input type="password" class="form-control" id="password" name="password" required>
                    </div>
                    <div class="d-grid">
                        <button type="submit" class="btn btn-primary btn-lg">Войти</button>
                    </div>
                </form>
            </div>
        </div>
    </div>
</div>
''')

ADMIN_DASHBOARD_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
<div class="d-flex justify-content-between align-items-center mb-4">
    <h2><i class="fas fa-tachometer-alt"></i> Панель администратора</h2>
    <a href="{{ url_for('add_user') }}" class="btn btn-success">
        <i class="fas fa-plus"></i> Добавить пользователя
    </a>
</div>

<div class="card">
    <div class="card-header">
        <h5><i class="fas fa-users"></i> Пользователи системы</h5>
    </div>
    <div class="card-body">
        <div class="table-responsive">
            <table class="table table-striped">
                <thead>
                    <tr>
                        <th>Логин</th>
                        <th>Роль</th>
                        <th>Оператор Telegram</th>
                        <th>Статус</th>
                        <th>Действия</th>
                    </tr>
                </thead>
                <tbody>
                    {% for user in users %}
                    <tr>
                        <td><i class="fas fa-user"></i> {{ user.username }}</td>
                        <td>
                            <span class="badge bg-{% if user.is_admin() %}danger{% else %}info{% endif %}">
                                {{ user.role }}
                            </span>
                        </td>
                        <td>
                            {% if user.assigned_operator_name %}
                                <i class="fab fa-telegram-plane"></i> {{ user.assigned_operator_name }}
                            {% else %}
                                <span class="text-muted">Не назначен</span>
                            {% endif %}
                        </td>
                        <td>
                            <span class="badge bg-{% if user.is_active %}success{% else %}secondary{% endif %}">
                                {% if user.is_active %}Активен{% else %}Заблокирован{% endif %}
                            </span>
                        </td>
                        <td>
                            <a href="{{ url_for('delete_user', user_id=user.id) }}" 
                               class="btn btn-sm btn-outline-danger"
                               onclick="return confirm('Удалить пользователя {{ user.username }}?')">
                                <i class="fas fa-trash"></i>
                            </a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
''')

OPERATOR_DASHBOARD_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
<div class="d-flex justify-content-between align-items-center mb-4">
    <h2><i class="fas fa-tachometer-alt"></i> Панель оператора</h2>
    <span class="badge bg-info fs-6">{{ current_user.username }}</span>
</div>

<div class="card">
    <div class="card-header">
        <h5><i class="fab fa-telegram-plane"></i> Мои Telegram чаты</h5>
    </div>
    <div class="card-body">
        {% if current_user.assigned_operator_name %}
        <p class="text-muted">
            <i class="fas fa-info-circle"></i> 
            Оператор: {{ current_user.assigned_operator_name }}
        </p>
        <div class="d-flex gap-2">
            <a href="/api/chats/{{ current_user.assigned_operator_name }}" class="btn btn-primary" target="_blank">
                <i class="fas fa-comments"></i> Просмотреть мои чаты (API)
            </a>
            <a href="/api/check_auth/{{ current_user.assigned_operator_name }}" class="btn btn-info" target="_blank">
                <i class="fas fa-check-circle"></i> Проверить авторизацию
            </a>
        </div>
        {% else %}
        <div class="alert alert-warning">
            <i class="fas fa-exclamation-triangle"></i>
            У вас не настроен доступ к Telegram. Обратитесь к администратору для назначения оператора.
        </div>
        {% endif %}
    </div>
</div>
''')

ADD_USER_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card">
            <div class="card-header">
                <h4><i class="fas fa-user-plus"></i> Добавить пользователя</h4>
            </div>
            <div class="card-body">
                <form method="POST">
                    <div class="row">
                        <div class="col-md-6">
                            <div class="mb-3">
                                <label for="username" class="form-label">Логин</label>
                                <input type="text" class="form-control" id="username" name="username" required minlength="3" maxlength="80">
                            </div>
                        </div>
                        <div class="col-md-6">
                            <div class="mb-3">
                                <label for="password" class="form-label">Пароль</label>
                                <input type="password" class="form-control" id="password" name="password" required minlength="6">
                            </div>
                        </div>
                    </div>
                    <div class="row">
                        <div class="col-md-6">
                            <div class="mb-3">
                                <label for="role" class="form-label">Роль</label>
                                <select class="form-select" id="role" name="role" required>
                                    <option value="">Выберите роль</option>
                                    <option value="operator">Оператор</option>
                                    <option value="admin">Администратор</option>
                                </select>
                            </div>
                        </div>
                        <div class="col-md-6">
                            <div class="mb-3">
                                <label for="assigned_operator_name" class="form-label">Имя оператора (для Telegram)</label>
                                <input type="text" class="form-control" id="assigned_operator_name" name="assigned_operator_name" maxlength="100">
                            </div>
                        </div>
                    </div>
                    <div class="d-flex justify-content-between">
                        <a href="{{ url_for('admin_dashboard') }}" class="btn btn-secondary">Назад</a>
                        <button type="submit" class="btn btn-success">Добавить пользователя</button>
                    </div>
                </form>
            </div>
        </div>
    </div>
</div>
''')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        create_admin_user()
    
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))