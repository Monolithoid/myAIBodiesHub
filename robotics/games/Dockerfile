FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY game_core.py game_master.py hangman.py overlay.html ./
COPY words/hangman_fr.json words/README.md words/

EXPOSE 8098
CMD ["python3", "/app/game_master.py"]
