import discord
from discord.ext import commands
import sqlite3
from datetime import datetime
import re
import asyncio
import os
from dotenv import load_dotenv

# Загружаем переменные из .env (для локального запуска;
# на Railway переменные задаются в панели проекта, load_dotenv() их не трогает)
load_dotenv()

# Настройки — берутся из переменных окружения, не хардкодим в коде
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '0'))
REPORT_CHANNEL_ID = int(os.getenv('REPORT_CHANNEL_ID', '0'))
VERIFY_CHANNEL_ID = int(os.getenv('VERIFY_CHANNEL_ID', '0'))
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID', '0'))

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN не задан! Укажи его в .env (локально) "
        "или в переменных окружения на Railway."
    )

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Инициализация БД
# ВАЖНО: на Railway файловая система эфемерная — при каждом редеплое
# scores.db будет обнулена, если не подключить persistent Volume
# и не указать его путь через переменную DB_PATH (например /data/scores.db)
DB_PATH = os.getenv('DB_PATH', 'scores.db')

def init_db():
    # Если папка для базы ещё не создана (актуально для Volume на Railway,
    # когда путь примонтирован, но самой поддиректории может не быть) —
    # создаём её сами, чтобы sqlite3.connect не падал с OperationalError.
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS players
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  contract_score INTEGER DEFAULT 0,
                  activity_score INTEGER DEFAULT 0,
                  capt_score INTEGER DEFAULT 0,
                  total_score INTEGER DEFAULT 0)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS score_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  category TEXT,
                  amount INTEGER,
                  action_type TEXT,
                  reason TEXT,
                  timestamp DATETIME,
                  verified_by INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_requests
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  message_id INTEGER,
                  channel_id INTEGER,
                  category TEXT,
                  action_name TEXT,
                  amount INTEGER,
                  proof_text TEXT,
                  proof_urls TEXT,
                  proof_attachments TEXT,
                  status TEXT DEFAULT 'pending',
                  timestamp DATETIME)''')
    
    conn.commit()
    return conn

class ScoreBot:
    def __init__(self):
        self.conn = init_db()
        self.categories = {
            'contract': {
                'name': 'Контракт',
                'emoji': '📋',
                'color': discord.Color.blue(),
                'actions': {
                    'contract_done': 'Выполнен контракт',
                    'contract_partial': 'Частично выполнен контракт',
                    'contract_bonus': 'Бонус за контракт',
                    'contract_penalty': 'Штраф по контракту'
                }
            },
            'activity': {
                'name': 'Актив',
                'emoji': '⚡',
                'color': discord.Color.green(),
                'actions': {
                    'daily_activity': 'Ежедневная активность',
                    'event_participation': 'Участие в ивенте',
                    'training': 'Тренировка',
                    'help_teammates': 'Помощь союзникам',
                    'other_activity': 'Другая активность'
                }
            },
            'capt': {
                'name': 'Капт',
                'emoji': '🎯',
                'color': discord.Color.orange(),
                'actions': {
                    'capture_point': 'Захват точки',
                    'defense_point': 'Оборона точки',
                    'capture_assist': 'Помощь в захвате',
                    'capture_leader': 'Лидер захвата',
                    'other_capt': 'Другой капт'
                }
            }
        }
        self.action_points = {
            'contract_done': 50,
            'contract_partial': 25,
            'contract_bonus': 75,
            'contract_penalty': -20,
            'daily_activity': 10,
            'event_participation': 30,
            'training': 20,
            'help_teammates': 15,
            'other_activity': 5,
            'capture_point': 40,
            'defense_point': 30,
            'capture_assist': 20,
            'capture_leader': 50,
            'other_capt': 10
        }
    
    def get_player(self, user_id):
        c = self.conn.cursor()
        c.execute("SELECT * FROM players WHERE user_id=?", (user_id,))
        return c.fetchone()
    
    def add_player(self, user_id, username):
        c = self.conn.cursor()
        c.execute("INSERT OR IGNORE INTO players (user_id, username) VALUES (?, ?)",
                 (user_id, username))
        self.conn.commit()
    
    def update_score(self, user_id, category, amount):
        c = self.conn.cursor()
        column = f"{category}_score"
        c.execute(f"UPDATE players SET {column} = {column} + ? WHERE user_id=?",
                 (amount, user_id))
        c.execute("""UPDATE players SET total_score = 
                    contract_score + activity_score + capt_score 
                    WHERE user_id=?""", (user_id,))
        self.conn.commit()
    
    def reset_scores(self, user_id, reason, admin_id):
        c = self.conn.cursor()
        player = self.get_player(user_id)
        if player:
            c.execute("""INSERT INTO score_history 
                        (user_id, category, action_type, reason, timestamp, verified_by)
                        VALUES (?, 'all', 'reset', ?, ?, ?)""",
                     (user_id, reason, datetime.now(), admin_id))
            
            c.execute("""UPDATE players SET 
                        contract_score = 0,
                        activity_score = 0,
                        capt_score = 0,
                        total_score = 0
                        WHERE user_id=?""", (user_id,))
            self.conn.commit()
            return True
        return False
    
    def create_request(self, user_id, message_id, channel_id, category, action_name, 
                       amount, proof_text, proof_urls, proof_attachments):
        c = self.conn.cursor()
        c.execute("""INSERT INTO pending_requests 
                    (user_id, message_id, channel_id, category, action_name, amount, 
                     proof_text, proof_urls, proof_attachments, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (user_id, message_id, channel_id, category, action_name, amount,
                  proof_text, proof_urls, proof_attachments, datetime.now()))
        self.conn.commit()
        return c.lastrowid

score_bot = ScoreBot()

@bot.event
async def on_ready():
    print(f'Бот {bot.user} запущен!')
    guild = bot.get_guild(GUILD_ID)
    if guild:
        print(f'Подключен к серверу: {guild.name}')

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    # Команда для создания заявки
    if message.content.startswith('!report'):
        await create_report_command(message)
    elif message.channel.id == REPORT_CHANNEL_ID:
        # Автоматически показываем интерфейс создания заявки
        await show_report_interface(message)
    
    await bot.process_commands(message)

async def show_report_interface(message):
    """Показывает интерфейс для создания заявки"""
    embed = discord.Embed(
        title="📝 Создание заявки на баллы",
        description="Выберите категорию действия:",
        color=discord.Color.blue()
    )
    
    view = CategorySelectionView(message)
    await message.channel.send(embed=embed, view=view, delete_after=300)

class CategorySelectionView(discord.ui.View):
    def __init__(self, original_message):
        super().__init__(timeout=300)
        self.original_message = original_message
    
    @discord.ui.button(label="Контракт", style=discord.ButtonStyle.primary, emoji="📋")
    async def contract_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_actions(interaction, "contract")
    
    @discord.ui.button(label="Актив", style=discord.ButtonStyle.success, emoji="⚡")
    async def activity_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_actions(interaction, "activity")
    
    @discord.ui.button(label="Капт", style=discord.ButtonStyle.danger, emoji="🎯")
    async def capt_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_actions(interaction, "capt")
    
    async def show_actions(self, interaction, category):
        actions = score_bot.categories[category]['actions']
        embed = discord.Embed(
            title=f"{score_bot.categories[category]['emoji']} {score_bot.categories[category]['name']}",
            description="Выберите конкретное действие:",
            color=score_bot.categories[category]['color']
        )
        
        view = ActionSelectionView(self.original_message, category)
        
        # Добавляем кнопки для каждого действия
        for action_id, action_name in actions.items():
            points = score_bot.action_points.get(action_id, 0)
            button = discord.ui.Button(
                label=f"{action_name} ({points} баллов)",
                style=discord.ButtonStyle.secondary,
                custom_id=f"action_{action_id}"
            )
            button.callback = self.create_action_callback(action_id, action_name, points)
            view.add_item(button)
        
        await interaction.response.edit_message(embed=embed, view=view)
    
    def create_action_callback(self, action_id, action_name, points):
        async def action_callback(interaction: discord.Interaction):
            await self.show_proof_modal(interaction, action_id, action_name, points)
        return action_callback
    
    async def show_proof_modal(self, interaction, action_id, action_name, points):
        modal = ProofModal(self.original_message, action_id, action_name, points)
        await interaction.response.send_modal(modal)

class ActionSelectionView(discord.ui.View):
    def __init__(self, original_message, category):
        super().__init__(timeout=300)
        self.original_message = original_message
        self.category = category

class ProofModal(discord.ui.Modal, title="Доказательства выполнения"):
    def __init__(self, original_message, action_id, action_name, points):
        super().__init__()
        self.original_message = original_message
        self.action_id = action_id
        self.action_name = action_name
        self.points = points
    
    comment = discord.ui.TextInput(
        label="Комментарий",
        placeholder="Опишите, что вы сделали (опционально)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    
    proof_link = discord.ui.TextInput(
        label="Ссылка на доказательство",
        placeholder="Вставьте ссылку на сообщение Discord или скриншот",
        required=False,
        style=discord.TextStyle.short,
        max_length=200
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        # Собираем информацию
        proof_text = self.comment.value if self.comment.value else ""
        proof_urls = []
        
        if self.proof_link.value:
            proof_urls.append(self.proof_link.value)
        
        # Проверяем вложения в оригинальном сообщении
        proof_attachments = []
        if self.original_message.attachments:
            for attachment in self.original_message.attachments:
                proof_attachments.append(attachment.url)
        
        # Если нет ни ссылок, ни вложений - создаем заявку только с текстом
        if not proof_urls and not proof_attachments and not proof_text:
            await interaction.response.send_message(
                "❌ Необходимо предоставить хотя бы комментарий, ссылку или вложение!",
                ephemeral=True
            )
            return
        
        # Определяем категорию по action_id
        category = None
        for cat_id, cat_data in score_bot.categories.items():
            if self.action_id in cat_data['actions']:
                category = cat_id
                break
        
        if not category:
            await interaction.response.send_message(
                "❌ Ошибка: неизвестная категория!",
                ephemeral=True
            )
            return
        
        # Создаем заявку
        request_id = score_bot.create_request(
            user_id=self.original_message.author.id,
            message_id=self.original_message.id,
            channel_id=self.original_message.channel.id,
            category=category,
            action_name=self.action_name,
            amount=self.points,
            proof_text=proof_text,
            proof_urls="\n".join(proof_urls) if proof_urls else None,
            proof_attachments="\n".join(proof_attachments) if proof_attachments else None
        )
        
        # Отправляем подтверждение
        embed = discord.Embed(
            title=f"✅ Заявка #{request_id} создана!",
            description=f"**Действие:** {self.action_name}\n"
                       f"**Баллы:** {self.points}\n"
                       f"**Категория:** {score_bot.categories[category]['name']}",
            color=discord.Color.green()
        )
        
        if proof_text:
            embed.add_field(name="📝 Комментарий", value=proof_text, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Отправляем заявку в канал верификации
        await send_to_verification_channel(request_id, self.original_message.author, 
                                          category, self.action_name, self.points,
                                          proof_text, proof_urls, proof_attachments)

async def create_report_command(message):
    """Обрабатывает команду !report"""
    embed = discord.Embed(
        title="📝 Создание заявки на баллы",
        description="Выберите категорию действия:",
        color=discord.Color.blue()
    )
    
    view = CategorySelectionView(message)
    await message.channel.send(embed=embed, view=view, delete_after=300)

async def send_to_verification_channel(request_id, author, category, action_name, 
                                      points, proof_text, proof_urls, proof_attachments):
    """Отправляет заявку в канал верификации"""
    verify_channel = bot.get_channel(VERIFY_CHANNEL_ID)
    if not verify_channel:
        return
    
    cat_data = score_bot.categories[category]
    
    embed = discord.Embed(
        title=f"📝 Заявка #{request_id}",
        description=f"**От:** {author.mention}\n"
                   f"**Действие:** {action_name}\n"
                   f"**Баллы:** {points}\n"
                   f"**Категория:** {cat_data['emoji']} {cat_data['name']}",
        color=cat_data['color'],
        timestamp=datetime.now()
    )
    
    if proof_text:
        embed.add_field(name="📝 Комментарий", value=proof_text, inline=False)
    
    if proof_urls:
        embed.add_field(name="🔗 Ссылки", value=proof_urls, inline=False)
    
    if proof_attachments:
        embed.add_field(name="📎 Вложения", value=proof_attachments, inline=False)
    
    # Добавляем превью первого вложения
    if proof_attachments:
        first_attachment = proof_attachments.split('\n')[0]
        if first_attachment.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            embed.set_image(url=first_attachment)
    
    # Кнопки для управления заявкой
    view = VerificationView(request_id)
    
    await verify_channel.send(embed=embed, view=view)

class VerificationView(discord.ui.View):
    def __init__(self, request_id):
        super().__init__(timeout=None)
        self.request_id = request_id
    
    @discord.ui.button(label="Одобрить", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Проверяем права
        if not any(role.id == STAFF_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message(
                "❌ У вас нет прав для этого действия!",
                ephemeral=True
            )
            return
        
        c = score_bot.conn.cursor()
        c.execute("SELECT * FROM pending_requests WHERE id=? AND status='pending'",
                 (self.request_id,))
        request = c.fetchone()
        
        if not request:
            await interaction.response.send_message(
                "❌ Заявка не найдена или уже обработана!",
                ephemeral=True
            )
            return
        
        user_id = request[1]
        category = request[4]
        amount = request[6]
        
        # Начисляем баллы
        user = bot.get_user(user_id)
        if user:
            score_bot.add_player(user_id, user.name)
        
        score_bot.update_score(user_id, category, amount)
        
        # Обновляем статус
        c.execute("UPDATE pending_requests SET status='approved' WHERE id=?",
                 (self.request_id,))
        score_bot.conn.commit()
        
        # Обновляем embed
        embed = interaction.message.embeds[0]
        embed.title = f"✅ Заявка #{self.request_id} - ОДОБРЕНА"
        embed.color = discord.Color.green()
        embed.add_field(
            name=f"Проверено: {interaction.user.name}",
            value=datetime.now().strftime("%Y-%m-%d %H:%M"),
            inline=False
        )
        
        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message(
            f"✅ Заявка #{self.request_id} одобрена! +{amount} баллов",
            ephemeral=True
        )
        
        # Уведомляем игрока
        if user:
            try:
                player_embed = discord.Embed(
                    title="🎉 Баллы начислены!",
                    description=f"Ваша заявка #{self.request_id} одобрена!\n"
                               f"+{amount} баллов",
                    color=discord.Color.green()
                )
                await user.send(embed=player_embed)
            except discord.Forbidden:
                pass
    
    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id == STAFF_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message(
                "❌ У вас нет прав для этого действия!",
                ephemeral=True
            )
            return
        
        # Показываем модальное окно для причины отказа
        modal = DenyReasonModal(self.request_id)
        await interaction.response.send_modal(modal)

class DenyReasonModal(discord.ui.Modal, title="Причина отклонения"):
    def __init__(self, request_id):
        super().__init__()
        self.request_id = request_id
    
    reason = discord.ui.TextInput(
        label="Причина",
        placeholder="Укажите причину отклонения",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        c = score_bot.conn.cursor()
        c.execute("UPDATE pending_requests SET status='denied' WHERE id=? AND status='pending'",
                 (self.request_id,))
        
        if c.rowcount == 0:
            await interaction.response.send_message(
                "❌ Заявка не найдена или уже обработана!",
                ephemeral=True
            )
            return
        
        score_bot.conn.commit()
        
        # Обновляем embed
        embed = interaction.message.embeds[0]
        embed.title = f"❌ Заявка #{self.request_id} - ОТКЛОНЕНА"
        embed.color = discord.Color.red()
        embed.add_field(
            name="Причина отказа",
            value=self.reason.value,
            inline=False
        )
        embed.add_field(
            name=f"Проверено: {interaction.user.name}",
            value=datetime.now().strftime("%Y-%m-%d %H:%M"),
            inline=False
        )
        
        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message(
            f"❌ Заявка #{self.request_id} отклонена",
            ephemeral=True
        )
        
        # Уведомляем игрока
        c.execute("SELECT user_id FROM pending_requests WHERE id=?", (self.request_id,))
        result = c.fetchone()
        if result:
            user = bot.get_user(result[0])
            if user:
                try:
                    player_embed = discord.Embed(
                        title="❌ Заявка отклонена",
                        description=f"Ваша заявка #{self.request_id} отклонена.\n"
                                   f"Причина: {self.reason.value}",
                        color=discord.Color.red()
                    )
                    await user.send(embed=player_embed)
                except discord.Forbidden:
                    pass

# Остальные команды (leaderboard, my_scores, reset_scores, history, pending)
@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Показать таблицу лидеров"""
    c = score_bot.conn.cursor()
    c.execute("""SELECT username, contract_score, activity_score, capt_score, total_score 
                FROM players 
                ORDER BY total_score DESC 
                LIMIT 10""")
    players = c.fetchall()
    
    embed = discord.Embed(
        title="🏆 Таблица лидеров",
        color=discord.Color.gold()
    )
    
    if players:
        medals = ["🥇", "🥈", "🥉"]
        for i, (username, contract, activity, capt, total) in enumerate(players, 1):
            medal = medals[i-1] if i <= 3 else f"{i}."
            embed.add_field(
                name=f"{medal} {username}",
                value=f"📋 Контракт: {contract}\n"
                     f"⚡ Актив: {activity}\n"
                     f"🎯 Капт: {capt}\n"
                     f"**Всего: {total}**",
                inline=False
            )
    else:
        embed.description = "Пока нет данных"
    
    await ctx.send(embed=embed)

@bot.command(name='my_scores')
async def my_scores(ctx):
    """Показать мои баллы"""
    player = score_bot.get_player(ctx.author.id)
    
    if player:
        embed = discord.Embed(
            title=f"📊 Баллы игрока {player[1]}",
            color=discord.Color.blue()
        )
        embed.add_field(name="📋 Контракт", value=player[2], inline=True)
        embed.add_field(name="⚡ Актив", value=player[3], inline=True)
        embed.add_field(name="🎯 Капт", value=player[4], inline=True)
        embed.add_field(name="**Всего**", value=player[5], inline=False)
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("У вас пока нет баллов")

@bot.command(name='reset_scores')
@commands.has_role(STAFF_ROLE_ID)
async def reset_scores_command(ctx, member: discord.Member, *, reason: str):
    """Обнулить баллы игрока"""
    if score_bot.reset_scores(member.id, reason, ctx.author.id):
        embed = discord.Embed(
            title="🔄 Баллы обнулены",
            description=f"Игрок: {member.mention}\nПричина: {reason}",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ Игрок не найден в базе")

@bot.command(name='history')
@commands.has_role(STAFF_ROLE_ID)
async def history(ctx, member: discord.Member, limit: int = 10):
    """Показать историю операций игрока"""
    c = score_bot.conn.cursor()
    c.execute("""SELECT category, amount, action_type, reason, timestamp, verified_by 
                FROM score_history 
                WHERE user_id=? 
                ORDER BY timestamp DESC 
                LIMIT ?""", (member.id, limit))
    records = c.fetchall()
    
    embed = discord.Embed(
        title=f"📜 История операций: {member.name}",
        color=discord.Color.purple()
    )
    
    if records:
        for category, amount, action_type, reason, timestamp, verified_by in records:
            admin = bot.get_user(verified_by)
            admin_name = admin.name if admin else "Unknown"
            embed.add_field(
                name=f"{timestamp.strftime('%Y-%m-%d %H:%M')}",
                value=f"Категория: {category}\n"
                     f"Сумма: {amount}\n"
                     f"Тип: {action_type}\n"
                     f"Причина: {reason}\n"
                     f"Кем: {admin_name}",
                inline=False
            )
    else:
        embed.description = "Нет записей"
    
    await ctx.send(embed=embed)

@bot.command(name='pending')
@commands.has_role(STAFF_ROLE_ID)
async def pending_requests(ctx):
    """Показать все необработанные заявки"""
    c = score_bot.conn.cursor()
    c.execute("""SELECT id, user_id, category, action_name, amount, timestamp 
                FROM pending_requests 
                WHERE status='pending' 
                ORDER BY timestamp DESC""")
    requests = c.fetchall()
    
    if requests:
        embed = discord.Embed(
            title=f"📋 Необработанные заявки ({len(requests)})",
            color=discord.Color.blue()
        )
        
        for req_id, user_id, category, action_name, amount, timestamp in requests:
            user = bot.get_user(user_id)
            user_name = user.name if user else f"User {user_id}"
            cat_emoji = score_bot.categories[category]['emoji']
            
            embed.add_field(
                name=f"Заявка #{req_id} - {cat_emoji} {action_name}",
                value=f"Игрок: {user_name}\nБаллы: {amount}",
                inline=False
            )
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("✅ Нет необработанных заявок!")

@bot.command(name='actions')
async def list_actions(ctx):
    """Показать все доступные действия и их стоимость"""
    embed = discord.Embed(
        title="📋 Доступные действия и баллы",
        color=discord.Color.blue()
    )
    
    for cat_id, cat_data in score_bot.categories.items():
        actions_text = ""
        for action_id, action_name in cat_data['actions'].items():
            points = score_bot.action_points.get(action_id, 0)
            actions_text += f"• {action_name}: {points} баллов\n"
        
        embed.add_field(
            name=f"{cat_data['emoji']} {cat_data['name']}",
            value=actions_text,
            inline=False
        )
    
    await ctx.send(embed=embed)

# Запуск бота
if __name__ == "__main__":
    bot.run(TOKEN)
