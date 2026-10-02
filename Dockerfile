FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends fonts-noto-cjk tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 apartment \
    && useradd --uid 10001 --gid apartment --no-create-home apartment
COPY requirements.txt requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt
COPY manage.py ./
COPY config ./config
COPY core ./core
COPY templates ./templates
COPY static ./static
COPY deploy/r4s/entrypoint.py deploy/r4s/gunicorn.conf.py ./deploy/r4s/
RUN python manage.py collectstatic --noinput
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "deploy/r4s/entrypoint.py"]
CMD ["gunicorn", "--config", "deploy/r4s/gunicorn.conf.py", "config.wsgi:application"]
