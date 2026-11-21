FROM python:3.13.9-slim-bookworm
WORKDIR /bot
COPY mechfight.py bot.py ./
USER 65534:65534
ENTRYPOINT ["python3", "-B", "-u", "/bot/bot.py"]
