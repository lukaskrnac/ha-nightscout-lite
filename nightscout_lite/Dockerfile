ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3

COPY app /app

WORKDIR /app
CMD ["python3", "-u", "/app/main.py"]
