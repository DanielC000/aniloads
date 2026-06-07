FROM python:3.11-slim-bookworm

WORKDIR /usr/src/app/anime-loads

# Install Firefox ESR, geckodriver, and build deps
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
       firefox-esr libgl1-mesa-glx libglib2.0-0 wget lbzip2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && GECKO_VER=$(wget -qO- https://api.github.com/repos/mozilla/geckodriver/releases/latest \
       | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])") \
    && wget -qO /tmp/gecko.tar.gz \
       "https://github.com/mozilla/geckodriver/releases/download/${GECKO_VER}/geckodriver-${GECKO_VER}-linux64.tar.gz" \
    && tar -xf /tmp/gecko.tar.gz -C /usr/local/bin && rm /tmp/gecko.tar.gz \
    && chmod +x /usr/local/bin/geckodriver

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python", "anibot.py", "--configfile", "/config/ani.json"]
CMD ["--docker"]
