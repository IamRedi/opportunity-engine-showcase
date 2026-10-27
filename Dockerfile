FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
COPY constraints/ constraints/
RUN pip install --no-cache-dir -r requirements.txt -c constraints/python314-linux.txt
COPY *.py ./
COPY templates/ templates/
COPY static/ static/
COPY scripts/ scripts/
COPY demo/ demo/
RUN useradd --create-home --uid 1000 appuser && mkdir /data && chown -R appuser:appuser /app /data
USER appuser
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)"
CMD ["python", "scripts/serve_demo.py", "--container"]
