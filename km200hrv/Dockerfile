ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip \
 && pip3 install --no-cache-dir --break-system-packages \
      pycryptodome requests "paho-mqtt>=2,<3"

COPY km200hrv.py /app/km200hrv.py

CMD ["python3", "-u", "/app/km200hrv.py"]
