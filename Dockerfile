# One image for the four Python simulators; the service command selects the app.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY simcore /app/simcore
COPY mom /app/mom
COPY iot /app/iot
COPY esb /app/esb
COPY cv /app/cv
COPY tests /app/tests
EXPOSE 8000
CMD ["uvicorn", "mom.app:app", "--host", "0.0.0.0", "--port", "8000"]
