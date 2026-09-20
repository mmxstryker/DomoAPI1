FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY domo_mcp.py .

# DOMO_CLIENT_ID, DOMO_CLIENT_SECRET, and MCP_AUTH_TOKEN are read from the
# environment at runtime — set them as secrets on your hosting platform,
# never bake them into this image.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "domo_mcp.py"]
