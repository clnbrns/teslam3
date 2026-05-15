# Tesla Fleet Telemetry Server
#
# Receives MQTT/HTTP push from the car (encrypted with our partner cert).
# Decodes protobuf telemetry. We configure it to forward decoded records
# to the main FastAPI app's /telemetry endpoint via HTTP dispatcher.

FROM golang:1.22-alpine AS build
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth=1 https://github.com/teslamotors/fleet-telemetry.git . \
 && go build -o /out/fleet-telemetry ./cmd/server

FROM alpine:3.19
RUN apk add --no-cache ca-certificates
COPY --from=build /out/fleet-telemetry /usr/local/bin/fleet-telemetry

COPY deploy/streaming/fts-config.json /etc/fts/config.json
COPY deploy/streaming/fts-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 443 4443
CMD ["/usr/local/bin/entrypoint.sh"]
