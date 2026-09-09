# Base image for the AIForgeCrew GitLab build job.
# Your original, plus five deltas — each marked AIFORGE and explained in
# ci/README.md. Nothing else is changed.
FROM ubuntu:24.04

# ensure local python is preferred over distribution python
ENV PATH=/root/.local/bin:$PATH

ARG ARTIFACTORY2_PASSWORD
ARG ARTIFACTORY_USERNAME
ARG ARTIFACTORY_PASSWORD

ENV PYTHON_VERSION=3.13.7
# AIFORGE 1: our uv.lock pins numpy 1.26.4, which has no cp313 wheel. On 3.13
# uv falls back to the sdist and needs a C compiler this image does not carry.
ENV PYTHON_VERSION_AIFORGE=3.12.11

# AIFORGE 2: git (the job runs in a repo), tmux (~1MB — without it the
# persistent-shell tests skip rather than run).
# runtime dependencies excl build-essential libapt-pkg-dev
RUN set -eux; \
    apt-get update && \
    apt-get install -y --no-install-recommends  curl unzip default-jre-headless ca-certificates python3-full python3-pip git tmux && \
    apt-get dist-clean

# Get certificate and use it
ENV JAVA_HOME=/usr/lib/jvm/default-java
RUN set -eux; \
    curl --header "Authorization: Bearer $ARTIFACTORY2_PASSWORD" --insecure -o /usr/local/share/ca-certificates/infra_intermediate.crt https://artifactory2.internal:443/artifactory/Public/infra_intermediate.cert.pem && \
    update-ca-certificates && \
    keytool -noprompt -importcert -alias infra_intermediate -keystore ${JAVA_HOME}/lib/security/cacerts -storepass changeit -file /usr/local/share/ca-certificates/infra_intermediate.crt

# AIFORGE 3: pin uv, so rebuilding this image does not silently change the
# resolver that reads uv.lock.
ARG UV_VERSION=0.9.30
RUN pip install --break-system-packages "uv==${UV_VERSION}"

ENV UV_PYTHON_INSTALL_DIR=/opt/python
ENV UV_MANAGED_PYTHON=true
# AIFORGE 4: UV_NO_CACHE=true was here. It makes GitLab's .uv-cache useless and
# rules out the warm-cache air-gap fallback. Left to the job to decide.

RUN set -eux; \
    uv python install --no-progress ${PYTHON_VERSION} ${PYTHON_VERSION_AIFORGE} && \
    ln /root/.local/bin/python3.13 /root/.local/bin/python3 && \
    ln /root/.local/bin/python3 /root/.local/bin/python

# AIFORGE 5: after the installs above, never fetch an interpreter at job time —
# an offline runner would hang or fail obscurely. Now a missing version says so.
ENV UV_PYTHON_DOWNLOADS=never

RUN export PYTHONDONTWRITEBYTECODE=1; \
    which python3; \
    python3 --version; \
    uv --version; \
    uv python list --only-installed
