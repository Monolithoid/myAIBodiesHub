# Build context: robotics/ (brain_client.py is shared with body-gateway).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY regie/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY regie/regie.py regie/twitch_auth.py regie/regie.html regie/robot.html regie/games.html \
     regie/stories.html regie/favicon.svg regie/favicon.ico regie/apple-touch-icon.png ./
COPY brain/body_gateway/brain_client.py ./
COPY regie/storyteller/*.py regie/storyteller/prerecording.schema.json ./storyteller/
COPY regie/storyteller/prompts ./storyteller/prompts
COPY regie/storyteller/examples ./storyteller/examples

EXPOSE 8097
CMD ["python3", "/app/regie.py"]
