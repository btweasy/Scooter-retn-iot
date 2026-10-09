FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY emulator.py .
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-u", "emulator.py"]
