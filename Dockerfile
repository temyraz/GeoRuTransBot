# Лёгкий образ для запуска Telegram-бота в PaaS (Railway, Render и т.п.)
FROM python:3.11-slim

# Отключаем буферизацию вывода, чтобы логи сразу попадали в консоль/лог-сервис
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db.py modes.py prompts.py keyboards.py vocabulary.py transliteration.py smart_routing.py .

# Бот работает через long polling — открывать HTTP-порт не нужно
CMD ["python", "main.py"]