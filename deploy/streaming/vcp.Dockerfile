# Tesla Vehicle Command HTTP Proxy
#
# Required for telemetry-config registration (and signed vehicle commands).
# Tesla publishes the source at https://github.com/teslamotors/vehicle-command
# We build the tesla-http-proxy binary and run it with our partner ECDSA key.

FROM golang:1.22-alpine AS build
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth=1 https://github.com/teslamotors/vehicle-command.git . \
 && go build -o /out/tesla-http-proxy ./cmd/tesla-http-proxy

FROM alpine:3.19
RUN apk add --no-cache ca-certificates openssl
COPY --from=build /out/tesla-http-proxy /usr/local/bin/tesla-http-proxy

# Partner private key is injected at runtime from Railway secret TESLA_PRIVATE_KEY.
# Self-signed cert pair for the proxy's own HTTPS listener.
RUN mkdir -p /etc/proxy
COPY deploy/streaming/vcp-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PROXY_HOST=0.0.0.0
EXPOSE 4443
CMD ["/usr/local/bin/entrypoint.sh"]
