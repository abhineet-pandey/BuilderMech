# The match runs this with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, map at /arena.
FROM python:3.13.5-slim
RUN pip install --no-cache-dir numpy==2.2.6
WORKDIR /bot
COPY mechfight.py world.py bot.py ./
ENV PYTHONDONTWRITEBYTECODE=1
ENV P_LOAD=gunlaser P_SEED=9 P_HEALHIDE=0.7 P_CUBECAP=16 P_UPG=hp
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]