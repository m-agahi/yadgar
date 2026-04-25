FROM python:3.14-slim

RUN pip install --no-cache-dir yadgar

EXPOSE 8765 42069

VOLUME /data

ENV YADGAR_DATA_DIR=/data \
    YADGAR_PORT=8765

CMD ["yadgar", "start", "--host", "0.0.0.0", "--transport", "streamable-http"]
