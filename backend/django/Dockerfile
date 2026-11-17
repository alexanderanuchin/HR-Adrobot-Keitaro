FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
EXPOSE 8001
CMD ["python", "-m", "gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8001", "--workers", "2", "--access-logfile", "-"]
