import os, math, requests
import io
import matplotlib.pyplot as plt
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import BaseMiddleware
from aiogram.types import BufferedInputFile
import asyncio

load_dotenv()
bot_token = os.getenv('bot_token')
openweather_api_key = os.getenv('openweather_api_key')

if not bot_token:
    raise ValueError('Не найден bot_token. Добавь его в переменные окружения или .env')
if not openweather_api_key:
    raise ValueError('Не найден openweather_api_key. Добавь его в переменные окружения или .env')

bot = Bot(token=bot_token)
dp = Dispatcher(storage=MemoryStorage())

from aiogram import BaseMiddleware

class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            u = event.from_user
            print(f"[CMD] user_id={u.id} username=@{u.username} text={event.text}")
        return await handler(event, data)

dp.message.middleware(LoggingMiddleware())

print('Ключи найдены, бот и диспетчер созданы.')

users = {}  # user_id -> dict

def now_str() -> str:
    return datetime.now().strftime("%H:%M")

def ensure_history(u: dict):
    # история: список кортежей (time_str, value)
    u.setdefault("water_history", [])
    u.setdefault("cal_history", [])
    u.setdefault("burn_history", [])

def calc_water_goal(weight_kg: float, activity_min: int, temp_c: float | None) -> int:
    # База: вес * 30 мл
    base = weight_kg * 30

    # +500 мл за каждые 30 минут активности
    extra_activity = 500 * (activity_min // 30)

    # +500 мл если жарко (>25°C)
    extra_heat = 0
    if temp_c is not None and temp_c > 25:
        extra_heat = 500

    return int(base + extra_activity + extra_heat)

def calc_calorie_goal(weight_kg: float, height_cm: int, age: int, activity_min: int, manual_goal: int | None = None) -> int:
    if manual_goal is not None:
        return int(manual_goal)

    base = 10 * weight_kg + 6.25 * height_cm - 5 * age

    # Надбавка за активность
    extra = 0
    if activity_min >= 60:
        extra = 400
    elif activity_min >= 30:
        extra = 200

    return int(base + extra)

workout_kcal_per_min = {
    "бег": 10,      # 30 мин -> ~300 ккал 
    "ходьба": 4,
    "зал": 8,
    "вело": 7,
    "плавание": 9,
}
default_workout_kcal_per_min = 7

def calc_workout_burned(workout_type: str, minutes: int) -> int:
    k = workout_kcal_per_min.get(workout_type.lower(), default_workout_kcal_per_min)
    return int(k * minutes)

def workout_extra_water_ml(minutes: int) -> int:
    # +200 мл за каждые 30 минут
    return 200 * (minutes // 30)

def get_temperature_c(city: str) -> float | None:
    """
    Возвращает температуру в градусах Цельсия для указанного города.
    Если не получилось — возвращает None.
    """
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "q": city,
        "appid": openweather_api_key,
        "units": "metric",
        "lang": "ru",
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        return float(data["main"]["temp"])
    except Exception:
        return None

def get_food_kcal_per_100g(query: str) -> tuple[str, float] | None:
    """
    Ищем продукт в OpenFoodFacts и возвращаем (product_name, kcal_per_100g).
    Берём первый продукт, где есть kcal на 100г.
    Если не нашли — None.
    """
    url = "https://world.openfoodfacts.org/cgi/search.pl"
    params = {
        "search_terms": query,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": 10,
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        products = data.get("products", [])

        for p in products:
            nutr = p.get("nutriments", {})

            # 1) Если есть kcal/100g напрямую
            kcal = nutr.get("energy-kcal_100g")
            if kcal is not None:
                name = p.get("product_name") or p.get("generic_name") or query
                return (name, float(kcal))

            # 2) Иногда есть только energy_100g в kJ — переведём в kcal (kcal = kJ / 4.184)
            kj = nutr.get("energy_100g")
            if kj is not None:
                name = p.get("product_name") or p.get("generic_name") or query
                kcal_from_kj = float(kj) / 4.184
                return (name, kcal_from_kj)

        return None
    except Exception:
        return None

class ProfileForm(StatesGroup):
    weight = State()
    height = State()
    age = State()
    activity = State()
    city = State()
    manual_choice = State()
    manual_calories = State()

def parse_float(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except Exception:
        return None

def parse_int(text: str) -> int | None:
    try:
        return int(text)
    except Exception:
        return None

@dp.message(Command("set_profile"))
async def cmd_set_profile(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Давайте настроим профиль.\n\n"
                         "Введите вес (кг), например: 70\n"
                         "Отмена: /cancel")
    await state.set_state(ProfileForm.weight)

@dp.message(ProfileForm.weight, ~F.text.startswith("/"))
async def process_weight(message: Message, state: FSMContext):
    w = parse_float(message.text)
    if w is None or w <= 0 or w > 400:
        await message.answer("Вес должен быть числом в кг (например 70). Попробуйте ещё раз:")
        return

    await state.update_data(weight=w)
    await message.answer("Введите рост (см), например: 175")
    await state.set_state(ProfileForm.height)

@dp.message(ProfileForm.height, ~F.text.startswith("/"))
async def process_height(message: Message, state: FSMContext):
    h = parse_int(message.text)
    if h is None or h < 50 or h > 260:
        await message.answer("Рост должен быть целым числом в см (например 175). Попробуйте ещё раз:")
        return

    await state.update_data(height=h)
    await message.answer("Введите возраст (лет), например: 22")
    await state.set_state(ProfileForm.age)

@dp.message(ProfileForm.age, ~F.text.startswith("/"))
async def process_age(message: Message, state: FSMContext):
    a = parse_int(message.text)
    if a is None or a < 5 or a > 120:
        await message.answer("Возраст должен быть целым числом (например 22). Попробуйте ещё раз:")
        return

    await state.update_data(age=a)
    await message.answer("Введите активность (минуты в день), например: 40")
    await state.set_state(ProfileForm.activity)

@dp.message(ProfileForm.activity, ~F.text.startswith("/"))
async def process_activity(message: Message, state: FSMContext):
    act = parse_int(message.text)
    if act is None or act < 0 or act > 1000:
        await message.answer("Активность должна быть целым числом минут (например 40). Попробуйте ещё раз:")
        return

    await state.update_data(activity=act)
    await message.answer("Введите город, например: Moscow или Tel Aviv\n"
                         "Отмена: /cancel")
    await state.set_state(ProfileForm.city)

@dp.message(ProfileForm.city, ~F.text.startswith("/"))
async def process_city(message: Message, state: FSMContext):
    city = message.text.strip()
    if not city:
        await message.answer("Город не должен быть пустым. Введите город, например: Moscow")
        return

    await state.update_data(city=city)
    await message.answer("Хотите задать цель по калориям вручную? (Да/Нет)\n"
                         "Отмена: /cancel")
    await state.set_state(ProfileForm.manual_choice)

@dp.message(ProfileForm.manual_choice, ~F.text.startswith("/"))
async def process_manual_choice(message: Message, state: FSMContext):
    ans = message.text.strip().lower()

    if ans in ("да", "yes", "y"):
        await message.answer("Введите цель по калориям (ккал в день), например: 2000")
        await state.set_state(ProfileForm.manual_calories)
        return

    if ans in ("нет", "no", "n"):
        data = await state.get_data()
        await save_profile_and_reply(message, state, manual_goal=None, data=data)
        return

    await message.answer("Ответьте 'да' или 'нет'.")

@dp.message(ProfileForm.manual_calories, ~F.text.startswith("/"))
async def process_manual_calories(message: Message, state: FSMContext):
    goal = parse_int(message.text)
    if goal is None or goal < 800 or goal > 10000:
        await message.answer("Цель калорий должна быть числом (например 2000). Попробуйте ещё раз:")
        return

    data = await state.get_data()
    await save_profile_and_reply(message, state, manual_goal=goal, data=data)

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    cur = await state.get_state()
    if cur is None:
        await message.answer("Сейчас нечего отменять 🙂")
        return
    await state.clear()
    await message.answer("Ок, отмена ✅")

async def save_profile_and_reply(message: Message, state: FSMContext, manual_goal: int | None, data: dict):
    user_id = message.from_user.id

    weight = float(data["weight"])
    height = int(data["height"])
    age = int(data["age"])
    activity = int(data["activity"])
    city = str(data["city"])

    temp = get_temperature_c(city)  # может вернуть None

    water_goal = calc_water_goal(weight, activity, temp)
    calorie_goal = calc_calorie_goal(weight, height, age, activity, manual_goal=manual_goal)

    users[user_id] = {
        "weight": weight,
        "height": height,
        "age": age,
        "activity": activity,
        "city": city,
        "temp": temp,
        "water_goal": water_goal,
        "calorie_goal": calorie_goal,

        # дневные логи
        "logged_water": 0,
        "logged_calories": 0,
        "burned_calories": 0,
    }

    ensure_history(users[user_id])
    t = now_str()
    users[user_id]["water_history"].append((t, users[user_id]["logged_water"]))
    users[user_id]["cal_history"].append((t, users[user_id]["logged_calories"]))
    users[user_id]["burn_history"].append((t, users[user_id]["burned_calories"]))

    temp_text = f"{temp:.1f}°C" if temp is not None else "Не удалось определить"

    await message.answer(
        "✅ Профиль сохранён.\n"
        f"Город: {city} (температура: {temp_text})\n\n"
        f"💧 Норма воды: {water_goal} мл/день\n"
        f"🔥 Норма калорий: {calorie_goal} ккал/день\n\n"
        "Теперь можно:\n"
        "/log_water\n"
        "/log_food\n"
        "/log_workout\n"
        "/check_progress\n"
        "/reset_day"
    )

    await state.clear()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для расчёта нормы воды, калорий и отслеживания прогресса.\n\n"
        "Основные команды:\n"
        "/set_profile — настроить профиль\n"
        "/log_water — добавить воду (мл)\n"
        "/log_food — добавить еду (потом спрошу граммы)\n"
        "/log_workout — добавить тренировку\n"
        "/check_progress — прогресс за день\n"
        "/reset_day — сбросить дневные логи\n"
        "/plot - графики прогресса\n"
        "/recommend - рекомендации"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await cmd_start(message)

def get_user_or_none(user_id: int) -> dict | None:
    return users.get(user_id)

@dp.message(Command("reset_day"))
async def cmd_reset_day(message: Message):
    user_id = message.from_user.id
    u = get_user_or_none(user_id)

    if not u:
        await message.answer("Сначала настрой профиль: /set_profile")
        return

    u["logged_water"] = 0
    u["logged_calories"] = 0
    u["burned_calories"] = 0
    u["water_history"] = []
    u["cal_history"] = []
    u["burn_history"] = []

    t = now_str()
    u["water_history"].append((t, 0))
    u["cal_history"].append((t, 0))
    u["burn_history"].append((t, 0))

    await message.answer("✅ Дневные данные сброшены. Профиль сохранён.")

@dp.message(Command("log_water"))
async def cmd_log_water(message: Message):
    user_id = message.from_user.id
    u = get_user_or_none(user_id)

    if not u:
        await message.answer("Сначала настрой профиль: /set_profile")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи количество воды в мл. Пример: /log_water 250")
        return

    ml = parse_int(parts[1].strip())
    if ml is None or ml <= 0 or ml > 5000:
        await message.answer("Введи корректное число мл (например 250).")
        return

    u["logged_water"] += ml
    ensure_history(u)
    u["water_history"].append((now_str(), u["logged_water"]))

    goal = u["water_goal"]
    done = u["logged_water"]
    left = max(goal - done, 0)

    await message.answer(
        f"💧 Записано: {ml} мл\n"
        f"Всего за день: {done} / {goal} мл\n"
        f"Осталось: {left} мл"
    )

class FoodForm(StatesGroup):
    waiting_grams = State()

@dp.message(Command("log_food"))
async def cmd_log_food(message: Message, state: FSMContext):
    user_id = message.from_user.id
    u = get_user_or_none(user_id)

    if not u:
        await message.answer("Сначала настрой профиль: /set_profile")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи продукт. Пример: /log_food banana")
        return

    query = parts[1].strip()
    info = get_food_kcal_per_100g(query)

    if not info:
        await message.answer("Не удалось найти продукт. Попробуй другое название (например на английском).")
        return

    name, kcal100 = info

    # сохраняем во временные данные FSM
    await state.update_data(food_name=name, food_kcal100=kcal100)

    await message.answer(
        f"🍏 Найдено: {name}\n"
        f"Калорийность: {kcal100:.1f} ккал/100г\n\n"
        "Сколько грамм съели? (введи число, например 120)\n"
        "Отмена: /cancel"
    )
    await state.set_state(FoodForm.waiting_grams)

@dp.message(FoodForm.waiting_grams, ~F.text.startswith("/"))
async def process_food_grams(message: Message, state: FSMContext):
    user_id = message.from_user.id
    u = get_user_or_none(user_id)

    if not u:
        await state.clear()
        await message.answer("Сначала настрой профиль: /set_profile")
        return

    grams = parse_int(message.text.strip())
    if grams is None or grams <= 0 or grams > 5000:
        await message.answer("Введи граммы числом (например 120).")
        return

    data = await state.get_data()
    name = data["food_name"]
    kcal100 = float(data["food_kcal100"])

    added = kcal100 * grams / 100.0
    u["logged_calories"] += int(round(added))
    ensure_history(u)
    u["cal_history"].append((now_str(), u["logged_calories"]))

    await message.answer(
        f"✅ Записано: {name}\n"
        f"Граммы: {grams} г\n"
        f"Добавлено: ~{int(round(added))} ккал\n"
        f"Всего съедено за день: {u['logged_calories']} ккал"
    )

    await state.clear()

low_cal_foods = [
    "огурцы", "помидоры", "салат/зелень", "брокколи", "цветная капуста",
    "куриная грудка", "яйца", "творог (если можно)", "греческий йогурт", "ягоды"
]

@dp.message(Command("recommend"))
async def cmd_recommend(message: Message):
    user_id = message.from_user.id
    u = get_user_or_none(user_id)

    if not u:
        await message.answer("Сначала настрой профиль: /set_profile")
        return

    water_goal = u["water_goal"]
    water_done = u["logged_water"]
    water_left = max(water_goal - water_done, 0)

    cal_goal = u["calorie_goal"]
    cal_done = u["logged_calories"]
    cal_burn = u["burned_calories"]
    balance = cal_done - cal_burn  # фактический "приход" с учётом тренировок

    tips = []

    # вода
    if water_left > 0:
        # предложим разбить на 2-3 порции
        portion = 250 if water_left >= 250 else water_left
        tips.append(f"💧 До нормы воды осталось {water_left} мл. Выпей сейчас {portion} мл и ещё раз через 30–60 минут.")
    else:
        tips.append("💧 По воде ты уже в норме на сегодня ✅")

    # калории
    if balance > cal_goal:
        over = balance - cal_goal
        tips.append(f"🍽 Ты выше цели на ~{over} ккал. Можно компенсировать прогулкой 30–40 минут или лёгкой тренировкой.")
    else:
        left = cal_goal - balance
        if left > 300:
            tips.append(f"🍽 До цели осталось ~{left} ккал. Лучше добрать чем-то лёгким и белковым.")
        else:
            tips.append(f"🍽 До цели осталось ~{left} ккал — можно закрыть маленьким перекусом.")

    # идеи еды
    food_suggestions = ", ".join(low_cal_foods[:5])
    tips.append(f"🥗 Идеи низкокалорийных продуктов: {food_suggestions}.")

    # идеи тренировки
    tips.append("🏃 Идеи активности: ходьба 20–30 мин, лёгкий бег 15–20 мин, вело 20 мин.")

    await message.answer("\n\n".join(tips))

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Сейчас нечего отменять 🙂")
        return

    await state.clear()
    await message.answer("✅ Отменено. Можешь начать заново: /set_profile или /log_food ...")

@dp.message(Command("log_workout"))
async def cmd_log_workout(message: Message):
    user_id = message.from_user.id
    u = get_user_or_none(user_id)

    if not u:
        await message.answer("Сначала настрой профиль: /set_profile")
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Формат: /log_workout <тип> <мин>\nПример: /log_workout бег 30")
        return

    workout_type = parts[1].strip().lower()
    minutes = parse_int(parts[2].strip())

    if minutes is None or minutes <= 0 or minutes > 1000:
        await message.answer("Минуты должны быть числом (например 30).")
        return

    burned = calc_workout_burned(workout_type, minutes)
    extra_water = workout_extra_water_ml(minutes)

    u["burned_calories"] += burned
    u["water_goal"] += extra_water  # Тренировка увеличивает цель воды
    ensure_history(u)
    u["burn_history"].append((now_str(), u["burned_calories"]))

    await message.answer(
        f"🏋️ Тренировка записана: {workout_type}, {minutes} мин\n"
        f"🔥 Сожжено: ~{burned} ккал\n"
        f"💧 Норма воды увеличена на: {extra_water} мл\n"
        f"Новая норма воды: {u['water_goal']} мл"
    )

@dp.message(Command("check_progress"))
async def cmd_check_progress(message: Message):
    user_id = message.from_user.id
    u = get_user_or_none(user_id)

    if not u:
        await message.answer("Сначала настрой профиль: /set_profile")
        return

    water_goal = u["water_goal"]
    water_done = u["logged_water"]
    water_left = max(water_goal - water_done, 0)

    cal_goal = u["calorie_goal"]
    cal_done = u["logged_calories"]
    cal_burn = u["burned_calories"]

    balance = cal_done - cal_burn  # сколько "в плюс" по еде с учетом тренировок
    cal_left = max(cal_goal - balance, 0)

    await message.answer(
        "📊 Прогресс за день:\n\n"
        f"💧 Вода: {water_done}/{water_goal} мл (осталось {water_left} мл)\n"
        f"🍽 Съедено: {cal_done} ккал\n"
        f"🏃 Сожжено: {cal_burn} ккал\n"
        f"⚖️ Баланс: {balance} ккал\n"
        f"🎯 Цель: {cal_goal} ккал\n"
        f"Осталось до цели: {cal_left} ккал"
    )

def build_plot(times: list[str], values: list[int], title: str, y_label: str, goal: int | None = None) -> io.BytesIO:
    plt.figure()
    plt.plot(times, values, marker="o")

    # линия цели
    if goal is not None:
        plt.axhline(y=goal, linestyle="--")
        # подпись цели
        plt.text(times[-1], goal, f" цель {goal}", va="bottom")

    plt.title(title)
    plt.xlabel("Время")
    plt.ylabel(y_label)
    plt.grid(True)

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png")
    plt.close()
    buf.seek(0)
    return buf

@dp.message(Command("plot"))
async def cmd_plot(message: Message):
    user_id = message.from_user.id
    u = get_user_or_none(user_id)

    if not u:
        await message.answer("Сначала настрой профиль: /set_profile")
        return

    ensure_history(u)

    # если нет точек — нечего рисовать
    if len(u["water_history"]) < 2 and len(u["cal_history"]) < 2:
        await message.answer("Пока недостаточно данных для графиков. Сначала добавь воду/еду/тренировку.")
        return

    # график воды
    if len(u["water_history"]) >= 2:
        t_w = [x[0] for x in u["water_history"]]
        v_w = [x[1] for x in u["water_history"]]
        buf_w = build_plot(t_w, v_w, "Прогресс воды за день", "мл", goal=u["water_goal"])
        await message.answer_photo(BufferedInputFile(buf_w.getvalue(), filename="water.png"))

    # график полученных калорий
    if len(u["cal_history"]) >= 2:
        t_c = [x[0] for x in u["cal_history"]]
        v_c = [x[1] for x in u["cal_history"]]
        buf_c = build_plot(t_c, v_c, "Прогресс калорий за день", "ккал", goal=u["calorie_goal"])
        await message.answer_photo(BufferedInputFile(buf_c.getvalue(), filename="calories.png"))

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
