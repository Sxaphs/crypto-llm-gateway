# Modified GMWalletApp/epusdt, licensed under GNU GPLv3.
# Upstream: https://github.com/GMWalletApp/epusdt
# Build from the repository root: docker build -f epusdt/Dockerfile .

FROM alpine:3.22.2 AS source
ARG UPSTREAM_TAG=v2.0.0
ARG UPSTREAM_COMMIT=619cecb11d6bbd1dcdd1b698ee08b37fc722d3e3
RUN apk add --no-cache ca-certificates git
WORKDIR /src
RUN git clone --depth 1 --branch "${UPSTREAM_TAG}" https://github.com/GMWalletApp/epusdt.git . \
    && test "$(git rev-parse HEAD)" = "${UPSTREAM_COMMIT}"
COPY patches/epusdt/0001-postgres-settings-compat.patch /tmp/postgres-settings-compat.patch
RUN git apply --check --whitespace=error-all /tmp/postgres-settings-compat.patch \
    && git apply --whitespace=error-all /tmp/postgres-settings-compat.patch

FROM golang:1.26.10-alpine@sha256:9ce231b28998b32e116927c61aa5c406183061d4f3f2d61b292c88e7f6bdf7ca AS builder
RUN apk add --no-cache --update git build-base
ENV CGO_ENABLED=0 GOWORK=off
WORKDIR /app/src
COPY --from=source /src/src/go.mod /src/src/go.sum ./
RUN go mod download
COPY --from=source /src/ /app/
ARG BUILD_VERSION=v2.0.0
RUN go build -trimpath -ldflags="-s -w -X github.com/GMWalletApp/epusdt/config.BuildVersion=${BUILD_VERSION}" -o /app/epusdt .

FROM alpine:3.22.2
ENV TZ=UTC
RUN apk --no-cache add ca-certificates tzdata wget
WORKDIR /app
COPY --from=source /src/src/.env.example /app/.env
COPY --from=source /src/LICENSE /licenses/LICENSE
COPY --from=builder /app/epusdt /app/epusdt
VOLUME /data
EXPOSE 8000
ENTRYPOINT ["./epusdt", "http", "start"]
