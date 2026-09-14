FROM python:3.12-slim

WORKDIR /app

# зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код бота
COPY bot.py .

# история и настройки будут храниться здесь (можно примонтировать volume)
VOLUME ["/data"]
ENV HISTORY_CSV=/data/price_history.csv
ENV SETTINGS_JSON=/data/settings.json

CMD ["python", "bot.py"]
