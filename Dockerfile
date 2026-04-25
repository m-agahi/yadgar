FROM python:3.14-slim

RUN pip install --no-cache-dir yadgar

EXPOSE 8765

VOLUME /data

ENV YADGAR_HOST=0.0.0.0 \
    YADGAR_PORT=8765 \
    YADGAR_DB_PATH=/data/surreal_db

CMD ["yadgar", "--transport", "streamable-http"]
