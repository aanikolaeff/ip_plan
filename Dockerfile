# React-таблица (см. docs/plans/2026-09-03-ip-table-react-design.md) —
# собирается отдельной стадией, в финальный образ попадает только
# ip_plan/static/ip_plan/dist/ (без node_modules/исходников). package*.json
# копируются отдельно от остального дерева — кэш `npm ci` не сбрасывается
# правкой .py/.html файлов, только правкой самих зависимостей.
FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY package.json package-lock.json* ./
RUN npm install
COPY vite.config.js ./
COPY ip_plan/static_src ./ip_plan/static_src
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

# fping/nmap — для пунктов меню "Проверить доступность"/"Сканировать порты".
# gcc/libldap2-dev/libsasl2-dev/libssl-dev — нужны только на этапе сборки
# python-ldap (C-расширение, компилируется при pip install), в runtime не
# используются, но удалять после сборки не стали — слой всё равно общий
# с pip install ниже, выгоды от отдельного builder-stage здесь нет.
# tesseract-ocr — для распознавания штампа "дата + № письма" в шапке PDF
# (ioc_parser.extract_letter_header_stamp, см. requirements.txt про
# pytesseract). Только "eng" (пакет по умолчанию, без tesseract-ocr-rus) —
# сам штамп это цифры/./−// и "№", кириллица там не встречается.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fping nmap gcc libldap2-dev libsasl2-dev libssl-dev tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-build /frontend/ip_plan/static/ip_plan/dist ./ip_plan/static/ip_plan/dist
RUN chmod +x entrypoint.sh

EXPOSE 8000

# collectstatic — в entrypoint.sh, а не здесь: на этом этапе .env ещё не
# примонтирован, а settings.py требует реальных SECRET_KEY/NETBOX_URL/
# NETBOX_TOKEN без дефолтов.
ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--timeout", "60"]
