# ninfer-console restarter: a single-file, stdlib-only sidecar whose
# only capability is restarting one named container (see
# restarter.py). It holds the Docker socket so the panel never has
# to. Publish it only on an internal network; never expose its port.
FROM python:3.13-alpine
COPY restarter.py /app/restarter.py
ENV RESTARTER_PORT=8080
EXPOSE 8080
CMD ["python3", "/app/restarter.py"]
