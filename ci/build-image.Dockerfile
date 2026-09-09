# Build image for AIForgeCrew's GitLab CI job.
#
# Dedicated to that one job, so it carries exactly what the job needs and
# nothing else. The runner has no route off the estate: everything below comes
# from Artifactory or the Ubuntu archive, and the job that uses this image
# fetches nothing the image did not already provide.
FROM ubuntu:24.04

ARG ARTIFACTORY2_PASSWORD
ARG CA_URL=https://artifactory2.internal:443/artifactory/Public/infra_intermediate.cert.pem
# Pinned: unpinned, a rebuild silently changes the resolver that reads uv.lock.
ARG UV_VERSION=0.9.30
# 3.12, not 3.13: uv.lock pins numpy 1.26.4, which publishes no cp313 wheel, so
# on 3.13 uv falls back to the sdist and needs a C compiler this image does not
# carry. 3.13 also fails three of AIForgeCrew's TLS chain tests.
ARG PYTHON_VERSION=3.12.11

# uv's interpreter ahead of the distribution python
ENV PATH=/root/.local/bin:$PATH
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PYTHONDONTWRITEBYTECODE=1

# curl/unzip for the cert step, JRE for keytool, git because the job runs in a
# repository, tmux because AIForgeCrew's persistent-shell tests skip without it.
# No build-essential: every dependency in uv.lock resolves to a wheel on 3.12.
RUN set -eux; \
    apt-get update && \
    apt-get install -y --no-install-recommends curl unzip default-jre-headless ca-certificates python3-full python3-pip git tmux && \
    apt-get dist-clean

# The estate CA, into both the system store and the JVM's.
RUN set -eux; \
    curl --header "Authorization: Bearer $ARTIFACTORY2_PASSWORD" --insecure -o /usr/local/share/ca-certificates/infra_intermediate.crt "$CA_URL" && \
    update-ca-certificates && \
    keytool -noprompt -importcert -alias infra_intermediate -keystore ${JAVA_HOME}/lib/security/cacerts -storepass changeit -file /usr/local/share/ca-certificates/infra_intermediate.crt

# uv is an ordinary wheel, so it comes from the same index as everything else.
RUN pip install --break-system-packages "uv==${UV_VERSION}"

ENV UV_PYTHON_INSTALL_DIR=/opt/python
ENV UV_MANAGED_PYTHON=true

RUN set -eux; \
    uv python install --no-progress "${PYTHON_VERSION}" && \
    ln -sf "/root/.local/bin/python${PYTHON_VERSION%.*}" /root/.local/bin/python3 && \
    ln -sf /root/.local/bin/python3 /root/.local/bin/python

# After the install above: never reach for an interpreter at job time. An
# offline runner would fail obscurely; now a version miss says so immediately.
ENV UV_PYTHON_DOWNLOADS=never

# Deliberately NOT set: UV_NO_CACHE. Setting it here would make the job's
# .uv-cache do nothing and rule out a warm-cache run with no index at all.

RUN set -eux; \
    which python3; python3 --version; python --version; \
    uv --version; uv python list --only-installed; \
    git --version; tmux -V; keytool -list -keystore ${JAVA_HOME}/lib/security/cacerts -storepass changeit -alias infra_intermediate >/dev/null
