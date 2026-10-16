ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3

COPY app /app

WORKDIR /app
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]
