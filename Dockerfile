FROM python:3.12-slim

# Standard library only — nothing to pip install.
WORKDIR /app
COPY ice_time.py .

# Your links and the cached schedule live on a mounted volume, never in the image.
# The service starts as root only long enough to take ownership of /data, then
# runs as PUID:PGID (99:100 = Unraid's nobody:users). Change them to match your
# host, or pass --user and it never runs as root at all.
ENV DATA_DIR=/data PORT=8080 PUID=99 PGID=100 PYTHONUNBUFFERED=1
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8080'),timeout=4)" || exit 1

CMD ["python", "-u", "ice_time.py"]
