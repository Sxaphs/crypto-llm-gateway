# Modified QuantumNous/new-api, licensed under GNU AGPLv3.
# Upstream: https://github.com/QuantumNous/new-api
# Build from the repository root: docker build -f new-api/Dockerfile .

FROM alpine:3.22.2 AS source
ARG UPSTREAM_TAG=v1.0.0-rc.49
ARG UPSTREAM_COMMIT=48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0
RUN apk add --no-cache ca-certificates git
WORKDIR /src
RUN git clone --depth 1 --branch "${UPSTREAM_TAG}" https://github.com/QuantumNous/new-api.git . \
    && test "$(git rev-parse HEAD)" = "${UPSTREAM_COMMIT}"
COPY patches/new-api/0001-exact-gmpay-settlement.patch /tmp/exact-gmpay-settlement.patch
RUN git apply --check --whitespace=error-all /tmp/exact-gmpay-settlement.patch \
    && git apply --whitespace=error-all /tmp/exact-gmpay-settlement.patch

# The following stages mirror the upstream v1.0.0-rc.49 Dockerfile.
FROM oven/bun:1.4.4@sha256:1d22548f177f4ff38e0c15baa3c2c46c6c2060ba2d1bc573bb4720b407548f1d AS builder

WORKDIR /build/web
COPY --from=source /src/web/package.json /src/web/bun.lock ./
RUN bun install --frozen-lockfile
COPY --from=source /src/web/ ./
COPY --from=source /src/VERSION /build/VERSION
RUN DISABLE_ESLINT_PLUGIN='true' VITE_REACT_APP_VERSION=$(cat /build/VERSION) bun run build

FROM golang:1.26.10-alpine@sha256:9ce231b28998b32e116927c61aa5c406183061d4f3f2d61b292c88e7f6bdf7ca AS builder2
ENV GO111MODULE=on CGO_ENABLED=0 GOWORK=off

ARG TARGETOS
ARG TARGETARCH
ENV GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-amd64}
ENV GOEXPERIMENT=greenteagc

WORKDIR /build

COPY --from=source /src/go.mod /src/go.sum ./
# relaykit and tokenkit are local submodules referenced via replace; their
# go.mod files must be present for go mod download to resolve the main module graph.
COPY --from=source /src/relaykit/go.mod ./relaykit/go.mod
COPY --from=source /src/tokenkit/go.mod ./tokenkit/go.mod
RUN go mod download

COPY --from=source /src/ ./
COPY --from=builder /build/web/dist ./web/dist
RUN go build -pgo="$(go list -m -f '{{.Dir}}' github.com/Calcium-Ion/moejs)/default.pgo" -ldflags "-s -w -X 'github.com/QuantumNous/new-api/common.Version=$(cat VERSION)'" -o new-api

FROM debian:bookworm-slim@sha256:f06537653ac770703bc45b4b113475bd402f451e85223f0f2837acbf89ab020a

LABEL org.opencontainers.image.title="New API" \
      org.opencontainers.image.source="https://github.com/QuantumNous/new-api" \
      org.opencontainers.image.revision="48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata libasan8 wget \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

COPY --from=builder2 /build/new-api /
COPY --from=source /src/LICENSE /src/NOTICE /src/THIRD-PARTY-LICENSES.md /licenses/
EXPOSE 3000
WORKDIR /data
ENTRYPOINT ["/new-api"]
