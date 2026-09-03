FROM python:3.12-slim

WORKDIR /app

# fping/nmap — для пунктов меню "Проверить доступность"/"Сканировать порты".
# gcc/libldap2-dev/libsasl2-dev/libssl-dev — нужны только на этапе сборки
# python-ldap (C-расширение, компилируется при pip install), в runtime не
# используются, но удалять после сборки не стали — слой всё равно общий
# с pip install ниже, выгоды от отдельного builder-stage здесь нет.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fping nmap gcc libldap2-dev libsasl2-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 8000

# collectstatic — в entrypoint.sh, а не здесь: на этом этапе .env ещё не
# примонтирован, а settings.py требует реальных SECRET_KEY/NETBOX_URL/
# NETBOX_TOKEN без дефолтов.
ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--timeout", "60"]
