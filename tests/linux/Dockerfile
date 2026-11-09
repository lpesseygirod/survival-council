# Checks the real bubblewrap sandbox on Linux, set up like the server.
#   docker build -f tests/linux/Dockerfile -t council-sandbox-check . && docker run --rm --privileged council-sandbox-check
FROM ubuntu:24.04
RUN apt-get update -qq && apt-get install -y -qq bubblewrap sudo python3 python3-venv procps acl >/dev/null
RUN useradd -m -s /bin/bash council && useradd -M -s /usr/sbin/nologin council-ai \
 && python3 -m venv /opt/council-ai/venv \
 && printf 'Defaults:council !use_pty\ncouncil ALL=(council-ai) NOPASSWD: /usr/bin/bwrap, /usr/bin/pkill\n' > /etc/sudoers.d/council \
 && chmod 440 /etc/sudoers.d/council
COPY survival /home/council/app/survival
COPY config.toml /home/council/app/config.toml
COPY tests/linux/check.py /home/council/check.py
RUN mkdir -p /home/council/app/secrets /home/council/app/data && echo SECRET > /home/council/app/secrets/vault.key \
 && cp -r /home/council/app/survival/sdk /opt/council-ai/sdk \
 && mkdir -p /srv/council-code/paper && chown -R council-ai:council /srv/council-code && chmod -R 2770 /srv/council-code \
 && setfacl -R -d -m g:council:rwX /srv/council-code && setfacl -R -m g:council:rwX /srv/council-code \
 && chown -R council:council /home/council && chmod 700 /home/council /home/council/app/secrets
USER council
ENV COUNCIL_CODE_ROOT=/srv/council-code COUNCIL_SDK_DIR=/opt/council-ai/sdk PYTHONPATH=/home/council/app
CMD ["python3", "/home/council/check.py"]
