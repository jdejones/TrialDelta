FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --create-home --uid 10001 app
COPY --chown=app:app trialdelta ./trialdelta
RUN mkdir /app/data && chown app:app /app/data
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "trialdelta.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
