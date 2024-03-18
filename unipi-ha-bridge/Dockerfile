ARG BUILD_FROM
FROM $BUILD_FROM


# Install requirements for add-on
RUN \
  apk add --no-cache \
    python3 cmd:pip3 \
  && pip3 install --no-cache-dir --break-system-packages \ 
    paho-mqtt loguru websocket-client ConfigArgParse requests


# Python 3 HTTP Server serves the current working dir
# So let's set it to our add-on persistent data directory.
WORKDIR /data

# Copy data for add-on
COPY run.sh unipi2ha.py /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

