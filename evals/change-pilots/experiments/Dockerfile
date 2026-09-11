# Build ONLY with build_image.py's allowlisted, pinned source context.
FROM rust:1.96.0-bookworm AS compiler
RUN apt-get update && apt-get install -y --no-install-recommends wget gnupg ca-certificates \
 && wget -qO /etc/apt/trusted.gpg.d/llvm.asc https://apt.llvm.org/llvm-snapshot.gpg.key \
 && echo 'deb https://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-22 main' > /etc/apt/sources.list.d/llvm.list \
 && apt-get update && apt-get install -y --no-install-recommends llvm-22-dev libpolly-22-dev clang-22 \
 && rm -rf /var/lib/apt/lists/*
ENV LLVM_SYS_221_PREFIX=/usr/lib/llvm-22 PRISM_CC=/usr/lib/llvm-22/bin/clang
WORKDIR /src
COPY compiler/ ./
RUN cargo build --release --locked && target/release/prism --version | grep '^prism 0.18.0$'

FROM node:25.2.1-bookworm-slim AS node
FROM python:3.14.7-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends wget gnupg ca-certificates \
 && wget -qO /etc/apt/trusted.gpg.d/llvm.asc https://apt.llvm.org/llvm-snapshot.gpg.key \
 && echo 'deb https://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-22 main' > /etc/apt/sources.list.d/llvm.list \
 && apt-get update && apt-get install -y --no-install-recommends llvm-22 clang-22 ripgrep \
 && apt-get purge -y wget gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && npm install --prefix /opt/typescript typescript@5.9.3 @types/node@25.0.3 \
 && ln -s /opt/typescript/node_modules/typescript/bin/tsc /usr/local/bin/tsc \
 && npm cache clean --force
COPY --from=compiler /src/target/release/prism /usr/local/bin/prism
COPY compiler/docs/src/spec.md /opt/prism-docs/spec.md
COPY compiler/docs/src/stdlib /opt/prism-docs/stdlib
COPY compiler/lib/std /opt/prism-lib/std
COPY provenance.json /opt/prism-toolchain-provenance.json
ENV PRISM_CC=/usr/lib/llvm-22/bin/clang HOME=/tmp/home PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /work /tmp/home && chown -R 1000:1000 /work /tmp/home \
 && python3 --version && node --version && prism --version && tsc --version
USER 1000:1000
WORKDIR /work
ENTRYPOINT []
CMD ["sleep", "infinity"]
