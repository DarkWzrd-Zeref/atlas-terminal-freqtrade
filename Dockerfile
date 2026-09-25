FROM node:22-slim AS atlas-frequi
WORKDIR /ui
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The same upstream FreqUI 3.1.2 release previously installed by Freqtrade,
# pinned to its source commit and built for the private portal route.
RUN curl -fsSL https://codeload.github.com/freqtrade/frequi/tar.gz/f76ad9e7f8b6ee8e2d1f214a484d6182393be717 -o /tmp/ui.tar.gz \
    && tar -xzf /tmp/ui.tar.gz --strip-components=1 -C /ui \
    && npm ci && npm run build -- --base=/apps/freqtrade/

FROM python:3.14.7-slim-trixie AS base

# Setup env
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONFAULTHANDLER=1
ENV PATH=/home/ftuser/.local/bin:$PATH
ENV FT_APP_ENV="docker"

# Prepare environment
RUN mkdir /freqtrade \
  && apt-get update \
  && apt-get -y install --no-install-recommends sudo libatlas3-base curl sqlite3 libgomp1 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && useradd -u 1000 -G sudo -U -m -s /bin/bash ftuser \
  && chown ftuser:ftuser /freqtrade \
  # Allow sudoers
  && echo "ftuser ALL=(ALL) NOPASSWD: /bin/chown" >> /etc/sudoers

WORKDIR /freqtrade

# Install dependencies
FROM base AS python-deps
RUN  apt-get update \
  && apt-get -y install --no-install-recommends build-essential libssl-dev git libffi-dev libgfortran5 pkg-config cmake gcc \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --upgrade pip wheel

# Install dependencies
COPY --chown=ftuser:ftuser requirements.txt requirements-hyperopt.txt /freqtrade/
USER ftuser
RUN  pip install --user --no-cache-dir "numpy<3.0" \
  && pip install --user --no-cache-dir -r requirements-hyperopt.txt

# Copy dependencies to runtime-image
FROM base AS runtime-image

COPY --from=python-deps --chown=ftuser:ftuser /home/ftuser/.local /home/ftuser/.local

USER ftuser
# Install and execute
COPY --chown=ftuser:ftuser . /freqtrade/

RUN pip install -e . --user --no-cache-dir \
  && mkdir /freqtrade/user_data/
COPY --from=atlas-frequi --chown=ftuser:ftuser /ui/dist /freqtrade/freqtrade/rpc/api_server/ui/installed
RUN printf '3.1.2-atlas' > /freqtrade/freqtrade/rpc/api_server/ui/installed/.uiversion

# Initialize the Railway volume and drop privileges before importing packages
# installed in ftuser's Python user site. CLI remains available as `freqtrade`.
ENTRYPOINT []
CMD ["python", "/freqtrade/atlas/start.py"]
