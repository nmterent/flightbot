FROM python:3.12-slim
 
WORKDIR /app
 
# зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
# код бота
COPY bot.py .
 
CMD ["python", "bot.py"]
 
